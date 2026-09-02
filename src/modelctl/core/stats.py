#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/stats.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : 用量统计服务
# ===============================================================================

"""core/stats.py — 用量统计服务（多引擎指标映射）。

迁移自 script/usage_stats_server.py：把单一 llama-server 数据源改为
targets 列表 + 按 ?model= 路由，指标名映射由各引擎适配器的
metrics_mapping() 提供（mapping 为 None 表示该引擎不支持精确统计）。

对外 /api/usage 输出字段与现版完全一致（cc-switch 无感），仅额外增加
"model" 字段标识数据源；另支持 ?view=tier 返回 cc-switch Token Plan
模板可渲染的百分比徽章数组（需在 profile 配置 usage.budget）。

支持两种数据获取模式（USAGE_MODE）：
- poll（默认）：后台线程按 USAGE_POLL_INTERVAL 定时轮询 /metrics，
  /api/usage 返回最近一次缓存快照。
- on-demand：不启动后台线程，由 cc-switch 轮询触发，每次请求同步拉取。

纯标准库实现，零第三方依赖。独立运行：
    python -m modelctl.core.stats
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT, load_env

USAGE_PORT = 5002


def _parse_env_bool(value: str | None, default: bool = True) -> bool:
    """env 开关解析：{"1","true","yes","on"} → True；{"0","false","no","off"} → False。
    空串/None/未知字符串回退到 default（保持现状行为，避免误关）。"""
    if value is None or value.strip() == "":
        return default
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class _NativeSample:
    """vLLM per-request 原生指标单样本（60s/20 请求滑窗口径）。"""
    ts: float                    # time.monotonic 入账时
    tokens_per_second: float     # vLLM 原生 decode 速率（仅 decode 段）
    prompt_inflight_rate: float  # num_prompt_tokens / ttft_s（与 vLLM avg_prompt gauge 同量纲）
    ttft_ms: float               # time_to_first_token_ms
    ttft_s: float                # 同上（秒）


def _percentile(values: list[float], p: float) -> float | None:
    """线性插值法百分位；空列表返回 None；单元素 P50/P95 都返回该元素。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _fmt_tokens(value: float) -> str:
    """token 数量换算为 k/m/g 单位缩写（648532 -> 648.5k），减少显示长度。

    规则：>=1e9 用 g（十亿）、>=1e6 用 m（百万）、>=1e3 用 k（千），否则整数。
    """
    av = abs(value)
    sign = "-" if value < 0 else ""
    if av >= 1e9:
        return f"{sign}{av / 1e9:.2f}g"
    if av >= 1e6:
        return f"{sign}{av / 1e6:.2f}m"
    if av >= 1e3:
        return f"{sign}{av / 1e3:.1f}k"
    return f"{sign}{int(round(av))}"


def benchmark_rates(completions_url: str, api_key: str | None, model: str) -> tuple[float, float, int] | None:
    """主动测速：发一次短流式请求，返回 (input_rate, output_rate, ttft_ms)。

    输入速率 = prompt_tokens / TTFT（prefill）；输出速率 = completion_tokens / (总耗时 - TTFT)（decode）。
    TTFT 取首个非 [DONE] 的 data: 事件到达时刻（reasoning 模型为思考首 token 时刻）。
    请求失败 / 超时 / 空响应返回 None。
    """
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
            "stream": True,
            "stream_options": {"include_usage": True},
            # Qwen3.5 家族直连测速时默认思考会占满 max_tokens，导致输出速率失真且慢；
            # 显式关闭思考让测速请求直接输出正文。不识别该字段的引擎（llama.cpp 等）
            # 忽略或报错——报错时 benchmark 失败返回 None，仅影响兜底，不影响主流程。
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(completions_url, data=body, headers=headers, method="POST")
    t_start = time.perf_counter()
    t_ttft: float | None = None
    t_end: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            reader = io.TextIOWrapper(resp, encoding="utf-8", errors="replace")
            for line in reader:
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                if t_ttft is None:
                    t_ttft = time.perf_counter()
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                usage = data.get("usage") if isinstance(data, dict) else None
                if isinstance(usage, dict):
                    if isinstance(usage.get("prompt_tokens"), int):
                        prompt_tokens = usage["prompt_tokens"]
                    if isinstance(usage.get("completion_tokens"), int):
                        completion_tokens = usage["completion_tokens"]
        t_end = time.perf_counter()
    except (OSError, ValueError):
        return None
    if t_ttft is None or t_end is None:
        return None
    ttft_s = t_ttft - t_start
    if ttft_s <= 0:
        return None
    prompt_tokens = prompt_tokens if prompt_tokens is not None else len("hi") // 4
    completion_tokens = completion_tokens if completion_tokens is not None else 1
    input_rate = round(prompt_tokens / ttft_s, 1)
    decode_s = (t_end - t_start) - ttft_s
    output_rate = round(completion_tokens / decode_s, 1) if decode_s > 0 else 0.0
    return input_rate, output_rate, round(ttft_s * 1000)


def _build_patterns(mapping: dict[str, list[str]]) -> dict[str, list[re.Pattern]]:
    """按 mapping 构建预编译的指标匹配模式（避免每轮轮询重复编译正则）。

    匹配形如 "name 123" 或 "name{label=\"x\"} 123" 的 Prometheus 采样行。
    """
    return {
        key: [
            re.compile(r"^" + re.escape(name) + r"(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE)
            for name in names
        ]
        for key, names in mapping.items()
    }


def _hist_mean(text: str, name: str) -> float:
    """Prometheus 直方图均值：name_sum / name_count。

    任一缺失或 count <= 0 返回 0.0。用于 vLLM time_to_first_token_seconds
    这类只有 Histogram、没有现成均值 gauge 的引擎内置指标。
    """
    sum_m = re.search(rf"^{re.escape(name)}_sum\s+([-+0-9.eE]+)\s*$", text, re.MULTILINE)
    cnt_m = re.search(rf"^{re.escape(name)}_count\s+([-+0-9.eE]+)\s*$", text, re.MULTILINE)
    if not sum_m or not cnt_m:
        return 0.0
    try:
        cnt = float(cnt_m.group(1))
    except ValueError:
        return 0.0
    if cnt <= 0:
        return 0.0
    return float(sum_m.group(1)) / cnt


def parse_metrics(text: str, mapping: dict[str, list[str]]) -> dict[str, float]:
    """解析 Prometheus 文本，返回指标名映射对应的数值。

    已知四个键（prompt_total / predicted_total / prompt_rate / predicted_rate）
    取 gauge 裸值（候选名第一个命中）。可选键 ttft_ms 额外支持直方图：
    候选名裸名未命中时用 <name>_sum / <name>_count 相除得均值（引擎内置 TTFT
    直方图，如 vllm:time_to_first_token_seconds）。未声明 ttft_ms 键恒得 0.0。
    """
    result = {
        "prompt_total": 0.0,
        "predicted_total": 0.0,
        "prompt_rate": 0.0,
        "predicted_rate": 0.0,
        "ttft_ms": 0.0,
    }
    patterns = _build_patterns({k: v for k, v in mapping.items() if k in result})
    for key, key_patterns in patterns.items():
        for pattern in key_patterns:
            m = pattern.search(text)
            if m:
                try:
                    result[key] = float(m.group(1))
                except ValueError:
                    pass
                break
    if "ttft_ms" in mapping and result["ttft_ms"] == 0.0:
        for name in mapping["ttft_ms"]:
            val = _hist_mean(text, name)
            if val:
                result["ttft_ms"] = val
                break
    return result


def calc_cost(prompt_total: float, predicted_total: float, price_in: float, price_out: float) -> float:
    """按元/M tokens 单价折算累计费用（元）。"""
    return prompt_total / 1e6 * price_in + predicted_total / 1e6 * price_out


def build_usage_payload(tokens: dict[str, float], usage_cfg: dict, start_time: float, now: float) -> dict:
    """由用量折算构造 cc-switch 可识别的 /api/usage 响应。

    tokens 键：prompt_total / predicted_total / prompt_rate / predicted_rate。
    usage_cfg 键：price_in / price_out / budget（均可选，缺省 price_in=1.0、price_out=2.0、无预算）。
    输出字段：isValid/used/unit/planName/extra/prompt_rate/predicted_rate/total/remaining。
    """
    price_in = float(usage_cfg.get("price_in", 1.0))
    price_out = float(usage_cfg.get("price_out", 2.0))
    budget_raw = usage_cfg.get("budget")
    budget = float(budget_raw) if budget_raw is not None else None
    prompt = tokens.get("prompt_total", 0.0)
    predicted = tokens.get("predicted_total", 0.0)
    prompt_rate = tokens.get("prompt_rate", 0.0)
    predicted_rate = tokens.get("predicted_rate", 0.0)
    ttft_ms_val = tokens.get("ttft_ms") or 0.0
    ttft_p95_val = tokens.get("ttft_ms_p95") or 0.0
    if ttft_ms_val <= 0:
        ttft_suffix = ""
    else:
        ttft_suffix = f"| 首 Token P50 = {round(ttft_ms_val)} ms"
        if ttft_p95_val > 0:
            ttft_suffix += "（P95 = " + str(round(ttft_p95_val)) + " ms）"
    used = round(calc_cost(prompt, predicted, price_in, price_out), 2)
    payload = {
        "isValid": True,
        "used": used,
        "unit": "CNY",
        "planName": "DeepSeek-V4-Flash 本地部署",
        "extra": (
            f"累计 {_fmt_tokens(prompt + predicted)} toks"
            f"（输入 {_fmt_tokens(prompt)}/输出 {_fmt_tokens(predicted)}）"
            f"| 输入速率 {prompt_rate:.1f} tok/s"
            f"| 输出速率 {predicted_rate:.1f} tok/s"
            f"{ttft_suffix}"
        ),
        "prompt_rate": prompt_rate,
        "predicted_rate": predicted_rate,
    }
    if budget is not None:
        payload["total"] = budget
        payload["remaining"] = round(max(budget - used, 0.0), 2)
    else:
        payload["total"] = None
        payload["remaining"] = None
    return payload


def _budget_of(usage_cfg: dict) -> float | None:
    """读取预算配置（元）；未设置、非法或非正数返回 None。"""
    raw = usage_cfg.get("budget")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def build_tier_item(name: str, snap: dict, usage_cfg: dict, plan_label: str) -> dict:
    """构造 cc-switch Token Plan 模板可渲染的单条徽章数据。

    cc-switch 在 templateType=token_plan 时把 used 解释为百分比（0-100），
    planName 作为徽章标签，extra 以 "{" 开头则按 JSON 解析出
    resetsAt/usedValueUsd/maxValueUsd/planLabel。本地部署无重置窗口，
    resetsAt 恒为 null；不带 USD 字段避免界面硬编码的 $ 前缀误导。

    未配置有效预算（usage.budget）时降级：isValid=False + used=0 +
    note="无预算配置"，不抛 ValueError——支持 "零配置看 tier" 场景。
    需要硬失败的调用方可以显式 check budget。
    """
    budget = _budget_of(usage_cfg)
    price_in = float(usage_cfg.get("price_in", 1.0))
    price_out = float(usage_cfg.get("price_out", 2.0))
    cost = calc_cost(snap.get("prompt_total", 0.0), snap.get("predicted_total", 0.0), price_in, price_out)
    if budget is None:
        return {
            "isValid": False,
            "planName": name,
            "used": 0.0,
            "unit": "CNY",
            "extra": json.dumps(
                {"resetsAt": None, "planLabel": plan_label, "note": "无预算配置"},
                ensure_ascii=False,
            ),
        }
    pct = min(max(round(cost / budget * 100.0, 1), 0.0), 100.0)
    return {
        "isValid": True,
        "planName": name,
        "used": pct,
        "unit": "CNY",
        "extra": json.dumps({"resetsAt": None, "planLabel": plan_label}, ensure_ascii=False),
    }


@dataclass
class StatsTarget:
    """单个模型的用量统计目标。mapping 为 None 表示该引擎不支持精确统计。"""

    name: str
    data_dir: Path
    metrics_url: str
    mapping: dict[str, list[str]] | None
    usage_cfg: dict = field(default_factory=dict)
    api_key: str | None = None
    aliases: list[str] = field(default_factory=list)
    # 主动测速配置（bench_url 为 None = 窗口无流量时不做兜底测速）
    bench_url: str | None = None
    bench_model: str | None = None
    # per-request 原生指标字段映射（仅 vLLM 双 flag 均开才非 None；其他引擎 None）
    native_mapping: dict[str, str] | None = None


# 主动测速结果缓存：cc-switch 约每 30-60s 轮询 /api/usage，节流避免每次都伪造请求
_BENCH_CACHE: dict[str, tuple[float, tuple[float, float, int]]] = {}
_BENCH_TTL = 30.0


def _bench_cached(target: StatsTarget) -> tuple[float, float, int] | None:
    """窗口无流量时的兜底测速（30s 节流）；未配置 bench_url 或测速失败返回 None。"""
    if not target.bench_url:
        return None
    now = time.time()
    cached = _BENCH_CACHE.get(target.name)
    if cached and now - cached[0] < _BENCH_TTL:
        return cached[1]
    result = benchmark_rates(target.bench_url, target.api_key, target.bench_model or target.name)
    if result is not None:
        _BENCH_CACHE[target.name] = (now, result)
    return result


class UsageCollector:
    """聚合单个模型 /metrics 用量。

    mode="poll"：后台线程定时轮询，维护最近一次缓存快照；
    mode="on-demand"：不启动后台线程，由 get_snapshot() 在每次请求时同步拉取。
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        poll_interval: float,
        api_key: str | None,
        data_dir: Path,
        mode: str = "poll",
        mapping: dict[str, list[str]] | None = None,
        native_mapping: dict[str, str] | None = None,
        bench_fallback: bool = True,
    ) -> None:
        self.name = name
        self.data_dir = data_dir
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.api_key = api_key
        self.mode = mode
        self.mapping = mapping or {}
        self.native_mapping = native_mapping
        if "USAGE_BENCH_FALLBACK" in os.environ:
            self.bench_fallback = _parse_env_bool(os.environ["USAGE_BENCH_FALLBACK"])
        else:
            self.bench_fallback = bench_fallback
        self._native_window: list[_NativeSample] = []
        self._native_window_ttl = 60.0
        self._native_window_cap = 20
        self._lock = threading.Lock()
        self._monotonic = time.monotonic  # 网关注入与轮询共用的速率计算时钟基准
        self._snapshot: dict[str, object] = {
            "ok": False,
            "error": None,
            "prompt_total": 0.0,
            "predicted_total": 0.0,
            "prompt_rate": 0.0,
            "predicted_rate": 0.0,
            "ttft_ms": 0.0,
            "ttft_ms_p95": 0.0,
            "rate_source": "none",
        }
        self._last = {"time": None, "predicted_total": 0.0}
        self._rate_window: list[tuple[float, float, float]] = []
        self._window_size = 10
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True) if mode == "poll" else None
        persisted_prompt, persisted_predicted = self._load_persisted()
        self._baseline = {
            "prompt_total": persisted_prompt,
            "predicted_total": persisted_predicted,
        }
        self._snapshot["prompt_total"] = persisted_prompt
        self._snapshot["predicted_total"] = persisted_predicted

    def _persist_path(self) -> Path:
        return self.data_dir / f"{self.name}.json"

    def _load_persisted(self) -> tuple[float, float]:
        path = self._persist_path()
        if not path.is_file():
            return 0.0, 0.0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return 0.0, 0.0
            return float(data.get("prompt_total", 0.0)), float(data.get("predicted_total", 0.0))
        except (OSError, ValueError, json.JSONDecodeError):
            return 0.0, 0.0

    def _persist(self, prompt_total: float, predicted_total: float) -> None:
        path = self._persist_path()
        tmp = path.with_suffix(".json.tmp")
        data = {
            "prompt_total": prompt_total,
            "predicted_total": predicted_total,
            "updated_at": time.time(),
        }
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except OSError:
            # 持久化失败不应中断轮询；错误信息仅通过日志/后续 snapshot 暴露
            pass

    def _record_window(self, now: float, prompt_total: float, predicted_total: float) -> None:
        self._rate_window.append((now, prompt_total, predicted_total))
        if len(self._rate_window) > self._window_size:
            self._rate_window.pop(0)

    def record_tokens(self, prompt_delta: int, completion_delta: int) -> None:
        """网关按真实请求用量累计 token（流式按增量调用），与轮询滑窗同源计算速率。"""
        if prompt_delta <= 0 and completion_delta <= 0:
            return
        now = self._monotonic()
        with self._lock:
            new_prompt = self._baseline["prompt_total"] + prompt_delta
            new_predicted = self._baseline["predicted_total"] + completion_delta
            self._baseline["prompt_total"] = new_prompt
            self._baseline["predicted_total"] = new_predicted
            self._snapshot["prompt_total"] = new_prompt
            self._snapshot["predicted_total"] = new_predicted
            self._snapshot["ok"] = True
            self._snapshot["error"] = None
            self._record_window(now, new_prompt, new_predicted)
            prompt_rate, predicted_rate = self._compute_window_rate()
            self._snapshot["prompt_rate"] = prompt_rate
            self._snapshot["predicted_rate"] = predicted_rate
            self._persist(new_prompt, new_predicted)
            # rate_source 提示：轮询失败/无值后网关端首次写入累积值时，标注窗口差分来源，
            # 供前端区分"网关实测"与"引擎 gauge"两条速率链路
            if self._snapshot["rate_source"] == "none" and prompt_rate > 0:
                self._snapshot["rate_source"] = "window_diff"

    def record_native_metrics(self, metric_dict: dict | None) -> None:
        """网关 mesh 回调注入 vLLM per-request 原生指标（单请求粒度滑窗口径）。

        metric_dict 的键由 native_mapping 指明（vLLM：tokens_per_second /
        time_to_first_token_ms / num_prompt_tokens）。native_mapping 为 None
        （非 vLLM 引擎）或入参非法时静默返回，不影响现有 /metrics 链路。
        """
        if not self.native_mapping or not isinstance(metric_dict, dict):
            return
        try:
            tps = float(metric_dict[self.native_mapping["rate"]])
            ttft_ms = float(metric_dict[self.native_mapping["ttft_ms"]])
            prompt_tk = int(metric_dict.get(self.native_mapping["prompt_tokens"]) or 0)
        except (KeyError, TypeError, ValueError):
            return
        if tps <= 0 or ttft_ms <= 0:
            return
        ttft_s = ttft_ms / 1000.0
        prompt_rate = (prompt_tk / ttft_s) if ttft_s > 1e-6 else 0.0
        sample = _NativeSample(
            ts=self._monotonic(),
            tokens_per_second=tps,
            prompt_inflight_rate=prompt_rate,
            ttft_ms=ttft_ms,
            ttft_s=ttft_s,
        )
        with self._lock:
            self._native_window.append(sample)
            now = self._monotonic()
            while (
                now - self._native_window[0].ts > self._native_window_ttl
                or len(self._native_window) > self._native_window_cap
            ):
                self._native_window.pop(0)

    def _compute_native_row(self) -> dict:
        """由原生样本滑窗计算 ttft_ms P50/P95 与速率 P50（窗口内最近请求级统计）。"""
        with self._lock:
            samples = list(self._native_window)
        if not samples:
            return {
                "ttft_ms": 0.0,
                "ttft_ms_p95": 0.0,
                "prompt_rate": 0.0,
                "predicted_rate": 0.0,
                "has_any": False,
            }
        return {
            "ttft_ms": round(_percentile([s.ttft_ms for s in samples], 50) or 0.0, 2),
            "ttft_ms_p95": round(_percentile([s.ttft_ms for s in samples], 95) or 0.0, 2),
            "prompt_rate": round(_percentile([s.prompt_inflight_rate for s in samples], 50) or 0.0, 2),
            "predicted_rate": round(_percentile([s.tokens_per_second for s in samples], 50) or 0.0, 2),
            "has_any": True,
        }

    def _compute_window_rate(self) -> tuple[float, float]:
        if len(self._rate_window) < 2:
            return 0.0, 0.0
        oldest = self._rate_window[0]
        latest = self._rate_window[-1]
        dt = latest[0] - oldest[0]
        if dt <= 0:
            return 0.0, 0.0
        prompt_rate = max((latest[1] - oldest[1]) / dt, 0.0)
        predicted_rate = max((latest[2] - oldest[2]) / dt, 0.0)
        return prompt_rate, predicted_rate

    def start(self) -> None:
        if self._thread is not None:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def get_snapshot(self) -> dict:
        """返回用量快照。

        poll 模式返回最近一次缓存快照；on-demand 模式先同步拉取一次最新指标再返回。
        """
        if self.mode == "on-demand":
            self._poll_once()
        return self.snapshot()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.poll_interval)

    def _poll_once(self) -> None:
        url = f"{self.base_url}/metrics"
        try:
            request = urllib.request.Request(url)
            if self.api_key:
                request.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(request, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            metrics = parse_metrics(body, self.mapping)
        except Exception as error:  # noqa: BLE001 —— 轮询失败仅记录，不中断服务
            with self._lock:
                self._snapshot = {
                    "ok": False,
                    "error": str(error),
                    "prompt_total": 0.0,
                    "predicted_total": 0.0,
                    "prompt_rate": 0.0,
                    "predicted_rate": 0.0,
                    "ttft_ms": 0.0,
                    "ttft_ms_p95": 0.0,
                    "rate_source": "none",
                }
            return
        # 引擎 token 计数 gauge 可能恒为 0（如 vLLM 未启用 --enable-metrics），此时累计值
        # 由网关按真实请求写入持久化文件；两者取更大者作为最新累计，窗口差分即真实速率。
        persisted_prompt, persisted_predicted = self._load_persisted()
        prompt_total = max(metrics["prompt_total"], persisted_prompt)
        predicted_total = max(metrics["predicted_total"], persisted_predicted)
        now = time.monotonic()
        new_prompt = max(prompt_total, self._baseline["prompt_total"])
        new_predicted = max(predicted_total, self._baseline["predicted_total"])
        changed = new_prompt != self._baseline["prompt_total"] or new_predicted != self._baseline["predicted_total"]
        self._baseline["prompt_total"] = new_prompt
        self._baseline["predicted_total"] = new_predicted

        # 统一使用滑动窗口内 total 计数器差分计算平均速率，与市面主流监控一致
        self._record_window(now, new_prompt, new_predicted)
        prompt_rate, predicted_rate = self._compute_window_rate()
        # 引擎自带实时速率 gauge（vLLM 等）优先——客户端直连模型端口绕过网关时也能
        # 统计到真实吞吐；缺失/为 0 时退化为上面的窗口差分
        if metrics["prompt_rate"] > 0 or metrics["predicted_rate"] > 0:
            prompt_rate, predicted_rate = metrics["prompt_rate"], metrics["predicted_rate"]
        metrics["prompt_rate"] = prompt_rate
        metrics["predicted_rate"] = predicted_rate

        # rate_source 反映当前快照速率数据的实际来源：引擎 gauge > 窗口差分 > 无
        source = (
            "engine_gauge"
            if (metrics["prompt_rate"] > 0 or metrics["predicted_rate"] > 0)
            else ("window_diff" if (prompt_rate > 0 or predicted_rate > 0) else "none")
        )
        with self._lock:
            self._snapshot = {
                "ok": True,
                "error": None,
                "prompt_total": new_prompt,
                "predicted_total": new_predicted,
                "prompt_rate": metrics["prompt_rate"],
                "predicted_rate": metrics["predicted_rate"],
                "ttft_ms": 0.0,
                "ttft_ms_p95": 0.0,
                "rate_source": source,
            }
        if changed:
            self._persist(new_prompt, new_predicted)
        self._last = {"time": now, "predicted_total": predicted_total}

    def snapshot(self) -> dict:
        with self._lock:
            base = dict(self._snapshot)
        # vLLM 原生指标融进快照：rate 桶仅原生值为真时覆盖引擎/窗口值（窗口无流量时
        # 仍给出测速真值）；ttft 时间桶覆写（原生注入是唯一数据源；引擎/窗口均置 0）。
        native_row = self._compute_native_row()
        base["prompt_rate"] = native_row["prompt_rate"] or base.get("prompt_rate") or 0.0
        base["predicted_rate"] = native_row["predicted_rate"] or base.get("predicted_rate") or 0.0
        base["ttft_ms"] = native_row["ttft_ms"]
        base["ttft_ms_p95"] = native_row["ttft_ms_p95"]
        if (
            base.get("rate_source") == "none"
            and native_row["has_any"]
            and (
                native_row["prompt_rate"]
                or native_row["predicted_rate"]
                or native_row["ttft_ms"]
            )
        ):
            base["rate_source"] = "native"
        return base


class UsageHandler(BaseHTTPRequestHandler):
    """/api/usage 处理器，按 ?model= 从 targets 选择数据源。"""

    targets: list[StatsTarget] = []
    collectors: dict[str, UsageCollector] = {}
    start_time: float = time.time()

    def do_GET(self) -> None:  # noqa: N802 —— http.server 命名约定
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/api/usage":
            self.send_error(404)
            return
        model = self._query_param("model")
        status_code = 200
        if self._query_param("view") == "tier":
            payload = self._resolve_tier_payload(model)
            # tier 视图的错误对象无法被 Token Plan 模板正常渲染，改用非 2xx
            # 让 cc-switch 走"查询失败 + 重试入口"分支并保留最近一次成功值
            if isinstance(payload, dict):
                status_code = 503
        else:
            payload = self._resolve_payload(model)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_param(self, key: str) -> str | None:
        if "?" not in self.path:
            return None
        query = self.path.split("?", 1)[1]
        for part in query.split("&"):
            if part.startswith(f"{key}="):
                return part.split("=", 1)[1]
        return None

    def _resolve_payload(self, model: str | None) -> dict:
        if model == "all":
            return self._aggregate_payload()
        if model:
            target = next((t for t in self.targets if t.name == model or model in t.aliases), None)
            if target is None:
                return {"error": f"未知模型：{model}"}
        else:
            target = self.targets[0] if self.targets else None
            if target is None:
                return {"error": "无可用模型"}
        return self._build_target_payload(target)

    def _build_target_payload(self, target: StatsTarget) -> dict:
        if target.mapping is None:
            return {"error": "该引擎不支持精确统计"}
        collector = self.collectors.get(target.name)
        if collector is None:
            return {"error": "该引擎不支持精确统计"}
        snap = collector.get_snapshot()
        if not snap["ok"]:
            return {"isValid": False, "invalidMessage": f"{target.name} 不可用：{snap['error'] or '未知错误'}"}
        tokens = dict(snap)
        # 原生指标（vLLM per-request）已给出速率/TTFT 时无需兜底测速；
        # 仅当速率全为 0 且 bench_fallback 开关打开时才伪造请求测速，避免 cc-switch 一直显示 0。
        native_has_any = (
            (tokens.get("prompt_rate") or 0) > 0
            or (tokens.get("predicted_rate") or 0) > 0
            or (tokens.get("ttft_ms") or 0) > 0
        )
        bench_fallback_enabled = getattr(collector, "bench_fallback", True) is True
        should_bench = (
            not native_has_any
            and bench_fallback_enabled
            and (tokens.get("prompt_rate", 0) == 0 or tokens.get("predicted_rate", 0) == 0)
        )
        if should_bench:
            bench = _bench_cached(target)
            if bench is not None:
                if tokens.get("prompt_rate", 0.0) == 0:
                    tokens["prompt_rate"] = bench[0]
                if tokens.get("predicted_rate", 0.0) == 0:
                    tokens["predicted_rate"] = bench[1]
                if tokens.get("ttft_ms", 0.0) == 0:
                    tokens["ttft_ms"] = float(bench[2])
            tokens["rate_source"] = "bench"
        payload = build_usage_payload(tokens, target.usage_cfg, self.start_time, time.time())
        payload["model"] = target.name
        # 覆盖默认 planName，避免多模型时卡片展开视图显示错误名称
        payload["planName"] = f"{target.name} 本地部署"
        # TTFT / 速率来源仅在非零/非空时透传，避免 cc-switch 卡片展开视图出现 0 噪音
        if (tokens.get("ttft_ms") or 0) > 0:
            payload["ttft_ms"] = tokens["ttft_ms"]
        if (tokens.get("ttft_ms_p95") or 0) > 0:
            payload["ttft_ms_p95"] = tokens["ttft_ms_p95"]
        if tokens.get("rate_source"):
            payload["rate_source"] = tokens["rate_source"]
        return payload

    def _aggregate_payload(self) -> dict:
        targets = [t for t in self.targets if t.mapping is not None]
        if not targets:
            return {"error": "无支持精确统计的模型"}
        total_used = 0.0
        total_budget = 0.0
        has_budget = True
        all_valid = True
        prompt_rate_total = 0.0
        predicted_rate_total = 0.0
        prompt_total_total = 0.0
        predicted_total_total = 0.0
        extra_parts: list[str] = []
        invalid_messages: list[str] = []
        for target in targets:
            collector = self.collectors.get(target.name)
            if collector is None:
                continue
            snap = collector.get_snapshot()
            if not snap["ok"]:
                all_valid = False
                invalid_messages.append(f"{target.name}: {snap['error'] or '未知错误'}")
                continue
            used = round(
                calc_cost(
                    snap["prompt_total"],
                    snap["predicted_total"],
                    float(target.usage_cfg.get("price_in", 1.0)),
                    float(target.usage_cfg.get("price_out", 2.0)),
                ),
                2,
            )
            total_used += used
            budget_raw = target.usage_cfg.get("budget")
            if budget_raw is None:
                has_budget = False
            else:
                total_budget += float(budget_raw)
            prompt_rate_total += snap.get("prompt_rate", 0.0)
            predicted_rate_total += snap.get("predicted_rate", 0.0)
            prompt_total_total += snap["prompt_total"]
            predicted_total_total += snap["predicted_total"]
            extra_parts.append(
                f"{target.name}: 累计 {_fmt_tokens(snap['prompt_total'] + snap['predicted_total'])} toks, "
                f"输出 {snap.get('predicted_rate', 0.0):.1f} tok/s"
            )
        payload: dict[str, object] = {
            "isValid": all_valid,
            "used": round(total_used, 2),
            "unit": "CNY",
            "planName": "modelctl 聚合用量",
            "extra": "; ".join(extra_parts),
            "model": "all",
            "prompt_rate": prompt_rate_total,
            "predicted_rate": predicted_rate_total,
        }
        if not all_valid:
            payload["invalidMessage"] = "; ".join(invalid_messages)
        if has_budget:
            payload["total"] = round(total_budget, 2)
            payload["remaining"] = round(max(total_budget - total_used, 0.0), 2)
        else:
            payload["total"] = None
            payload["remaining"] = None
        return payload

    def _resolve_tier_payload(self, model: str | None) -> list[dict] | dict:
        """view=tier：返回 cc-switch Token Plan 模板的百分比徽章数组。

        每条对应一个已配置预算且可访问的 profile；used 为预算消耗百分比（0-100），
        cc-switch 按使用率渲染彩色徽章（<70% 绿 / 70–89% 橙 / ≥90% 红）。
        无可用数据时返回 {"error": ...}，由 extractor 映射为失效提示。
        """
        plan_label = "modelctl 本地部署"
        if model and model != "all":
            target = next((t for t in self.targets if t.name == model or model in t.aliases), None)
            if target is None:
                return {"error": f"未知模型：{model}"}
            collector = self.collectors.get(target.name)
            if collector is None:
                return {"error": "该引擎不支持精确统计"}
            if _budget_of(target.usage_cfg) is None:
                return {"error": "未配置预算：请在本 profile 的 usage.budget 设置预算（元）后重试"}
            snap = collector.get_snapshot()
            if not snap["ok"]:
                return {"error": f"{target.name} 不可用：{snap['error'] or '未知错误'}"}
            return [build_tier_item(target.name, snap, target.usage_cfg, plan_label)]

        items: list[dict] = []
        for target in self.targets:
            if target.mapping is None or target.name not in self.collectors:
                continue
            if _budget_of(target.usage_cfg) is None:
                continue
            snap = self.collectors[target.name].get_snapshot()
            if not snap["ok"]:
                continue
            items.append(build_tier_item(target.name, snap, target.usage_cfg, plan_label))
        if not items:
            return {"error": "无可用预算数据：请为至少一个模型配置 usage.budget（元）并确保服务运行中"}
        return items

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 —— 抑制默认请求日志
        pass


def run_server(targets: list[StatsTarget] | None = None) -> None:
    """启动用量统计 HTTP 服务（阻塞运行，Ctrl-C 退出）。

    环境变量：USAGE_HOST（默认 0.0.0.0）、USAGE_PORT（默认 5002）、
    USAGE_MODE（poll/on-demand）、USAGE_POLL_INTERVAL（默认 5）、
    USAGE_DATA_DIR（默认 <PROJECT_ROOT>/data/cache）。
    """
    load_env()  # 先加载 .env，确保 USAGE_DATA_DIR 等配置在 data_dir 计算前生效
    raw_data_dir = os.environ.get("USAGE_DATA_DIR", "")
    data_dir = Path(raw_data_dir) if raw_data_dir else PROJECT_ROOT / "data" / "cache"
    data_dir.mkdir(parents=True, exist_ok=True)
    if targets is None:
        targets = _targets_from_profiles(data_dir)

    host = os.environ.get("USAGE_HOST", "0.0.0.0")
    port = int(os.environ.get("USAGE_PORT", str(USAGE_PORT)))
    mode = os.environ.get("USAGE_MODE", "poll")
    poll_interval = float(os.environ.get("USAGE_POLL_INTERVAL", "5"))

    collectors: dict[str, UsageCollector] = {}
    for target in targets:
        if target.mapping is not None:
            # UsageCollector 的 base_url 语义是"指标服务根地址"（_poll_once 内部拼 /metrics），
            # 而 metrics_url 已含 /metrics 后缀，此处需去掉避免拼出 /metrics/metrics。
            collector = UsageCollector(
                target.name,
                target.metrics_url.removesuffix("/metrics"),
                poll_interval,
                target.api_key,
                target.data_dir,
                mode=mode,
                mapping=target.mapping,
                native_mapping=target.native_mapping,
                bench_fallback=_parse_env_bool(os.environ.get("USAGE_BENCH_FALLBACK")),
            )
            collector.start()
            collectors[target.name] = collector

    UsageHandler.targets = targets
    UsageHandler.collectors = collectors
    UsageHandler.start_time = time.time()

    server = ThreadingHTTPServer((host, port), UsageHandler)
    mode_desc = "由 cc-switch 轮询触发、每次请求同步拉取" if mode == "on-demand" else f"后台每 {poll_interval:g}s 轮询"
    print(
        f"cc-switch 用量统计服务运行于 http://{host}:{port}/api/usage（{mode_desc}，{len(targets)} 个模型）",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for collector in collectors.values():
            collector.stop()
        server.server_close()


def _targets_from_profiles(data_dir: Path) -> list[StatsTarget]:
    """从 models/*.yaml 构造统计目标（供独立运行 / Task 9 后台化）。"""
    from modelctl.core.capabilities import Capabilities
    from modelctl.core.profile import list_profiles
    from modelctl.engines import get_adapter

    targets: list[StatsTarget] = []
    for profile in list_profiles():
        # 统计服务仅调用 metrics_mapping()，无需真实硬件探测
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        try:
            native_mapping = adapter.native_metrics_mapping()
        except (NotImplementedError, AttributeError):
            native_mapping = None
        targets.append(
            StatsTarget(
                name=profile.name,
                data_dir=data_dir,
                metrics_url=f"http://127.0.0.1:{profile.port}/metrics",
                mapping=adapter.metrics_mapping(),
                usage_cfg=profile.usage,
                api_key=profile.api_key,
                aliases=profile.aliases,
                # 窗口无流量时主动测速兜底（复用与 cli 测速相同的请求构造）
                bench_url=f"http://127.0.0.1:{profile.port}/v1/chat/completions",
                bench_model=adapter.upstream_model_name(),
                native_mapping=native_mapping,
            )
        )
    return targets


def main() -> None:
    """独立运行入口：加载全部 profile 并启动统计服务。"""
    run_server()


if __name__ == "__main__":
    main()

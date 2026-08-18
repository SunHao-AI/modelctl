#!/usr/bin/env python3
"""core/stats.py — 用量统计服务（多引擎指标映射）。

迁移自 script/usage_stats_server.py：把单一 llama-server 数据源改为
targets 列表 + 按 ?model= 路由，指标名映射由各引擎适配器的
metrics_mapping() 提供（mapping 为 None 表示该引擎不支持精确统计）。

对外 /api/usage 输出字段与现版完全一致（cc-switch 无感），仅额外增加
"model" 字段标识数据源。

支持两种数据获取模式（USAGE_MODE）：
- poll（默认）：后台线程按 USAGE_POLL_INTERVAL 定时轮询 /metrics，
  /api/usage 返回最近一次缓存快照。
- on-demand：不启动后台线程，由 cc-switch 轮询触发，每次请求同步拉取。

纯标准库实现，零第三方依赖。独立运行：
    python -m modelctl.core.stats
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from modelctl.core.envfile import PROJECT_ROOT

USAGE_PORT = 5002


def _fmt_int(value: float) -> str:
    """千分位格式化整数。"""
    return f"{int(round(value)):,}"


def _build_patterns(mapping: dict[str, list[str]]) -> dict[str, list[re.Pattern]]:
    """按 mapping 构建预编译的指标匹配模式（避免每轮轮询重复编译正则）。

    匹配形如 "name 123" 或 "name{label=\"x\"} 123" 的 Prometheus 采样行。
    """
    return {
        key: [re.compile(r"^" + re.escape(name) + r"(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE) for name in names]
        for key, names in mapping.items()
    }


def parse_metrics(text: str, mapping: dict[str, list[str]]) -> dict[str, float]:
    """解析 Prometheus 文本，返回 {prompt_total, predicted_total, prompt_rate, predicted_rate}。

    按 mapping 各键的候选指标名取第一个命中；无命中返回 0.0。
    速率为 gauge 值（tok/s）。
    """
    result = {"prompt_total": 0.0, "predicted_total": 0.0, "prompt_rate": 0.0, "predicted_rate": 0.0}
    patterns = _build_patterns(mapping)
    for key, key_patterns in patterns.items():
        for pattern in key_patterns:
            m = pattern.search(text)
            if m:
                try:
                    result[key] = float(m.group(1))
                except ValueError:
                    pass
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
    used = round(calc_cost(prompt, predicted, price_in, price_out), 2)
    runtime = max(now - start_time, 0.0)
    payload = {
        "isValid": True,
        "used": used,
        "unit": "CNY",
        "planName": "DeepSeek-V4-Flash 本地部署",
        "extra": (
            f"累计 {_fmt_int(prompt + predicted)} tokens"
            f"（输入 {_fmt_int(prompt)} / 输出 {_fmt_int(predicted)}）"
            f"| 输入速率 {prompt_rate:.1f} tok/s"
            f"| 生成速率 {predicted_rate:.1f} tok/s"
            f"| 运行 {int(runtime // 3600)}h{int((runtime % 3600) // 60)}m"
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


@dataclass
class StatsTarget:
    """单个模型的用量统计目标。mapping 为 None 表示该引擎不支持精确统计。"""

    name: str
    data_dir: Path
    metrics_url: str
    mapping: dict[str, list[str]] | None
    usage_cfg: dict = field(default_factory=dict)
    api_key: str | None = None


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
    ) -> None:
        self.name = name
        self.data_dir = data_dir
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.api_key = api_key
        self.mode = mode
        self.mapping = mapping or {}
        self._lock = threading.Lock()
        self._snapshot: dict[str, object] = {
            "ok": False,
            "error": None,
            "prompt_total": 0.0,
            "predicted_total": 0.0,
            "prompt_rate": 0.0,
            "predicted_rate": 0.0,
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
            now = time.monotonic()
            new_prompt = max(metrics["prompt_total"], self._baseline["prompt_total"])
            new_predicted = max(metrics["predicted_total"], self._baseline["predicted_total"])
            changed = new_prompt != self._baseline["prompt_total"] or new_predicted != self._baseline["predicted_total"]
            self._baseline["prompt_total"] = new_prompt
            self._baseline["predicted_total"] = new_predicted

            # 滑动窗口回填速率：gauge > 0 用 gauge；gauge <= 0 时用窗口内累计差值计算速率
            self._record_window(now, new_prompt, new_predicted)
            if metrics["prompt_rate"] <= 0.0 or metrics["predicted_rate"] <= 0.0:
                prompt_rate, predicted_rate = self._compute_window_rate()
                if metrics["prompt_rate"] <= 0.0:
                    metrics["prompt_rate"] = prompt_rate
                if metrics["predicted_rate"] <= 0.0:
                    metrics["predicted_rate"] = predicted_rate

            with self._lock:
                self._snapshot = {
                    "ok": True,
                    "error": None,
                    "prompt_total": new_prompt,
                    "predicted_total": new_predicted,
                    "prompt_rate": metrics["prompt_rate"],
                    "predicted_rate": metrics["predicted_rate"],
                }
            if changed:
                self._persist(new_prompt, new_predicted)
            self._last = {"time": now, "predicted_total": metrics["predicted_total"]}
        except Exception as error:  # noqa: BLE001 —— 轮询失败仅记录，不中断服务
            with self._lock:
                self._snapshot = {
                    "ok": False,
                    "error": str(error),
                    "prompt_total": 0.0,
                    "predicted_total": 0.0,
                    "prompt_rate": 0.0,
                    "predicted_rate": 0.0,
                }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)


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
        payload = self._resolve_payload(model)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
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
            target = next((t for t in self.targets if t.name == model), None)
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
        payload = build_usage_payload(snap, target.usage_cfg, self.start_time, time.time())
        payload["model"] = target.name
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
            used = round(calc_cost(snap["prompt_total"], snap["predicted_total"], float(target.usage_cfg.get("price_in", 1.0)), float(target.usage_cfg.get("price_out", 2.0))), 2)
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
                f"{target.name}: 累计 {_fmt_int(snap['prompt_total'] + snap['predicted_total'])} tokens, "
                f"生成 {snap.get('predicted_rate', 0.0):.1f} tok/s"
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

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 —— 抑制默认请求日志
        pass


def run_server(targets: list[StatsTarget] | None = None) -> None:
    """启动用量统计 HTTP 服务（阻塞运行，Ctrl-C 退出）。

    环境变量：USAGE_HOST（默认 0.0.0.0）、USAGE_PORT（默认 5002）、
    USAGE_MODE（poll/on-demand）、USAGE_POLL_INTERVAL（默认 5）、
    USAGE_DATA_DIR（默认 <PROJECT_ROOT>/data/cache）。
    """
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
            collector = UsageCollector(
                target.name,
                target.metrics_url,
                poll_interval,
                target.api_key,
                target.data_dir,
                mode=mode,
                mapping=target.mapping,
            )
            collector.start()
            collectors[target.name] = collector

    UsageHandler.targets = targets
    UsageHandler.collectors = collectors
    UsageHandler.start_time = time.time()

    server = ThreadingHTTPServer((host, port), UsageHandler)
    mode_desc = "由 cc-switch 轮询触发、每次请求同步拉取" if mode == "on-demand" else f"后台每 {poll_interval:g}s 轮询"
    print(
        f"cc-switch 用量统计服务运行于 http://{host}:{port}/api/usage（{mode_desc}，{len(targets)} 个模型）", flush=True
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
    from modelctl.core.envfile import load_env
    from modelctl.core.profile import list_profiles
    from modelctl.engines import get_adapter

    load_env()
    targets: list[StatsTarget] = []
    for profile in list_profiles():
        # 统计服务仅调用 metrics_mapping()，无需真实硬件探测
        adapter = get_adapter(profile.engine)(profile, Capabilities())
        targets.append(
            StatsTarget(
                name=profile.name,
                data_dir=data_dir,
                metrics_url=f"http://127.0.0.1:{profile.port}/metrics",
                mapping=adapter.metrics_mapping(),
                usage_cfg=profile.usage,
                api_key=profile.api_key,
            )
        )
    return targets


def main() -> None:
    """独立运行入口：加载全部 profile 并启动统计服务。"""
    run_server()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/vllm.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : vLLM 引擎适配器
# ===============================================================================

"""engines/vllm.py — vLLM 适配器。"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from loguru import logger

from modelctl.core import docker_setup, envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.core.process import docker_container_alive, wait_health
from modelctl.engines._download import download_repo
from modelctl.engines.base import EngineAdapter, RequirementError

# per-request metrics flag 所需最低 vLLM 版本（2026-08 实测值；>= 该版本 --enable-per-request-metrics 可用）
MIN_VLLM_PER_REQUEST = (0, 13, 0)

#: docker 缓存卷：volume 名后缀 → 容器内目录（`vllm/vllm-openai` 以 root 运行，HOME=/root）。
#: cache 卷覆盖 torch.compile 产物（`VLLM_CACHE_ROOT/torch_compile_cache`）与 HF assets
#: （`VLLM_ASSETS_CACHE` 不随 `VLLM_CACHE_ROOT` 联动，故必须挂父目录而非靠 env 重定向）；
#: triton 卷覆盖内核编译缓存（默认 `~/.triton/cache`）——这两项是本环境最慢的一段。
_ENGINE_CACHE_DIRS: dict[str, str] = {
    "cache": "/root/.cache/vllm",
    "triton": "/root/.triton",
}

#: docker named volume 名字符集（Docker 要求 `[a-zA-Z0-9][a-zA-Z0-9_.-]*`）。
_VOL_NAME_BAD = re.compile(r"[^a-zA-Z0-9_.-]")


def _cache_volume_name(profile_name: str) -> str:
    """profile.name → 合法 volume 名片段（非法字符转 `-`，去首尾连接符）。

    volume 名不允许以 `.`/`-` 开头，而 profile.name 理论上可含奇异字符；不 sanitize 会让
    `docker run` 以 "volume name is invalid" 直接失败，容器根本起不来。
    """
    cleaned = _VOL_NAME_BAD.sub("-", (profile_name or "").strip())
    return cleaned.strip("-.") or "default"


class VllmAdapter(EngineAdapter):
    def check_requirements(self, *, readonly: bool = False) -> None:
        cfg = self.profile.engine_config
        runtime, _image, dual_error = self._resolve_runtime()
        if dual_error:
            raise RequirementError(dual_error)
        if runtime == "docker":
            container_name = self._container_name
            # docker / nvidia-smi 都在 PATH；硬拦截不降级（检查与指引统一在 core.docker_setup）
            missing = docker_setup.path_level_missing()
            if missing:
                raise RequirementError(f"docker_image 已配置但 Docker 环境未就绪：{'；'.join(missing)}——{docker_setup.MSG_GUIDE}")
            # 清冲突残留容器（幂等；失败仅 warning + 解码 stderr，不再静默吞）
            # readonly（TUI 预检渲染）：浏览界面不得删容器
            if not readonly:
                from modelctl.core.process import clear_stale_docker_container
                clear_stale_docker_container(self.profile.name, container_name)
            # model 必填
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）")
        else:
            # 现状路径：venv 检查、model 检查
            envs.ensure_env("vllm")
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）")
            # per-request 版本门控（仅开启任一 flag 时才探测；docker 路径跳过——版本真相在 docker 镜像 tag）
            if cfg.get("enable_per_request_metrics") or cfg.get("enable_force_include_usage"):
                min_v = ".".join(map(str, MIN_VLLM_PER_REQUEST))
                v = envs.vllm_version()  # 经模块属性访问，test 可 monkeypatch 该属性
                if v is None:
                    # 带上安装目录：探测失败时（多为 import 超时）便于人工进 venv 直接核对版本
                    venv_dir = envs.VENV_ROOT / "vllm"
                    py = envs.engine_python("vllm")
                    logger.warning(
                        f"无法探测 vLLM 版本（将放行；若启动报错请人工确认 ≥ {min_v}）。"
                        f"安装目录：{venv_dir}，可手动核对："
                        f"`{envs.engine_bin('vllm', 'vllm')} --version` 或 "
                        f'`{py} -c "import vllm; print(vllm.__version__)"`'
                    )
                elif v < MIN_VLLM_PER_REQUEST:
                    raise RequirementError(f"enable_per_request_metrics 需 vLLM ≥ {min_v}，" f"当前 {v[0]}.{v[1]}.{v[2]}；" "可升级（uv sync --project envs/vllm --upgrade vllm）或在 yaml 中关闭该项")
        # 共享部分：GPU / TP / VRAM / compat / gpu lock
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}，二者必须一致")
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数 {self.caps.gpu_count}")
        self._check_vram_advisory(cfg, gpus)
        # per-request 双 flag 告警（venv / docker 两条运行路径均覆盖；仅写告警、不硬拦截）
        per_request_on = bool(cfg.get("enable_per_request_metrics"))
        force_on = bool(cfg.get("enable_force_include_usage"))
        if per_request_on and not force_on:
            self.warnings.append(f"{self.profile.name}：enable_per_request_metrics=true 但 enable_force_include_usage=false，" "流式中间块缺 usage 会使 stats.record_tokens 仅末块入账；建议同时开启")
        self.run_compat_checks()  # 预检：软件规则 + 模型 id 特征
        if gpus is not None and not readonly:
            acquire_gpu_lock(self.profile.name, gpus)

    def _check_vram_advisory(self, cfg: dict, gpus: list[int] | None) -> None:
        """HF 权重粗估（spec §2.1）：权重大小超可用显存上限时仅告警、不硬拦截。

        上限按 总显存 × gpu_memory_utilization 估算；未计 KV cache/激活，
        HF 权重加载行为复杂，故不做硬性 block（vllm 自身启动时会 OOM 报错）。
        """
        self._check_weights_advisory(
            str(cfg.get("model") or ""),
            gpus,
            self.caps,
            float(cfg.get("gpu_memory_utilization", 0.9)),
            "gpu_memory_utilization",
            self.profile.engine,
            self.warnings,
        )

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        # docker 路径先确保镜像就位：21.8GB 级 Day-0 镜像跨境拉取极易中途 EOF，
        # 交给 `docker run` 隐式 pull 会让失败原因消失在 launch 日志里。
        runtime, image, dual_error = self._resolve_runtime()
        if dual_error:
            # pre_start 在 check_requirements 之后被调用，正常路径下 dual_error 通常为 None；
            # 兜底以防外部调用链（如 rebuild）绕开 check_requirements 直接进 pre_start。
            raise RequirementError(dual_error)
        if runtime == "docker" and not docker_setup.ensure_image(image, on_progress=self._progress_cb):
            raise RequirementError(
                f"{self.profile.name}：镜像 {image} 未就位，无法启动容器；"
                "详见日志中的 docker pull 错误分类与对应处置"
            )
        model = str(cfg.get("model") or "")
        if not (model and (Path(model).expanduser().is_dir() or Path(model).expanduser().is_file())):
            if cfg.get("download"):
                modelscope_id = cfg["download"]["modelscope_id"]
                model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
                # 落地路径由 MODEL_ROOT + modelscope_id 确定性推导，目录已存在即复用；
                # 仅更新内存中的 cfg，不写回 YAML（保持 profile 文件干净、多机可移植）。
                local_dir = download_repo(modelscope_id, model_root)
                cfg["model"] = str(local_dir.resolve())
        # 精检：模型文件就位后，以 config.json 判定更精确的模型特征
        # 延迟导入 ModelSpec，避免 compat 部分初始化时经 engines/__init__ 回环
        from modelctl.core.compat import ModelSpec

        local = Path(str(cfg.get("model") or "")).expanduser()
        if local.is_dir():
            self.run_compat_checks(ModelSpec.from_local(self.profile.engine, local))

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image, dual_error = self._resolve_runtime()
        if dual_error:
            raise RequirementError(dual_error)

        # 安全加固：默认仅绑定 loopback，杜绝外部直连引擎端口绕过网关鉴权/限额/审计。
        # 确需对外暴露时显式配置 bind_host: 0.0.0.0（并配合防火墙/白名单限制来源）。
        bind_host = str(cfg.get("bind_host", "127.0.0.1"))
        # 共用：--served-model-name 之后的 model_args
        # （原 "--host", "0.0.0.0" 已从共享段移除：venv 分支在命令末尾追加权威
        #   --host {bind_host}，docker 分支容器固定 --host 0.0.0.0、宿主机 -p 绑定 bind_host）
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model_args = [
            "--served-model-name",
            self.upstream_model_name(),
            "--tensor-parallel-size",
            str(tp),
            "--gpu-memory-utilization",
            str(cfg.get("gpu_memory_utilization", 0.9)),
            "--disable-uvicorn-access-log",
        ]
        if cfg.get("max_model_len"):
            model_args += ["--max-model-len", str(cfg["max_model_len"])]
        if cfg.get("quantization"):
            model_args += ["--quantization", str(cfg["quantization"])]
        if cfg.get("kv_cache_dtype"):
            model_args += ["--kv-cache-dtype", str(cfg["kv_cache_dtype"])]
        # per-request metrics：两 flag 独立（任一为 True 则追加），未配置时 model_args 与改造前一致
        if cfg.get("enable_per_request_metrics"):
            model_args.append("--enable-per-request-metrics")
        if cfg.get("enable_force_include_usage"):
            model_args.append("--enable-force-include-usage")
        # 审计关联（默认开）：网关 JSONL 审计靠这两个 flag 才能与上游对上号——
        #   request-id-headers → 响应头 X-Request-Id，写进审计 upstream_request_id；
        #   prompt-tokens-details → usage.prompt_tokens_details.cached_tokens，写进审计 cached_tokens
        #   （prefix cache 命中量，判断"重复上下文是否真省了算力"的唯一依据）。
        # 两者只增输出、不改推理行为，故默认 True；异常场景可显式置 false 关闭。
        if cfg.get("enable_request_id_headers", True):
            model_args.append("--enable-request-id-headers")
        if cfg.get("enable_prompt_tokens_details", True):
            model_args.append("--enable-prompt-tokens-details")
        # 运维安全类（默认关/不设，按 profile 需要显式打开）
        if cfg.get("shutdown_timeout") is not None:
            # 0 = 立即中止，>0 = 等待在途请求结束。venv 分支走 SIGTERM，此值决定
            # 正在生成的长回复是否会被强杀；docker 分支仍由 `docker rm -f` 兜底。
            model_args += ["--shutdown-timeout", str(cfg["shutdown_timeout"])]
        if cfg.get("disable_access_log_for_endpoints"):
            # 逗号分隔路径列表，压制 /health、/metrics 这类高频轮询的访问日志噪音。
            # 本项目健康检查每秒探一次，不设则该引擎日志几乎只剩健康检查行。
            ep = cfg["disable_access_log_for_endpoints"]
            ep = ",".join(str(x).strip() for x in ep) if isinstance(ep, (list, tuple)) else str(ep).strip()
            if ep:
                model_args += ["--disable-access-log-for-endpoints", ep]
        if cfg.get("disable_fastapi_docs"):
            # 关闭 /docs、/redoc、/openapi.json，减少对外暴露的接口面（引擎端口虽默认
            # 只绑 loopback，但 bind_host: 0.0.0.0 的部署应一并关掉文档页）。
            model_args.append("--disable-fastapi-docs")
        if cfg.get("root_path"):
            # 引擎经 nginx 按路径前缀转发时必须设置，否则 OpenAPI/文档里的回调地址、
            # 以及部分 SDK 拼出的 URL 会丢掉前缀。
            model_args += ["--root-path", str(cfg["root_path"])]
        # api_key / extra_args 恒定追加到末尾：extra_args 里的同名参数需能覆盖上面的默认值
        tail = self.api_key_args() + extra

        if runtime == "venv":
            # 必须显式传 --port：不传时 `vllm serve` 回退默认 8000，既与 profile.port 不一致
            # （健康检查探 127.0.0.1:{profile.port}/health 必然 Connection refused），
            # 又和其他未指定端口的实例撞 8000（OSError: [Errno 98] Address already in use）。
            # 置于 tail 之前，保证 extra_args 显式指定的 --port 仍能覆盖。
            cmd = [
                str(envs.engine_bin("vllm", "vllm")),
                "serve",
                str(cfg["model"]),
                *model_args,
                "--port",
                str(self.profile.port),
                *tail,
                # --host 置于 extra 之后，保证 bind_host 权威、不被 extra_args 里的
                # 同名 --host 覆盖回 0.0.0.0（安全值不应被临时参数静默回退）
                "--host",
                bind_host,
            ]
            return cmd, self._venv_env(gpus)

        # docker 分支
        model_raw = str(cfg.get("model") or "").strip()
        model_local = Path(model_raw).expanduser()
        if not model_local.is_absolute() or not model_local.is_dir():
            raise RequirementError(f"{self.profile.name}：docker_image 路径下 model 必须为本地绝对路径" f"且目录已存在（当前: {model_raw}——HF id 需先 modelctl start 触发 pre_start 下载）")
        model_local = model_local.resolve()
        cmd = (
            [
                "docker",
                "run",
                "--name",
                self._container_name,
                "--gpus",
                self._gpus_json(),
                # -p 绑定 {bind_host}:{port}:8000：宿主机 docker-proxy 仅监听 127.0.0.1，
                # 容器内 vLLM 仍绑 0.0.0.0（保持 --host 不动，避免转发边界问题），
                # 外部无法连接宿主机该端口，彻底杜绝绕过网关直连。
                "-p",
                f"{bind_host}:{self.profile.port}:8000",
                "-v",
                f"{model_local.parent.as_posix()}:/models:ro",
                "--ipc=host",
                "--detach",
                # 镜像 ENTRYPOINT 是 ["vllm", "serve"]（vllm/vllm-openai Day-0 镜像约定），
                # CMD 仅传位置参数（模型路径）+ 命名参数。重复 "serve" 会被拼成
                # `vllm serve serve /models/...` 触发 argparse "unrecognized arguments" 退出 (exit code 2)
                image,
                f"/models/{model_local.name}",
            ]
            + model_args
            + tail
            # 容器内 vLLM 固定绑 0.0.0.0（置于 extra 之后，防 extra_args 用 --host 改动）；
            # 外部可达性由宿主机 -p 绑定 {bind_host}:{port}:8000 隔离（默认仅 127.0.0.1）
            + ["--port", "8000", "--host", "0.0.0.0"]
        )
        # docker_env：yaml vllm.docker_env（dict）→ 容器内环境变量。
        # ⚠ 必须用 `docker run -e`（而非 build_command 返回的 env dict）才能进入容器：
        #   start_detached 只把 env 注入 docker CLI 宿主进程（Popen），不会透传进容器。
        # TZ 同理，必须显式带上，否则容器内 vLLM 日志是 UTC。
        env_args = self.docker_timezone_args()
        wsl_pin = self._wsl2_pin_memory_args()
        if wsl_pin:
            env_args += wsl_pin
        for k, v in (cfg.get("docker_env") or {}).items():
            env_args += ["-e", f"{k}={v}"]
        # 缓存卷（named volume）持久化编译/assets 缓存，加速二次启动。与 env_args 同段插在
        # --ipc=host 之前（docker 要求所有选项先于镜像名）。
        insert = env_args + self._cache_volume_args()
        ipc_idx = cmd.index("--ipc=host")
        cmd[ipc_idx:ipc_idx] = insert
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        return cmd, env

    def native_metrics_mapping(self) -> dict[str, str]:
        """vLLM per-request 原生指标字段映射（vLLM 0.13+ 对应双 flag：--enable-per-request-metrics
        与 --enable-force-include-usage，后者使流式中间块携带 usage 字段）。

        键固定 5 项 → vLLM OpenAI 兼容响应根级 "metrics"/SSE 末块真实字段名。
        """
        return {
            "rate": "tokens_per_second",
            "ttft_ms": "time_to_first_token_ms",
            "gen_time_ms": "generation_time_ms",
            "prompt_tokens": "num_prompt_tokens",
            "completion_tokens": "num_generation_tokens",
        }

    def metrics_mapping(self) -> dict[str, list[str]]:
        """vLLM /metrics 指标名 → stats 内部键的映射。

        核心五键（prompt_total / predicted_total / prompt_rate / predicted_rate /
        ttft_ms）由 stats 直接渲染；其余为**扩展键**，stats 按原键名透传到快照，
        仅在 vLLM 暴露该指标时出现（老版本/未开 --enable-metrics 时为 0）。

        候选名列表首个命中即生效，故老版本 vLLM 的旧指标名（gpu_cache_usage_perc、
        num_preemptions）作为兜底放在后面。
        """
        return {
            "prompt_total": ["vllm:prompt_tokens_total"],
            "predicted_total": ["vllm:generation_tokens_total"],
            # 实时速率 gauge：vLLM 自带（内部滑动窗口），客户端直连模型端口（绕过网关）时
            # 也能统计到真实吞吐；缺失/为 0 时 stats 退化为窗口差分
            "prompt_rate": [
                "vllm:prompt_tokens_seconds",
                "vllm:avg_prompt_throughput_toks_per_sec",
            ],
            "predicted_rate": [
                "vllm:generation_tokens_seconds",
                "vllm:avg_generation_throughput_toks_per_sec",
            ],
            # 首 Token 耗时：Histogram，无现成均值 gauge；stats.parse_metrics 以
            # sum/count 取均值并按 _ms 键约定换算成毫秒
            "ttft_ms": ["vllm:time_to_first_token_seconds"],
            # ---- 调度与容量（Gauge）----
            # 运行中 / 等待中的请求数：waiting 持续 >0 说明已过饱和，是排队深度的直接信号
            "running": ["vllm:num_requests_running"],
            "waiting": ["vllm:num_requests_waiting"],
            # KV cache 占用：0–1 的分数（不是百分数），>0.9 即接近打满、随时可能抢占
            "kv_cache_usage": ["vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"],
            # 抢占次数：Counter，显存不足导致请求被退回重算 prefill，非 0 即值得警觉
            "preemptions_total": ["vllm:num_preemptions_total", "vllm:num_preemptions"],
            # ---- Prefix cache（Counter；命中率 gauge 上游已弃用，由 hits/queries 派生）----
            # 查询/命中的 token 数；--enable-prefix-caching 未开时两者恒为 0
            "prefix_cache_queries": ["vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries"],
            "prefix_cache_hits": ["vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits"],
            # ---- 分段耗时（Histogram，均值 = sum/count，stats 换算为毫秒）----
            # 端到端延迟含排队；queue_ms 单拎出来用于区分"慢在算"还是"慢在等"
            "e2e_ms": ["vllm:e2e_request_latency_seconds"],
            "queue_ms": ["vllm:request_queue_time_seconds"],
            "prefill_ms": ["vllm:request_prefill_time_seconds"],
            "decode_ms": ["vllm:request_decode_time_seconds"],
        }

    def upstream_model_name(self) -> str:
        """vLLM 对外暴露的 served 模型名 = profile.name。

        与 build_command 的 --served-model-name、modelctl list 标识符一致：
        无论经网关转发还是直连 vLLM 端口，请求体 model 都用 profile.name（如 qwen3.8-vllm）。
        """
        return self.profile.name

    def _resolve_runtime(self) -> tuple[str, str | None, str | None]:
        """runtime 分流：yaml 显式声明 docker_image 时容器优先（尊重用户意图），
        venv 仅作为未声明镜像时的兜底路径，避免已装好 venv 的部署机把 yaml
        作者期望"必须容器"的模型（如 Day-0 架构镜像）静默切到 PyPI 版本再炸。

        返回三元组 ``(runtime, image, dual_error)``：
        - ``runtime``：``'venv' | 'docker'``；dual_error 非空时为 ``'venv'``（占位，调用方应先抛错）
        - ``image``：docker 分支填镜像名，venv 分支填 None；dual_error 非空时为 None
        - ``dual_error``：仅 "两条路径都不可用" 时非空；调用方 ``check_requirements`` 里
          应直接 `raise RequirementError(dual_error)`，避免静默落入 venv 分支后由
          ``ensure_env`` 抛出"环境未创建"这种**只暴露 venv 缺口**、丢失"docker 兜底
          也没配"信息的误导性错误。

        分流规则（2026-09 反转：docker 优先）：
        1. yaml ``docker_image`` 非空 → ``('docker', image, None)``
           —— 容器是模型作者的权威声明（如 Day-0 架构、官方 FP8 镜像），
           即使本机有可用 venv（``engine_native_usable`` 返回 True）仍走 docker，
           不静默用 PyPI 版本去 load 一个架构不支持的模型
        2. 上述不成立 且 ``envs.engine_native_usable('vllm')`` 为 True（Linux + .venvs/vllm 已建）
           → ``('venv', None, None)`` —— venv 兜底，普通标准架构模型走此路径
        3. 两条路径都不可用 → ``('venv', None, <friendly_msg>)``，文案按 platform_supports 区分：
           Windows / macOS 等非 Linux 平台 → 引导 docker_image（venv 不可用）
           Linux → 引导 `modelctl env setup vllm`，或配 docker_image 走容器
        """
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        if image:
            return ("docker", image, None)
        if envs.engine_native_usable("vllm"):
            return ("venv", None, None)
        # 两条路径都不可用 —— 文案按 platform_supports 区分
        if envs.platform_supports("vllm"):
            # Linux 但 venv 没建
            dual = (
                f"{self.profile.name}：vLLM 的托管 venv 未创建（当前平台 Linux 支持 venv），"
                "且 yaml vllm.docker_image 未配置（docker 主路径也未启用）。请先执行 "
                "`modelctl env setup vllm` 建立 venv，或在 yaml 的 vllm: 块下配置 "
                "`docker_image: vllm/vllm-openai:<tag>` 走 docker 运行时"
            )
        else:
            # 非 Linux（Windows 等）
            dual = (
                f"{self.profile.name}：vLLM 的托管 venv 仅支持 Linux 部署机，当前平台不支持；"
                "且 yaml vllm.docker_image 未配置（docker 主路径也未启用）。请在 yaml 的 "
                "vllm: 块下配置 `docker_image: vllm/vllm-openai:<tag>` 走 docker 运行时"
                "（镜像需已 pull；model 必须是本地目录）"
            )
        return ("venv", None, dual)

    @property
    def _container_name(self) -> str:
        """docker 容器名：profile.name 已以 ``-vllm`` 结尾时不再追加（避免双后缀）。

        典型 profile.name（{group}-{engine} 推导）如 ``qwen2.5-0.5b-vllm`` 已含 engine
        短名，旧实现 ``f"{profile.name}-vllm"`` 会拼出 ``qwen2.5-0.5b-vllm-vllm``——
        与 CLI/CLI 日志/nginx 路由里的 profile.name 不一致。本实现幂等：以
        ``-vllm`` 结尾时直接用 profile.name，否则追加 ``-vllm``。
        """
        if self.profile.name.endswith("-vllm"):
            return self.profile.name
        return f"{self.profile.name}-vllm"

    def _gpus_json(self) -> str:
        """返回 docker --gpus 参数值（带引号的 JSON 格式）。"""
        gpus = self.selected_gpus()
        if gpus:
            seq = list(gpus)
        else:
            tp = int(self.profile.engine_config.get("tensor_parallel_size", 1))
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    def _wsl2_pin_memory_args(self) -> list[str]:
        """WSL2 上的 vLLM 必须开 pinned memory，否则 v1 引擎硬失败退出。

        vLLM 在 WSL2 默认 `VLLM_WSL2_ENABLE_PIN_MEMORY=0`，使 `is_uva_available()`
        返回 False，而 v1 引擎 `RequestState` 用 UVA 张量存 all_token_ids，直接抛
        `RuntimeError: UVA is not available` → EngineCore 起不来、容器 exit 1
        （实测 vllm/vllm-openai:latest + WSL2 内核 6.18 复现）。

        因此探测到 daemon 跑在 WSL2 内核时自动注入 `-e VLLM_WSL2_ENABLE_PIN_MEMORY=1`。
        放在 yaml docker_env 之前，用户仍可显式覆盖。探测结果在 `docker_setup` 内缓存，
        避免每次构建命令都多跑一次 `docker info`。
        """
        return ["-e", "VLLM_WSL2_ENABLE_PIN_MEMORY=1"] if docker_setup.daemon_is_wsl2() else []

    def _cache_volume_args(self) -> list[str]:
        """docker 分支的引擎缓存卷挂载参数（named volume，加速二次启动）。

        vLLM 启动会往 `~/.cache/vllm`（编译产物、assets）、`~/.config/vllm`（config/
        tokenizer）、`~/.triton`（Triton 内核缓存）写缓存。`stop` 走 `docker rm -f`
        销毁容器可写层，这些缓存每次全丢，下次启动重新生成（torch/Triton 编译可达分钟级）。
        把三个目录挂成 **named volume**（存于 Docker Desktop 的 WSL2 ext4 虚拟磁盘），
        跨容器生命周期持久化，二次启动直接复用。

        为什么 named volume 而非 bind mount：Windows 宿主路径 bind 进 Linux 容器走
        9p/virtiofs，编译缓存这种小文件随机读写会慢到可能反超"不挂缓存"；named volume
        落在 ext4 里 IO 接近原生。volume 名带容器名（引擎后缀，与 `docker ps` 一致），
        多 profile / 多引擎互不串台。

        直接挂镜像内真实默认目录（`vllm/vllm-openai` 以 root 运行，HOME=/root），不用
        env 改路径：实测 `VLLM_ASSETS_CACHE` 不随 `VLLM_CACHE_ROOT` 联动（仍写
        `/root/.cache/vllm/assets`），靠 env 重定向会漏掉 assets 子目录；直接挂父目录
        `/root/.cache/vllm` 则天然覆盖 assets，也更少出错的环节。空 named volume 首次
        挂载时 Docker 会把镜像内该目录既有内容拷进卷，不丢镜像预置文件。
        """
        vol = _cache_volume_name(self._container_name)
        args: list[str] = []
        for sub, container_dir in _ENGINE_CACHE_DIRS.items():
            args += ["-v", f"modelctl-{vol}-{sub}:{container_dir}"]
        return args

    def _venv_env(self, gpus: list[int] | None) -> dict[str, str]:
        """现状 env 注入段（HF_HOME / VIRTUAL_ENV / PATH）抽取为独立方法。"""
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "vllm")
        env["PATH"] = str(envs.engine_bin("vllm", "vllm").parent) + os.pathsep + os.environ.get("PATH", os.environ["PATH"])
        return env

    def wait_ready(self, timeout: float) -> bool:
        """docker 分支：`docker run` 客户端 daemonize 后立刻退出，容器在 daemon 后台持续运行。

        与 venv 路径不同——客户端早退(1 秒内 poll() != None)是**预期行为**而非异常，
        不能用客户端进程做 alive_check（否则 600s 超时被 1 秒中断，roll 不出权重加载进度）。
        改用容器状态探针：容器死亡（OOM / GPU 不够 / 架构不识别等）时立即中止健康检查，
        不再空转到超时；容器存活（仍在加载权重）则继续等待直至就绪或超时。
        """
        if self._resolve_runtime()[0] == "docker":
            return wait_health(
                self.health_url(),
                timeout,
                self.upstream_api_key(),
                alive_check=lambda: docker_container_alive(self._container_name),
            )
        # venv 分支：保持现状行为（本工具拉起的 venv 进程早退 → 中止健康检查）
        return super().wait_ready(timeout)

    def backend_dead(self) -> bool:
        """docker 分支：后端死亡 = 容器已退出/不存在（客户端进程早退不等于容器死亡）。"""
        if self._resolve_runtime()[0] == "docker":
            return not docker_container_alive(self._container_name)
        return super().backend_dead()

    def stop_patterns(self) -> list[str]:
        # 用启动命令特征而非引擎短名：`modelctl stop qwen3.8-vllm` 自身命令行
        # 含 "vllm"，若 pkill -f "vllm" 会误杀 modelctl 进程（shell 打印 Terminated）
        if self._resolve_runtime()[0] != "docker":
            return ["vllm serve"]
        # docker 分支：Popen cmdline 为空格连接，模式 1 是其中连续子串，精准命中
        name = self._container_name
        gpus_json = self._gpus_json()
        model_raw = str(self.profile.engine_config.get("model") or "").strip()
        root = Path(model_raw).expanduser().resolve().parent.as_posix() if model_raw else "/"
        return [
            f"docker run --name {name} --gpus {gpus_json}",
            f"-v {root}:/models:ro",
        ]

    def is_docker_runtime(self) -> bool:
        """vllm 路径判定：docker_image 字段非空时走 docker runtime。"""
        return self._resolve_runtime()[0] == "docker"

    def log_tee_cmd(self) -> list[str] | None:
        """docker 分支：`docker logs -f --tail all <container>` 续写容器输出。

        用 `--tail all` 而非 `--tail 0`：`docker run --detach` 秒返回，只跟新行会漏掉
        tee 挂上前数百毫秒内的 banner 行，伤及 loading 段模式表匹配。launch log 此
        前只有一行容器 ID，全量重放无副作用；重复行由 watcher 的 pct 单调不减兜住。
        """
        if self._resolve_runtime()[0] != "docker":
            return None
        return ["docker", "logs", "-f", "--tail", "all", self._container_name]

    def log_fallback_cmd(self) -> list[str] | None:
        """docker 分支：`docker logs --tail 50 <container>`，启动失败摘录兜底（设计 §4.4）。"""
        if self._resolve_runtime()[0] != "docker":
            return None
        return ["docker", "logs", "--tail", "50", self._container_name]

    def stop_backend(self) -> None:
        """docker 分支：docker rm -f <container>（清 PID 防御 venv/docker 环境切换残留）；
        venv 分支：基类 stop_instance（pkill 兜底）。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance

            stop_docker_instance(self.profile.name, self._container_name)
        else:
            super().stop_backend()

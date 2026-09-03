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
import shlex
import shutil
import subprocess
from pathlib import Path

from loguru import logger

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.core.process import docker_container_alive, wait_health
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError

# per-request metrics flag 所需最低 vLLM 版本（2026-08 实测值；>= 该版本 --enable-per-request-metrics 可用）
MIN_VLLM_PER_REQUEST = (0, 13, 0)


class VllmAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        cfg = self.profile.engine_config
        runtime, _image = self._resolve_runtime()
        if runtime == "docker":
            container_name = f"{self.profile.name}-vllm"
            # docker / nvidia-smi 都在 PATH；硬拦截不降级
            if shutil.which("docker") is None:
                raise RequirementError(
                    "docker 命令不在 PATH——docker_image 已配置，请先安装 docker "
                    "（参考 `apt install docker.io docker-compose` 或官网）"
                )
            if shutil.which("nvidia-smi") is None:
                raise RequirementError(
                    "nvidia-smi 不在 PATH / nvidia-container-toolkit 未就绪——"
                    "docker 方式下 --gpus 设定需要 toolkit 支持，"
                    "请先安装 nvidia-container-toolkit"
                )
            # 清冲突残留容器（幂等）
            try:
                subprocess.run(["docker", "rm", "-f", container_name],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
            # model 必填
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(
                    f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）"
                )
        else:
            # 现状路径：venv 检查、model 检查
            envs.ensure_env("vllm")
            if not cfg.get("model") and not cfg.get("download"):
                raise RequirementError(f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）")
            # per-request 版本门控（仅开启任一 flag 时才探测；docker 路径跳过——版本真相在 docker 镜像 tag）
            if cfg.get("enable_per_request_metrics") or cfg.get("enable_force_include_usage"):
                v = envs.vllm_version()  # 经模块属性访问，test 可 monkeypatch 该属性
                if v is None:
                    logger.warning("无法探测 vLLM 版本（将放行；若启动报错请人工确认 ≥ 0.13.0）")
                elif v < MIN_VLLM_PER_REQUEST:
                    raise RequirementError(
                        f"enable_per_request_metrics 需 vLLM ≥ {'.'.join(map(str, MIN_VLLM_PER_REQUEST))}，"
                        f"当前 {v[0]}.{v[1]}.{v[2]}；"
                        "可升级（uv sync --project envs/vllm --upgrade vllm）或在 yaml 中关闭该项"
                    )
        # 共享部分：GPU / TP / VRAM / compat / gpu lock
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}，二者必须一致"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数 {self.caps.gpu_count}")
        self._check_vram_advisory(cfg, gpus)
        # per-request 双 flag 告警（venv / docker 两条运行路径均覆盖；仅写告警、不硬拦截）
        per_request_on = bool(cfg.get("enable_per_request_metrics"))
        force_on = bool(cfg.get("enable_force_include_usage"))
        if per_request_on and not force_on:
            self.warnings.append(
                f"{self.profile.name}：enable_per_request_metrics=true 但 enable_force_include_usage=false，"
                "流式中间块缺 usage 会使 stats.record_tokens 仅末块入账；建议同时开启"
            )
        self.run_compat_checks()  # 预检：软件规则 + 模型 id 特征
        if gpus is not None:
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
        model = str(cfg.get("model") or "")
        if not (model and (Path(model).expanduser().is_dir() or Path(model).expanduser().is_file())):
            if cfg.get("download"):
                modelscope_id = cfg["download"]["modelscope_id"]
                model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
                local_dir = download_repo(modelscope_id, model_root)
                if self.profile.path is None:
                    raise RequirementError(f"{self.profile.name}：profile 文件路径缺失，无法写回模型路径")
                persist_model_path(self.profile.path, "vllm", str(local_dir.resolve()))
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
        runtime, image = self._resolve_runtime()

        # 共用：--served-model-name 之后的 model_args
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model_args = [
            "--served-model-name", self.upstream_model_name(),
            "--host", "0.0.0.0",
            "--tensor-parallel-size", str(tp),
            "--gpu-memory-utilization", str(cfg.get("gpu_memory_utilization", 0.9)),
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
        model_args += self.api_key_args() + extra

        if runtime == "venv":
            cmd = [str(envs.engine_bin("vllm", "vllm")), "serve", str(cfg["model"])] + model_args
            return cmd, self._venv_env(gpus)

        # docker 分支
        model_raw = str(cfg.get("model") or "").strip()
        model_local = Path(model_raw).expanduser()
        if not model_local.is_absolute() or not model_local.is_dir():
            raise RequirementError(
                f"{self.profile.name}：docker_image 路径下 model 必须为本地绝对路径"
                f"且目录已存在（当前: {model_raw}——HF id 需先 modelctl start 触发 pre_start 下载）"
            )
        model_local = model_local.resolve()
        cmd = [
            "docker", "run",
            "--name", self._container_name,
            "--gpus", self._gpus_json(),
            "-p", f"{self.profile.port}:8000",
            "-v", f"{model_local.parent.as_posix()}:/models:ro",
            "--ipc=host",
            "--detach",
            # 镜像 ENTRYPOINT 是 ["vllm", "serve"]（vllm/vllm-openai Day-0 镜像约定），
            # CMD 仅传位置参数（模型路径）+ 命名参数。重复 "serve" 会被拼成
            # `vllm serve serve /models/...` 触发 argparse "unrecognized arguments" 退出 (exit code 2)
            image,
            f"/models/{model_local.name}",
        ] + model_args + ["--port", "8000"]
        # docker_env：yaml vllm.docker_env（dict）→ 容器内环境变量。
        # ⚠ 必须用 `docker run -e`（而非 build_command 返回的 env dict）才能进入容器：
        #   start_detached 只把 env 注入 docker CLI 宿主进程（Popen），不会透传进容器。
        # TZ 同理，必须显式带上，否则容器内 vLLM 日志是 UTC。
        env_args = self.docker_timezone_args()
        for k, v in (cfg.get("docker_env") or {}).items():
            env_args += ["-e", f"{k}={v}"]
        if env_args:
            ipc_idx = cmd.index("--ipc=host")
            cmd[ipc_idx:ipc_idx] = env_args
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
            # 首 Token 耗时：Histogram，无现成均值 gauge；stats.parse_metrics 以 sum/count 取均值
            "ttft_ms": ["vllm:time_to_first_token_seconds"],
        }

    def upstream_model_name(self) -> str:
        """vLLM 对外暴露的 served 模型名 = profile.name。

        与 build_command 的 --served-model-name、modelctl list 标识符一致：
        无论经网关转发还是直连 vLLM 端口，请求体 model 都用 profile.name（如 qwen3.8-vllm）。
        """
        return self.profile.name

    def _resolve_runtime(self) -> tuple[str, str | None]:
        """yaml 字段 vllm.docker_image 非空→('docker', image)；其余回退 ('venv', None)。

        留空时与改造前完全等价（仍走 `envs.engine_bin("vllm", "vllm")`），
        已部署的 7 个 models/vllm/*.yaml 行为零变化。
        """
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        return ("docker", image) if image else ("venv", None)

    @property
    def _container_name(self) -> str:
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

    def _venv_env(self, gpus: list[int] | None) -> dict[str, str]:
        """现状 env 注入段（HF_HOME / VIRTUAL_ENV / PATH）抽取为独立方法。"""
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "vllm")
        env["PATH"] = str(envs.engine_bin("vllm", "vllm").parent) + os.pathsep + \
            os.environ.get("PATH", os.environ["PATH"])
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

    def stop_backend(self) -> None:
        """docker 分支：docker rm -f <container>（清 PID 防御 venv/docker 环境切换残留）；
        venv 分支：基类 stop_instance（pkill 兜底）。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance
            stop_docker_instance(self.profile.name, self._container_name)
        else:
            super().stop_backend()

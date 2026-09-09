#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/tokenspeed.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/1 10:00
# @Desc   : TokenSpeed 引擎适配器
# ===============================================================================

"""engines/tokenspeed.py — TokenSpeed 适配器。"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from modelctl.core import docker_setup, envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.core.process import docker_container_alive, wait_health
from modelctl.engines._download import download_repo
from modelctl.engines.base import EngineAdapter, RequirementError


class TokenSpeedAdapter(EngineAdapter):
    def _resolve_runtime(self) -> tuple[str, str | None, str | None]:
        """runtime 分流：yaml 显式声明 docker_image 时容器优先（与 vllm 一致，2026-09 反转），
        venv 作为未声明镜像时的兜底路径。

        返回三元组 ``(runtime, image, dual_error)``；dual_error 非空时调用方（尤其是
        ``check_requirements``）应直接 `raise RequirementError(dual_error)`，避免落入 venv
        分支后由 ``ensure_env`` 抛出"环境未创建"这种只暴露 venv 缺口、丢失 docker 兜底
        缺失信息的误导性错误。

        分流规则（与 vllm 一致）：
        1. yaml ``docker_image`` 非空 → ``('docker', image, None)``
           —— 容器是模型作者的权威声明，即使本机有可用 venv 仍走 docker
        2. 否则 ``envs.engine_native_usable('tokenspeed')`` 为 True → ``('venv', None, None)``
        3. 否则 → ``('venv', None, <friendly_msg>)``，文案按 platform_supports 分支
        """
        cfg = self.profile.engine_config
        image = str(cfg.get("docker_image") or "").strip()
        if image:
            return ("docker", image, None)
        if envs.engine_native_usable("tokenspeed"):
            return ("venv", None, None)
        if envs.platform_supports("tokenspeed"):
            dual = (
                f"{self.profile.name}：TokenSpeed 的托管 venv 未创建（当前平台 Linux 支持 venv），"
                "且 yaml tokenspeed.docker_image 未配置（docker 兜底也未启用）。请先执行 "
                "`modelctl env setup tokenspeed` 建立 venv，或在 yaml 的 tokenspeed: 块下配置 "
                "`docker_image: lightseekorg/tokenspeed:latest` 走 docker 运行时"
            )
        else:
            dual = (
                f"{self.profile.name}：TokenSpeed 的托管 venv 仅支持 Linux 部署机，当前平台不支持；"
                "且 yaml tokenspeed.docker_image 未配置（docker 兜底也未启用）。请在 yaml 的 "
                "tokenspeed: 块下配置 `docker_image: lightseekorg/tokenspeed:latest` 走 docker 运行时"
                "（镜像需已 pull；model 必须是本地目录）"
            )
        return ("venv", None, dual)

    def check_requirements(self) -> None:
        cfg = self.profile.engine_config
        runtime, image, dual_error = self._resolve_runtime()
        if dual_error:
            raise RequirementError(dual_error)
        if runtime == "docker":
            missing = docker_setup.path_level_missing()
            if missing:
                raise RequirementError(f"docker_image 已配置但 Docker 环境未就绪：{'；'.join(missing)}——{docker_setup.MSG_GUIDE}")
            # 清冲突残留容器（幂等；失败仅 warning + 解码 stderr，不再静默吞）
            from modelctl.core.process import clear_stale_docker_container
            clear_stale_docker_container(self.profile.name, self._container_name)
        else:
            envs.ensure_env("tokenspeed")
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：tokenspeed.model 必填（或配置 download 段自动下载）")
        try:
            gpus = self.selected_gpus()
        except (GPUValidationError, ValueError) as exc:
            raise RequirementError(f"[gpu_list] {exc}") from exc
        if gpus is not None:
            self.validate_gpu_selection(gpus)
            tp = int(cfg.get("tensor_parallel_size", len(gpus)))
            if tp != len(gpus):
                raise RequirementError(
                    f"gpu_list 指定了 {len(gpus)} 块 GPU，但 tensor_parallel_size={tp}"
                )
        else:
            tp = int(cfg.get("tensor_parallel_size", 1))
            if self.caps.gpu_count and tp > self.caps.gpu_count:
                raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数")
        self.run_compat_checks()
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        # docker 路径先确保镜像就位（大镜像跨境拉取易中途 EOF，需显式 pull + 重试）
        runtime, image, dual_error = self._resolve_runtime()
        if dual_error:
            raise RequirementError(dual_error)
        if runtime == "docker" and not docker_setup.ensure_image(image, on_progress=self._progress_cb):
            raise RequirementError(
                f"{self.profile.name}：镜像 {image} 未就位，无法启动容器；"
                "详见日志中的 docker pull 错误分类与对应处置"
            )
        model = str(cfg.get("model") or "")
        if not (model and Path(model).expanduser().is_dir()):
            if cfg.get("download"):
                modelscope_id = cfg["download"]["modelscope_id"]
                model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
                # 落地路径由 MODEL_ROOT + modelscope_id 确定性推导，目录已存在即复用；
                # 仅更新内存中的 cfg，不写回 YAML（保持 profile 文件干净、多机可移植）。
                local_dir = download_repo(modelscope_id, model_root)
                cfg["model"] = str(local_dir.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        gpus = self.selected_gpus()
        tp = len(gpus) if gpus else int(cfg.get("tensor_parallel_size", 1))
        runtime, image, dual_error = self._resolve_runtime()
        if dual_error:
            raise RequirementError(dual_error)
        extra = shlex.split(str(cfg.get("extra_args") or ""))
        model = str(cfg["model"])
        # 安全加固：默认仅绑定 loopback，杜绝外部直连引擎端口绕过网关鉴权/限额/审计。
        # 确需对外暴露时显式配置 bind_host: 0.0.0.0（并配合防火墙/白名单限制来源）。
        bind_host = str(cfg.get("bind_host", "127.0.0.1"))

        if runtime == "docker":
            model_local = Path(model).expanduser().resolve()
            cmd = [
                "docker", "run", "--rm", "--detach",
                "--name", self._container_name,
                "--gpus", self._gpus_json(gpus, tp),
                # -p 绑定 {bind_host}:{port}:8000：宿主机 docker-proxy 仅监听 127.0.0.1，
                # 容器内服务仍绑 0.0.0.0，外部无法连接宿主机该端口，杜绝绕过网关直连。
                "-p", f"{bind_host}:{self.profile.port}:8000",
                "-v", f"{model_local.parent.as_posix()}:/models:ro",
                "--ipc=host",
                # 容器时区只能靠 -e（start_detached 的 env 进不了容器），否则日志是 UTC
                *self.docker_timezone_args(),
                image,
                "serve", f"/models/{model_local.name}",
                "--host", "0.0.0.0",
                "--port", "8000",
                "--tp", str(tp),
            ]
            if cfg.get("max_model_len"):
                cmd += ["--max-model-len", str(cfg["max_model_len"])]
            cmd += self.api_key_args()
            cmd += extra
            env = {}
            if gpus:
                env.update(self.cuda_visible_devices(gpus))
            return cmd, env

        cmd = [
            str(envs.engine_bin("tokenspeed", "tokenspeed")),
            "serve",
            model,
            "--port", str(self.profile.port),
            "--tp", str(tp),
        ]
        if cfg.get("max_model_len"):
            cmd += ["--max-model-len", str(cfg["max_model_len"])]
        cmd += self.api_key_args() + extra
        # 权威 --host 置于 extra 之后：bind_host 安全值不被 extra_args 里的同名 --host 覆盖回 0.0.0.0
        cmd += ["--host", bind_host]
        env = {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "tokenspeed")
        env["PATH"] = str(envs.engine_bin("tokenspeed", "tokenspeed").parent) + os.pathsep + os.environ.get("PATH", "")
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        # §2.2 速率 gauge 补全：参照 vllm/sglang 风格暴露 avg_*_throughput gauge。
        # 缺失/恒为 0 时 stats 退化为窗口差分（不会出错）。
        return {
            "prompt_total": ["tokenspeed:prompt_tokens_total"],
            "predicted_total": ["tokenspeed:generation_tokens_total"],
            "prompt_rate": [
                "tokenspeed:prompt_tokens_seconds",
                "tokenspeed:avg_prompt_throughput_toks_per_sec",
            ],
            "predicted_rate": [
                "tokenspeed:generation_tokens_seconds",
                "tokenspeed:avg_generation_throughput_toks_per_sec",
            ],
        }

    def _gpus_json(self, gpus, tp) -> str:
        if gpus:
            seq = list(gpus)
        else:
            seq = list(range(int(self.caps.gpu_count or tp)))
        return '"device=' + ",".join(str(g) for g in seq) + '"'

    @property
    def _container_name(self) -> str:
        """docker 容器名：profile.name 已以 ``-tokenspeed`` 结尾时不再追加（避免双后缀）。

        典型 profile.name（{group}-{engine} 推导）如 ``qwen2.5-0.5b-tokenspeed`` 已含
        engine 短名，旧实现 ``f"{profile.name}-tokenspeed"`` 会拼出
        ``qwen2.5-0.5b-tokenspeed-tokenspeed``。本实现幂等：以 ``-tokenspeed`` 结尾时
        直接用 profile.name，否则追加 ``-tokenspeed``。
        """
        if self.profile.name.endswith("-tokenspeed"):
            return self.profile.name
        return f"{self.profile.name}-tokenspeed"

    def wait_ready(self, timeout: float) -> bool:
        if self._resolve_runtime()[0] == "docker":
            return wait_health(
                self.health_url(),
                timeout,
                self.upstream_api_key(),
                alive_check=lambda: docker_container_alive(self._container_name),
            )
        return super().wait_ready(timeout)

    def backend_dead(self) -> bool:
        """docker 分支：后端死亡 = 容器已退出/不存在（客户端进程早退不等于容器死亡）。"""
        if self._resolve_runtime()[0] == "docker":
            return not docker_container_alive(self._container_name)
        return super().backend_dead()

    def stop_patterns(self) -> list[str]:
        if self._resolve_runtime()[0] != "docker":
            return ["tokenspeed serve"]
        name = self._container_name
        return [f"docker run --rm --detach --name {name}"]

    def is_docker_runtime(self) -> bool:
        """tokenspeed 路径判定：docker_image 字段非空时走 docker runtime。"""
        return self._resolve_runtime()[0] == "docker"

    def log_tee_cmd(self) -> list[str] | None:
        """docker 分支：`docker logs -f --tail all <container>` 续写容器输出。

        用 `--tail all` 而非 `--tail 0`：`docker run --detach` 秒返回，只跟新行会漏掉
        tee 挂上前数百毫秒内的 banner 行，伤及 loading 段模式表匹配（理由同 vllm）。
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
        """docker 分支：docker rm -f <container>；venv 分支：基类 stop_instance。"""
        if self._resolve_runtime()[0] == "docker":
            from modelctl.core.process import stop_docker_instance
            stop_docker_instance(self.profile.name, self._container_name)
        else:
            super().stop_backend()

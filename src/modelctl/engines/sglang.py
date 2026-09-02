#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/engines/sglang.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/7/25 10:00
# @Desc   : SGLang 引擎适配器
# ===============================================================================

"""engines/sglang.py — SGLang 适配器。"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from modelctl.core import envs
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.core.gpu_lock import acquire_gpu_lock
from modelctl.core.gpu_utils import GPUValidationError
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError


class SglangAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        envs.ensure_env("sglang")
        cfg = self.profile.engine_config
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：sglang.model 必填（或配置 download 段自动下载）")
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
        self.run_compat_checks()  # 预检：软件规则 + 模型 id 特征
        if gpus is not None:
            acquire_gpu_lock(self.profile.name, gpus)

    def _check_vram_advisory(self, cfg: dict, gpus: list[int] | None) -> None:
        """HF 权重粗估（spec §2.1）：权重大小超可用显存上限时仅告警、不硬拦截。

        上限按 总显存 × mem_fraction_static 估算；未计 KV cache/激活，
        HF 权重加载行为复杂，故不做硬性 block（sglang 自身启动时会 OOM 报错）。
        """
        self._check_weights_advisory(
            str(cfg.get("model") or ""),
            gpus,
            self.caps,
            float(cfg.get("mem_fraction_static", 0.85)),
            "mem_fraction_static",
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
                persist_model_path(self.profile.path, "sglang", str(local_dir.resolve()))
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
        cmd = [
            str(envs.engine_python("sglang")),
            "-m",
            "sglang.launch_server",
            "--model-path",
            str(cfg["model"]),
            "--served-model-name",
            self.upstream_model_name(),
            "--host",
            "0.0.0.0",
            "--port",
            str(self.profile.port),
            "--tp",
            str(tp),
        ]
        if cfg.get("context_length"):
            cmd += ["--context-length", str(cfg["context_length"])]
        if cfg.get("mem_fraction_static"):
            cmd += ["--mem-fraction-static", str(cfg["mem_fraction_static"])]
        if cfg.get("extra_args"):
            cmd += shlex.split(str(cfg["extra_args"]))
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        if gpus:
            env.update(self.cuda_visible_devices(gpus))
        # 注入引擎专用 venv：让 sglang 子进程显式感知虚拟环境
        env["VIRTUAL_ENV"] = str(envs.VENV_ROOT / "sglang")
        env["PATH"] = str(envs.engine_python("sglang").parent) + os.pathsep + env.get("PATH", os.environ["PATH"])
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["sglang:prompt_tokens_total"],
            "predicted_total": ["sglang:generation_tokens_total"],
            "prompt_rate": [],
            "predicted_rate": [],
        }

    def upstream_model_name(self) -> str:
        """SGLang 对外暴露的 served 模型名 = profile.name。

        与 build_command 的 --served-model-name、modelctl list 标识符一致：
        无论经网关转发还是直连 SGLang 端口，请求体 model 都用 profile.name。
        """
        return self.profile.name

    def stop_patterns(self) -> list[str]:
        # 用启动模块特征而非引擎短名，避免 pkill 误杀 modelctl 自身（命令行含 profile 名）
        return ["sglang.launch_server"]

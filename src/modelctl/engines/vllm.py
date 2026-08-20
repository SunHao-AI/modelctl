#!/usr/bin/env python3
"""engines/vllm.py — vLLM 适配器。"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from modelctl.core.capabilities import cc_at_least
from modelctl.core.envfile import PROJECT_ROOT
from modelctl.engines._download import download_repo
from modelctl.engines._persist import persist_model_path
from modelctl.engines.base import EngineAdapter, RequirementError

# DeepSeek-V4 的 mHC（Manifold-Constrained Hyper-Connections）层依赖 DeepGEMM 的
# tf32_hc_prenorm_gemm 内核，官方仅提供 SM90（Hopper）/SM100（Blackwell DC）实现，
# 在 Ada（sm_89）等架构上启动即抛 "Unsupported architecture"。
_DEEPSEEK_V4_ARCHS = ("DeepseekV4ForCausalLM",)
_DEEPSEEK_V4_NAME_MARKERS = ("deepseek-v4", "deepseek_v4")
_DEEPSEEK_V4_SUPPORTED_CC_MAJORS = (9, 10)


def _cc_major(cc: str) -> int | None:
    """提取 compute capability 主版本号（"8.9" -> 8）；无法解析返回 None。"""
    try:
        return int(cc.split(".", 1)[0])
    except (ValueError, AttributeError):
        return None


def _is_deepseek_v4(cfg: dict) -> bool:
    """判断目标模型是否为 DeepSeek-V4。

    本地目录优先读取 config.json 判定架构；远程 id / download 配置按名称特征兜底。
    """
    model = str(cfg.get("model") or "")
    local = Path(model).expanduser()
    if local.is_dir():
        config = local / "config.json"
        if config.is_file():
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            archs = " ".join(data.get("architectures") or [])
            model_type = str(data.get("model_type") or "").lower()
            if any(a in archs for a in _DEEPSEEK_V4_ARCHS) or "deepseek_v4" in model_type:
                return True
    download = cfg.get("download") or {}
    markers = [model, str(download.get("modelscope_id") or "")]
    lowered = " ".join(m.lower() for m in markers)
    return any(marker in lowered for marker in _DEEPSEEK_V4_NAME_MARKERS)


class VllmAdapter(EngineAdapter):
    def check_requirements(self) -> None:
        if not self.caps.binaries.get("vllm"):
            raise RequirementError("未安装 vllm（PATH 中找不到 vllm 命令）")
        cfg = self.profile.engine_config
        if not cfg.get("model") and not cfg.get("download"):
            raise RequirementError(f"{self.profile.name}：vllm.model 必填（或配置 download 段自动下载）")
        tp = int(cfg.get("tensor_parallel_size", 1))
        if self.caps.gpu_count and tp > self.caps.gpu_count:
            raise RequirementError(f"tensor_parallel_size={tp} 超过实际 GPU 数 {self.caps.gpu_count}")
        if cfg.get("quantization") == "fp8" and not cc_at_least(self.caps.compute_capability, 8, 9):
            raise RequirementError(f"FP8 量化需要 CC ≥ 8.9，当前 {self.caps.compute_capability or '未知'}")
        if _is_deepseek_v4(cfg):
            cc = self.caps.compute_capability
            if cc and _cc_major(cc) not in _DEEPSEEK_V4_SUPPORTED_CC_MAJORS:
                gpu = self.caps.gpu_name or f"GPU（CC {cc}）"
                raise RequirementError(
                    f"当前服务器不支持 vllm 引擎部署 {self.profile.name} 模型，原因："
                    f"DeepSeek-V4 的 mHC（HyperConnection）层依赖 DeepGEMM hyperconnection 内核，"
                    f"官方仅支持 Hopper/Blackwell DC（计算能力 9.0/10.0），"
                    f"当前 GPU 为 {gpu}（CC {cc}）。"
                    f"如仍需在 {cc} 架构上部署，可改用 llamacpp 引擎运行 GGUF 版本"
                    "（models/llamacpp/deepseek-v4-flash.yaml）。"
                )

    def pre_start(self) -> None:
        cfg = self.profile.engine_config
        model = str(cfg.get("model") or "")
        if model and (Path(model).expanduser().is_dir() or Path(model).expanduser().is_file()):
            return
        if cfg.get("download"):
            modelscope_id = cfg["download"]["modelscope_id"]
            model_root = Path(os.environ.get("MODEL_ROOT") or PROJECT_ROOT.parent / "model-hf")
            local_dir = download_repo(modelscope_id, model_root)
            if self.profile.path is None:
                raise RequirementError(f"{self.profile.name}：profile 文件路径缺失，无法写回模型路径")
            persist_model_path(self.profile.path, "vllm", str(local_dir.resolve()))
            cfg["model"] = str(local_dir.resolve())

    def build_command(self) -> tuple[list[str], dict[str, str]]:
        cfg = self.profile.engine_config
        cmd = [
            "vllm",
            "serve",
            str(cfg["model"]),
            "--host",
            "0.0.0.0",
            "--port",
            str(self.profile.port),
            "--tensor-parallel-size",
            str(cfg.get("tensor_parallel_size", 1)),
            "--gpu-memory-utilization",
            str(cfg.get("gpu_memory_utilization", 0.9)),
        ]
        if cfg.get("max_model_len"):
            cmd += ["--max-model-len", str(cfg["max_model_len"])]
        if cfg.get("quantization"):
            cmd += ["--quantization", str(cfg["quantization"])]
        if cfg.get("kv_cache_dtype"):
            cmd += ["--kv-cache-dtype", str(cfg["kv_cache_dtype"])]
        cmd += self.api_key_args()
        if cfg.get("extra_args"):
            cmd += shlex.split(str(cfg["extra_args"]))
        env = {"HF_HOME": os.environ["HF_HOME"]} if os.environ.get("HF_HOME") else {}
        return cmd, env

    def metrics_mapping(self) -> dict[str, list[str]]:
        return {
            "prompt_total": ["vllm:prompt_tokens_total"],
            "predicted_total": ["vllm:generation_tokens_total"],
            "prompt_rate": [],
            "predicted_rate": [],
        }

    def upstream_model_name(self) -> str:
        return str(self.profile.engine_config.get("model") or self.profile.name)

    def stop_patterns(self) -> list[str]:
        return ["vllm"]

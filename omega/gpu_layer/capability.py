"""capability — 硬件能力分级与模式选择。

根据 HardwareProfile 自动判断能力等级 (capability tier)，
并映射到 PlanManager 的 gpu_mode 参数。

等级体系:
  cpu-only:    无可用 GPU，仅 DeepSeek API
  gpu-light:   VRAM < 24GB，可跑 7B Q4 模型，不支持 LoRA 训练
  gpu-full:    VRAM ≥ 24GB，可跑 8B+ 模型 + LoRA + Beam 搜索
  gpu-cluster: 多卡 (≥4)，可跑分布式训练 + 批量推理
  mps-only:    Apple Silicon (MPS)，能力介于 cpu-only 和 gpu-light 之间
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omega.gpu_layer.detector import HardwareProfile

logger = logging.getLogger("omega.gpu_layer.capability")


# ── 能力等级 → gpu_mode 映射 ──────────────────────────────────

CAPABILITY_TO_GPU_MODE: dict[str, str] = {
    "cpu-only": "cpu-only",
    "gpu-light": "gpu-minimal",
    "gpu-full": "gpu-batch",
    "gpu-cluster": "gpu-batch",
    "mps-only": "gpu-minimal",
}

CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    "cpu-only": "无可用 GPU → DeepSeek API 作为唯一 LLM 后端",
    "gpu-light": "轻量 GPU (<24GB VRAM) → 仅 7B Q4 推理，不支持 LoRA 训练",
    "gpu-full": "标准 GPU (≥24GB VRAM) → 8B+ 模型 + LoRA + Beam 搜索",
    "gpu-cluster": "多卡集群 (≥4) → 批量推理 + 分布式训练",
    "mps-only": "Apple Silicon (MPS) → 小型模型推理 + API 混合",
}


def classify_capability(hw: HardwareProfile) -> str:
    """根据硬件画像自动确定能力等级。

    Args:
        hw: 全方位硬件画像 (HardwareProfile.detect() 的结果)

    Returns:
        能力等级字符串: cpu-only | gpu-light | gpu-full | gpu-cluster | mps-only
    """
    # ── 1. NVIDIA GPU with CUDA ──
    if hw.has_gpu and hw.pytorch_device == "cuda":
        n_gpus = len(hw.gpus)
        best_vram = hw.best_vram_gb

        if n_gpus >= 4 and best_vram >= 40:
            return "gpu-cluster"
        if best_vram >= 24:
            return "gpu-full"
        if best_vram >= 12:
            return "gpu-light"
        # VRAM < 12GB — 勉强可跑 7B Q4
        return "gpu-light"

    # ── 2. Apple Silicon (MPS) ──
    if hw.pytorch_device == "mps":
        # Apple Silicon 统一内存，RAM size = 可用容量
        if hw.ram_total_gb >= 16:
            return "gpu-light"  # M1 Pro/Max/Ultra 或 M2/M3 可跑 7B Q4
        return "mps-only"

    # ── 3. Intel XPU / OpenVINO ──
    if hw.pytorch_device == "xpu":
        return "gpu-light"

    # ── 4. CPU-only — 无任何加速设备 ──
    return "cpu-only"


def capability_to_gpu_mode(capability: str) -> str:
    """能力等级 → PlanManager 的 gpu_mode 参数。"""
    mode = CAPABILITY_TO_GPU_MODE.get(capability)
    if mode is None:
        logger.warning("Unknown capability '%s', falling back to cpu-only", capability)
        return "cpu-only"
    return mode


def describe_capability(capability: str) -> str:
    """返回能力等级的中文描述。"""
    return CAPABILITY_DESCRIPTIONS.get(capability, "未知能力等级")


def suggest_model(capability: str) -> str:
    """根据能力等级推荐 LLM 模型。"""
    models = {
        "cpu-only": "deepseek/deepseek-v4-flash",
        "gpu-light": "deepseek/deepseek-v4-flash",  # 本地 7B 不如 API 强
        "gpu-full": "Goedel-Prover-V2-8B",           # 本地推理主力
        "gpu-cluster": "Goedel-Prover-V2-8B",        # 批量推理
        "mps-only": "deepseek/deepseek-v4-flash",    # API 优先
    }
    return models.get(capability, "deepseek/deepseek-v4-flash")


def suggest_backend(capability: str) -> str:
    """根据能力等级推荐推理后端。"""
    backends = {
        "cpu-only": "deepseek-api",
        "gpu-light": "deepseek-api",
        "gpu-full": "vllm",
        "gpu-cluster": "vllm",
        "mps-only": "deepseek-api",
    }
    return backends.get(capability, "deepseek-api")

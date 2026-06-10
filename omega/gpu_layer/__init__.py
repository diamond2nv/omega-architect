"""GPU Layer — 硬件感知与推理后端调度层。

三层架构:
  1. detector: GPU、VRAM、CUDA、Ollama/vLLM 服务检测
  2. backends: 统一推理接口 (Ollama / vLLM API / Transformers inline)
  3. scheduler: 模型路由 + 生命周期管理 + 故障恢复

核心设计原则:
  - 自动检测最优后端，不需要用户指定
  - 服务端（vLLM）常驻，不反复加载/卸载
  - Ollama 走 Windows 原生（WSL 穿 /mnt/）
  - 所有推理调用幂等，可重试
"""

from .detector import HardwareDetector, detect_all
from .backends import (
    InferenceBackend,
    VLLMBackend,
    OllamaBackend,
    TransformersBackend,
    get_best_backend,
)
from .scheduler import GPUScheduler, gpu_scheduler

__all__ = [
    "HardwareDetector", "detect_all",
    "InferenceBackend", "VLLMBackend", "OllamaBackend", "TransformersBackend",
    "get_best_backend",
    "GPUScheduler", "gpu_scheduler",
]

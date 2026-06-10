"""GPUScheduler — 全局 GPU 资源调度与生命周期管理。

核心功能:
  - 单例调度器 (gpu_scheduler)
  - 自动检测 + 启动最优后端
  - 服务健康监控
  - 推理时自动重试/切换后端
  - 空闲超时自动释放 GPU

用法:
  from omega.gpu_layer import gpu_scheduler

  # 自动选择后端并推理
  texts = gpu_scheduler.generate(
      messages=[{"role": "user", "content": "..."}],
      model="goedel",
      max_tokens=2048,
      n=4,
  )
"""

import time
import threading
from typing import Optional

from .detector import HardwareDetector, HardwareSnapshot
from .backends import (
    InferenceBackend,
    VLLMBackend,
    OllamaBackend,
    TransformersBackend,
    get_best_backend,
)

# ── 模型到后端映射 ────────────────────────────────────────────

MODEL_ROUTING = {
    # Goedel-Prover-V2 → vLLM (本地推理)
    "goedel": {
        "backend": "vllm",
        "vllm_port": 8001,
        "prefer_batch": True,  # 支持批量推理
    },
    # DeepSeek → vLLM or Transformers
    "deepseek": {
        "backend": "vllm",
        "fallback": "transformers",
    },
    # 通用 / 未映射 → 自动路由
    "default": {
        "backend": "auto",
    },
}


# ── 调度器 ─────────────────────────────────────────────────────

class GPUScheduler:
    """GPU 资源调度器（单例）。"""

    def __init__(self):
        self._backend: Optional[InferenceBackend] = None
        self._snapshot: Optional[HardwareSnapshot] = None
        self._lock = threading.Lock()
        self._last_health_check = 0.0
        self._health_check_interval = 30.0  # 30s 检查一次

    # ── 初始化 ────────────────────────────────────────────────

    def ensure_initialized(self, force_refresh: bool = False):
        """确保后端已初始化。"""
        with self._lock:
            if self._backend is not None and not force_refresh:
                return
            self._snapshot = HardwareDetector().detect()
            print(f"[GPUScheduler] Hardware: {self._snapshot.summary()}")
            self._backend = get_best_backend(self._snapshot, model_name="goedel")
            print(f"[GPUScheduler] Using backend: {self._backend.name}")

    # ── 健康检查 ──────────────────────────────────────────────

    def _check_health(self):
        now = time.time()
        if now - self._last_health_check < self._health_check_interval:
            return True
        self._last_health_check = now
        if self._backend and self._backend.health():
            return True
        # 后端挂了，尝试恢复
        print("[GPUScheduler] Backend unhealthy, reinitializing...")
        self.ensure_initialized(force_refresh=True)
        return self._backend is not None and self._backend.health()

    # ── 推理 ──────────────────────────────────────────────────

    def generate(
        self,
        messages: list[dict],
        model: str = "goedel",
        max_tokens: int = 2048,
        temperature: float = 0.6,
        n: int = 1,
    ) -> list[str]:
        """统一推理入口。自动选择后端、健康检查、重试。"""
        self.ensure_initialized()
        self._check_health()

        if self._backend is None:
            raise RuntimeError("No backend available")

        # 配置参数
        routing = MODEL_ROUTING.get(model, MODEL_ROUTING["default"])

        # 如果模型指定了特定后端，但当前不是，尝试切换
        if routing["backend"] != "auto":
            bk_type = routing["backend"]
            if self._backend.name != bk_type:
                self._switch_backend(bk_type)

        # 执行推理
        max_retries = 2
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                return self._backend.generate(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    n=n,
                )
            except Exception as e:
                print(f"[GPUScheduler] Inference failed (attempt {attempt+1}): {e}")
                last_error = e
                if attempt < max_retries:
                    self.ensure_initialized(force_refresh=True)
        raise RuntimeError(f"Inference failed after {max_retries+1} attempts: {last_error}")

    def _switch_backend(self, backend_type: str):
        """切换到指定类型后端。"""
        if self._backend:
            self._backend.release()
        if backend_type == "vllm":
            self._backend = VLLMBackend()
            self._backend.ensure_running()
        elif backend_type == "ollama":
            self._backend = OllamaBackend()
        elif backend_type == "transformers":
            self._backend = TransformersBackend()
        else:
            self.ensure_initialized(force_refresh=True)

    # ── 资源释放 ──────────────────────────────────────────────

    def release(self):
        """释放所有 GPU 资源。"""
        with self._lock:
            if self._backend:
                self._backend.release()
                self._backend = None
            print("[GPUScheduler] GPU resources released")

    def snapshot(self) -> HardwareSnapshot:
        """获取当前硬件快照。"""
        return HardwareDetector().detect()

    def status(self) -> dict:
        """调度器状态报告。"""
        snap = self.snapshot()
        return {
            "backend": self._backend.name if self._backend else None,
            "vllm_running": snap.vllm_server_alive,
            "vllm_port": snap.vllm_port,
            "ollama_running": snap.ollama_alive or snap.ollama_windows_alive,
            "gpus": [
                {"index": g.index, "name": g.name,
                 "vram_free_gb": g.vram_free_gb,
                 "vram_total_gb": g.vram_total_gb}
                for g in snap.gpus
            ],
            "health": self._backend.health() if self._backend else False,
        }


# ── 全局单例 ──────────────────────────────────────────────────

gpu_scheduler = GPUScheduler()


# ── 便捷接口 ──────────────────────────────────────────────────

def generate(
    messages: list[dict],
    model: str = "goedel",
    max_tokens: int = 2048,
    temperature: float = 0.6,
    n: int = 1,
) -> list[str]:
    """全局便捷推理函数。"""
    return gpu_scheduler.generate(
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        n=n,
    )


if __name__ == "__main__":
    import json
    snap = gpu_scheduler.snapshot()
    print(snap.summary())
    print()
    print(json.dumps(gpu_scheduler.status(), indent=2))

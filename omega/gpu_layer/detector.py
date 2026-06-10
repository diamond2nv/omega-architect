"""HardwareDetector — GPU 硬件、CUDA、推理服务检测。

检测项:
  - GPU 型号 & VRAM (nvidia-smi)
  - CUDA 版本
  - vLLM server 存活 (localhost:8001)
  - Ollama 服务存活 (Windows 宿主机 / WSL)
  - Transformers (PyTorch CUDA 可用性)
"""

import json
import os
import subprocess
import shutil
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GPUInfo:
    """单个 GPU 信息。"""
    index: int
    name: str
    vram_total_gb: float
    vram_free_gb: float
    vram_used_gb: float
    utilization_pct: float


@dataclass
class HardwareSnapshot:
    """一次硬件扫描的快照。"""
    gpus: list[GPUInfo] = field(default_factory=list)
    cuda_version: str = ""
    pytorch_cuda_available: bool = False
    vllm_server_alive: bool = False
    vllm_port: int = 8001
    ollama_alive: bool = False         # WSL-detectable Ollama
    ollama_windows_alive: bool = False  # Windows原生 Ollama
    transformers_available: bool = False

    @property
    def total_vram_gb(self) -> float:
        return sum(g.vram_total_gb for g in self.gpus)

    @property
    def free_vram_gb(self) -> float:
        return sum(g.vram_free_gb for g in self.gpus)

    @property
    def best_gpu(self) -> Optional[GPUInfo]:
        return max(self.gpus, key=lambda g: g.vram_free_gb) if self.gpus else None

    def summary(self) -> str:
        lines = [f"GPU: {len(self.gpus)} detected"]
        for g in self.gpus:
            lines.append(f"  [{g.index}] {g.name} — {g.vram_free_gb:.1f}/{g.vram_total_gb:.0f} GB free")
        lines.append(f"CUDA: {self.cuda_version}")
        lines.append(f"vLLM server: {'✅' if self.vllm_server_alive else '❌'} :{self.vllm_port}")
        lines.append(f"Ollama: {'✅' if self.ollama_alive else '❌'} (WSL) / {'✅' if self.ollama_windows_alive else '❌'} (Windows)")
        lines.append(f"Transformers: {'✅' if self.transformers_available else '❌'}")
        return "\n".join(lines)


class HardwareDetector:
    """硬件与服务检测器。"""

    def __init__(self, vllm_port: int = 8001, ollama_host: str = "http://localhost:11434"):
        self.vllm_port = vllm_port
        self.ollama_host = ollama_host

    def detect(self) -> HardwareSnapshot:
        snap = HardwareSnapshot(vllm_port=self.vllm_port)
        snap.gpus = self._detect_gpus()
        snap.cuda_version = self._detect_cuda_version()
        snap.pytorch_cuda_available = self._check_pytorch_cuda()
        snap.vllm_server_alive = self._check_vllm()
        snap.ollama_alive = self._check_ollama(host=self.ollama_host)
        snap.ollama_windows_alive = self._check_ollama_windows()
        snap.transformers_available = snap.pytorch_cuda_available
        return snap

    # ── GPU ──────────────────────────────────────────────────

    def _detect_gpus(self) -> list[GPUInfo]:
        """检测 GPU — 优先 nvidia-smi，回退 PyTorch。"""
        gpus = self._detect_gpus_nvidia_smi()
        if gpus:
            return gpus
        return self._detect_gpus_pytorch()

    def _detect_gpus_nvidia_smi(self) -> list[GPUInfo]:
        """通过 nvidia-smi 检测 GPU。"""
        if not shutil.which("nvidia-smi"):
            return []
        try:
            out = subprocess.run(
                [
                    "nvidia-smi", "--query-gpu=index,name,memory.total,memory.free,"
                    "memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=10,
            )
            gpus = []
            for line in out.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    try:
                        gpus.append(GPUInfo(
                            index=int(parts[0]),
                            name=parts[1],
                            vram_total_gb=float(parts[2]) / 1024,
                            vram_free_gb=float(parts[3]) / 1024,
                            vram_used_gb=float(parts[4]) / 1024,
                            utilization_pct=float(parts[5]),
                        ))
                    except ValueError:
                        continue
            return gpus
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def _detect_gpus_pytorch(self) -> list[GPUInfo]:
        """通过 PyTorch 检测 GPU (不依赖 nvidia-smi)。"""
        try:
            import torch
            if not torch.cuda.is_available():
                return []
            gpus = []
            for i in range(torch.cuda.device_count()):
                p = torch.cuda.get_device_properties(i)
                free_bytes, total_bytes = torch.cuda.mem_get_info(i)
                gpus.append(GPUInfo(
                    index=i,
                    name=p.name,
                    vram_total_gb=total_bytes / 1e9,
                    vram_free_gb=free_bytes / 1e9,
                    vram_used_gb=(total_bytes - free_bytes) / 1e9,
                    utilization_pct=0.0,  # PyTorch 无法获取利用率
                ))
            return gpus
        except (ImportError, RuntimeError, AttributeError):
            return []

    def _detect_cuda_version(self) -> str:
        try:
            out = subprocess.run(
                ["nvidia-smi"], capture_output=True, text=True, timeout=5
            )
            for line in out.stdout.split("\n"):
                if "CUDA Version" in line:
                    return line.split("CUDA Version:")[-1].strip()
            return ""
        except Exception:
            return ""

    def _check_pytorch_cuda(self) -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    # ── vLLM ─────────────────────────────────────────────────

    def _check_vllm(self) -> bool:
        """检查 vLLM server health endpoint。"""
        try:
            import urllib.request
            req = urllib.request.Request(f"http://localhost:{self.vllm_port}/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ── Ollama ───────────────────────────────────────────────

    OLLAMA_ENDPOINTS = [
        "http://localhost:11434",          # WSL 本地 / WSL2 端口转发到 Windows
        "http://host.docker.internal:11434", # Docker 网络穿透
    ]

    def _check_ollama(self, host: str = "") -> bool:
        """检查指定端点的 Ollama 服务。"""
        endpoints = [host] if host else self.OLLAMA_ENDPOINTS
        import urllib.request
        for ep in endpoints:
            try:
                req = urllib.request.Request(f"{ep}/api/tags")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
        return False

    def _check_ollama_windows(self) -> bool:
        """检查 Windows 宿主机 Ollama (仅 HTTP API，不依赖 /mnt/ 文件系统)。

        WSL2 自动转发 localhost:11434 → Windows，无需特殊配置。
        """
        return self._check_ollama("http://localhost:11434")


def detect_all() -> HardwareSnapshot:
    """一键检测所有硬件和服务。"""
    return HardwareDetector().detect()


if __name__ == "__main__":
    snap = detect_all()
    print(snap.summary())

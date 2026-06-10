"""InferenceBackend — 统一推理接口 (Ollama / vLLM / Transformers)。

三个后端实现同一个抽象:
  - generate(messages, max_tokens, temperature) → list[str]
  - health() → bool
  - release() → None  # 释放 GPU 资源

自动路由: get_best_backend(snapshot, model_name) 返回最优可用后端。
"""

import json
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .detector import HardwareSnapshot, HardwareDetector


# ── 抽象基类 ──────────────────────────────────────────────────

class InferenceBackend(ABC):
    """统一推理接口。"""

    name: str = "base"

    @abstractmethod
    def generate(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.6,
        n: int = 1,
    ) -> list[str]:
        ...

    @abstractmethod
    def health(self) -> bool:
        ...

    def release(self):
        """释放 GPU 资源。默认空实现。"""
        pass


# ── vLLM Backend ──────────────────────────────────────────────

DEFAULT_VLLM_PORT = 8001
VLLM_MODEL_PATH = "/home/shenli/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B/snapshots/dfd02e6271a58375dfbf3ece0175277cf6b6a89a"


class VLLMBackend(InferenceBackend):
    """通过 vLLM API server 推理。

    支持:
      - 自动启动 server (如果未运行且有 GPU)
      - 自动检测已运行 server
      - 优雅关闭
    """

    name = "vllm"

    def __init__(
        self,
        host: str = "localhost",
        port: int = DEFAULT_VLLM_PORT,
        api_key: str = "goe@local",
        model_id: str = VLLM_MODEL_PATH,
    ):
        self.host = host
        self.port = port
        self.api_key = api_key
        self.model_id = model_id
        self._server_pid: Optional[int] = None  # 跟踪我们启动的 server
        self._base_url = f"http://{host}:{port}"

    @property
    def chat_url(self) -> str:
        return f"{self._base_url}/v1/chat/completions"

    @property
    def health_url(self) -> str:
        return f"{self._base_url}/health"

    # ── 生命周期 ──────────────────────────────────────────────

    def ensure_running(self) -> bool:
        """确保 vLLM server 正在运行。如果检测到已经运行的，直接使用。"""
        if self.health():
            return True
        # 尝试启动
        return self._start_server()

    def _start_server(self) -> bool:
        """启动 vLLM server（后台进程）。"""
        log_path = "/tmp/vllm_goedel_server.log"
        cmd = [
            "/home/shenli/miniconda3/bin/python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", self.model_id,
            "--gpu-memory-utilization", "0.85",
            "--max-model-len", "4096",
            "--dtype", "bfloat16",
            "--enforce-eager",
            "--trust-remote-code",
            "--api-key", self.api_key,
            "--port", str(self.port),
            "--disable-log-stats",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=open(log_path, "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._server_pid = proc.pid
            # 等待 server 就绪 (最多 120s)
            for _ in range(60):
                if self.health():
                    return True
                time.sleep(2)
            return False
        except Exception as e:
            print(f"[VLLMBackend] Failed to start server: {e}")
            return False

    def release(self):
        """停止我们启动的 server。如果是外部 server，不干涉。"""
        if self._server_pid is not None:
            try:
                os.kill(self._server_pid, 15)  # SIGTERM
                self._server_pid = None
            except ProcessLookupError:
                pass

    # ── 推理 ──────────────────────────────────────────────────

    def generate(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.6,
        n: int = 1,
    ) -> list[str]:
        import requests
        payload = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.95,
            "n": 1,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        texts = []
        for _ in range(n):
            resp = requests.post(
                self.chat_url, json=payload, headers=headers, timeout=300
            )
            resp.raise_for_status()
            texts.append(resp.json()["choices"][0]["message"]["content"])
        return texts

    def health(self) -> bool:
        import urllib.request
        try:
            req = urllib.request.Request(self.health_url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False


# ── Ollama Backend ────────────────────────────────────────────

class OllamaBackend(InferenceBackend):
    """通过 Ollama API 推理。

    支持:
      - WSL 本地 Ollama
      - Windows 宿主机 Ollama (/mnt/ 穿透)
      - 自动检测可用端点
    """

    name = "ollama"

    def __init__(self, model: str = "deepseek-coder-v2", host: Optional[str] = None):
        self.model = model
        # 自动检测最优端点
        self.host = host or self._discover_host()

    def _discover_host(self) -> str:
        """自动发现 Ollama 端点。"""
        candidates = [
            "http://localhost:11434",
            "http://host.docker.internal:11434",
        ]
        import urllib.request
        for host in candidates:
            try:
                req = urllib.request.Request(f"{host}/api/tags")
                with urllib.request.urlopen(req, timeout=2):
                    return host
            except Exception:
                continue
        return candidates[0]  # 默认

    def generate(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.6,
        n: int = 1,
    ) -> list[str]:
        import requests

        # 提取 system + user content
        system = ""
        user_content = ""
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            elif m["role"] == "user":
                user_content = m["content"]

        payload = {
            "model": self.model,
            "prompt": user_content,
            "system": system or None,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "top_p": 0.95,
            },
        }
        headers = {"Content-Type": "application/json"}

        texts = []
        for _ in range(n):
            resp = requests.post(
                f"{self.host}/api/generate",
                json=payload,
                headers=headers,
                timeout=300,
            )
            resp.raise_for_status()
            texts.append(resp.json().get("response", ""))
        return texts

    def health(self) -> bool:
        import urllib.request
        try:
            req = urllib.request.Request(f"{self.host}/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False


# ── Transformers Backend ──────────────────────────────────────

class TransformersBackend(InferenceBackend):
    """直接 PyTorch Transformers 推理（不通过服务端）。

    适用于:
      - 小模型快速测试
      - 无需服务端的场景
      - 模型未部署在 vLLM/Ollama 上
    """

    name = "transformers"

    def __init__(self, model_name_or_path: str = ""):
        self.model_name = model_name_or_path
        self._model = None
        self._tokenizer = None
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_name, trust_remote_code=True
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            device_map="auto",
            torch_dtype="auto",
            trust_remote_code=True,
        )
        self._loaded = True

    def generate(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.6,
        n: int = 1,
    ) -> list[str]:
        self._ensure_loaded()
        prompt = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        import torch
        inputs = self._tokenizer(prompt, return_tensors="pt").to(self._model.device)

        texts = []
        for _ in range(n):
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=max_tokens,
                    temperature=temperature,
                    top_p=0.95,
                    do_sample=True,
                    pad_token_id=self._tokenizer.pad_token_id or
                                 self._tokenizer.eos_token_id,
                )
            text = self._tokenizer.decode(
                outputs[0][inputs["input_ids"].shape[1]:],
                skip_special_tokens=True,
            )
            texts.append(text)
        return texts

    def health(self) -> bool:
        return self._loaded

    def release(self):
        """卸载模型释放 GPU 显存。"""
        self._model = None
        self._tokenizer = None
        self._loaded = False
        import torch
        torch.cuda.empty_cache()


# ── 自动路由 ──────────────────────────────────────────────────

BACKEND_REGISTRY = {
    "vllm": VLLMBackend,
    "ollama": OllamaBackend,
    "transformers": TransformersBackend,
}


def get_best_backend(
    snapshot: Optional[HardwareSnapshot] = None,
    model_name: str = "",
    prefer: str = "vllm",
) -> InferenceBackend:
    """自动选择最优可用后端。

    Priority:
      1. vLLM (如果 server 已运行)
      2. vLLM (如果有 GPU 可启动)
      3. Ollama (如果有运行中服务)
      4. Transformers (如果有 GPU)
    """
    if snapshot is None:
        snapshot = HardwareDetector().detect()

    # 1. vLLM already running
    if snapshot.vllm_server_alive:
        return VLLMBackend()

    # 2. vLLM can start (GPU available)
    if snapshot.gpus:
        bk = VLLMBackend()
        if bk.ensure_running():
            return bk

    # 3. Ollama
    if snapshot.ollama_alive or snapshot.ollama_windows_alive:
        return OllamaBackend(model=model_name or "deepseek-coder-v2")

    # 4. Transformers fallback
    if model_name:
        return TransformersBackend(model_name_or_path=model_name)
    if snapshot.gpus:
        return TransformersBackend()

    raise RuntimeError("No GPU or backend available")

#!/usr/bin/env python3
"""Simple OpenAI-compatible server for Goedel-Prover-V2 using HuggingFace Transformers.

Usage:
    python scripts/serve-goedel-simple.py          # 4-bit (default, ~6GB VRAM)
    python scripts/serve-goedel-simple.py --fp16   # FP16 (~16GB VRAM)

Then in another terminal:
    omega prove "theorem t : True :=" --model goedel/goedel-v2-8b
"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("goedel-server")

MODEL_ID = "Goedel-LM/Goedel-Prover-V2-8B"


class GoedelHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible chat completions handler."""

    model = None
    tokenizer = None
    device = None

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            self._handle_chat()
        elif self.path == "/v1/completions":
            self._handle_completion()
        elif self.path == "/health":
            self._json_response({"status": "ok", "model": MODEL_ID})
        else:
            self._json_response({"error": "not found"}, 404)

    def _handle_chat(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        messages = body.get("messages", [])
        prompt = self._build_prompt(messages)
        temperature = body.get("temperature", 0.3)
        max_tokens = body.get("max_tokens", 4096)

        output = self._generate(prompt, temperature, max_tokens)
        self._json_response({
            "id": "chatcmpl-goedel",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": output}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _handle_completion(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}
        prompt = body.get("prompt", "")
        temperature = body.get("temperature", 0.3)
        max_tokens = body.get("max_tokens", 4096)

        output = self._generate(prompt, temperature, max_tokens)
        self._json_response({
            "id": "cmpl-goedel",
            "object": "text_completion",
            "choices": [{"text": output, "finish_reason": "stop"}],
        })

    def _build_prompt(self, messages: list[dict]) -> str:
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.append(f"System: {content}")
            elif role == "user":
                parts.append(f"User: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
        return "\n".join(parts)

    @classmethod
    def _generate(cls, prompt: str, temperature: float = 0.3, max_tokens: int = 4096) -> str:
        if cls.model is None:
            return ""
        inputs = cls.tokenizer(prompt, return_tensors="pt").to(cls.device)
        with torch.no_grad():
            outputs = cls.model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                temperature=temperature,
                do_sample=temperature > 0,
                pad_token_id=cls.tokenizer.eos_token_id,
            )
        return cls.tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)

    def _json_response(self, data: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format: str, *args: str) -> None:
        logger.info(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--fp16", action="store_true", help="Load in FP16 (uses ~16GB VRAM)")
    parser.add_argument("--model", default=MODEL_ID)
    args = parser.parse_args()

    logger.info("Loading model %s...", args.model)
    if args.fp16:
        model = AutoModelForCausalLM.from_pretrained(
            args.model, device_map="auto", torch_dtype=torch.bfloat16,
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            args.model, device_map="auto", load_in_4bit=True,
        )
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    GoedelHandler.model = model
    GoedelHandler.tokenizer = tokenizer
    GoedelHandler.device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Model loaded on %s. Starting server on port %d...", GoedelHandler.device, args.port)

    server = HTTPServer(("0.0.0.0", args.port), GoedelHandler)
    logger.info("Server ready at http://localhost:%d/v1/chat/completions", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        server.server_close()


if __name__ == "__main__":
    main()

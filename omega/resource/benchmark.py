#!/usr/bin/env python3
"""Hardware benchmark: auto-detect local model throughput for time-based budget.

Detects Windows ollama (via WSL gateway IP), measures tok/s for each model,
caches results in ``~/.omega/benchmark.json``.

Usage::

    >>> from omega.resource.benchmark import load_benchmark
    >>> rates = load_benchmark()
    >>> rates.get_tok_s("deepseek-r1:8b")
    31.9
    >>> rates.estimate_12h_tokens("deepseek-r1:8b")
    1378080
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("omega.resource.benchmark")

# Lazy imports — langchain is optional for benchmark
_HAS_LANGCHAIN = False
try:
    from langchain_core.messages import HumanMessage
    from langchain_ollama import ChatOllama

    _HAS_LANGCHAIN = True
except ImportError:
    ChatOllama = None  # type: ignore[assignment]
    HumanMessage = None  # type: ignore[assignment]

BENCHMARK_PATH = Path.home() / ".omega" / "benchmark.json"
MODEL_MAP: dict[str, list[str]] = {
    # model_id_prefix → ollama model names to try (ordered by preference)
    "ollama/deepseek": ["deepseek-r1:8b"],
    "ollama/qwen3-coder": ["qwen3-coder:30b"],
    "ollama/qwen3": ["qwen3.6:latest"],
    "ollama/gemma4": ["gemma4:26b"],
}

# ── Default rates (conservative fallbacks when benchmark can't run) ──
# Based on typical RTX 4500 Ada + Q4_K_M quantization
FALLBACK_RATES: dict[str, float] = {
    "deepseek-r1:8b": 25.0,
    "qwen3-coder:30b": 20.0,
    "qwen3.6:latest": 15.0,
    "gemma4:26b": 50.0,
}

BENCHMARK_PROMPT = (
    "theorem add_comm (a b : Nat) : a + b = b + a := by\n"
    "  induction a with\n"
    "  | zero => simp\n"
    "  | succ a ih => simp [add_succ, ih]"
)


@dataclass
class BenchResult:
    """Benchmark result for one model."""

    avg_tok_s: float
    avg_tokens_per_call: int
    measured_at: str = ""
    hardware: str = ""


@dataclass
class BenchData:
    """Loaded benchmark data with convenience accessors."""

    models: dict[str, BenchResult] = field(default_factory=dict)
    measured_at: str = ""
    hardware: str = ""

    def get_tok_s(self, model_name_or_prefix: str) -> float:
        """Get measured tok/s for a model by name or prefix match."""
        # Exact match first
        if model_name_or_prefix in self.models:
            r = self.models[model_name_or_prefix]
            if r.avg_tok_s > 0:
                return r.avg_tok_s
        # Prefix match: "deepseek-r1:8b" or "deepseek"
        for name, r in self.models.items():
            if (model_name_or_prefix in name or name.startswith(model_name_or_prefix)) and r.avg_tok_s > 0:
                return r.avg_tok_s
        # Fallback
        return FALLBACK_RATES.get(model_name_or_prefix, 20.0)

    def estimate_tokens(self, model_name: str, time_s: float) -> int:
        """Estimate total tokens producible in *time_s* seconds."""
        tok_s = self.get_tok_s(model_name)
        return int(tok_s * time_s)

    def estimate_12h_tokens(self, model_name: str) -> int:
        """Estimate tokens producible in 12 hours."""
        return self.estimate_tokens(model_name, 43200)

    def to_dict(self) -> dict[str, Any]:
        return {
            "measured_at": self.measured_at,
            "hardware": self.hardware,
            "models": {
                k: {"avg_tok_s": v.avg_tok_s, "avg_tokens_per_call": v.avg_tokens_per_call}
                for k, v in self.models.items()
            },
        }

    def save(self) -> None:
        """Persist benchmark data to ``~/.omega/benchmark.json``."""
        BENCHMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        BENCHMARK_PATH.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchData:
        models = {}
        for k, v in d.get("models", {}).items():
            models[k] = BenchResult(
                avg_tok_s=v.get("avg_tok_s", 0),
                avg_tokens_per_call=v.get("avg_tokens_per_call", 0),
            )
        return cls(
            models=models, measured_at=d.get("measured_at", ""), hardware=d.get("hardware", "")
        )


# ── Detection ────────────────────────────────────────────────────


def detect_ollama_url() -> str:
    """Auto-detect Windows ollama URL via WSL gateway IP.

    Returns ``http://<gateway>:11434`` or empty string if unreachable.
    """
    try:
        gw = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.split()[2]
        url = f"http://{gw}:11434"
        # Quick ping
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3", f"{url}/api/tags"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and '"models"' in result.stdout:
            return url
    except Exception:
        pass
    return ""


def run_benchmark(model_name: str, ollama_url: str, num_trials: int = 3) -> BenchResult:
    """Run a small benchmark on a single ollama model using ChatOllama.

    Measures tok/s for a typical theorem-proving prompt (~50 tok output)
    using ``langchain_ollama.ChatOllama`` for consistent HTTP handling
    and token accounting.

    Falls back to ``FALLBACK_RATES`` when ``langchain-ollama`` is
    unavailable or the model is unreachable.
    """
    if not _HAS_LANGCHAIN:
        logger.warning(
            "langchain-ollama not installed — using fallback rate for %s "
            "(run: pip install langchain-ollama)",
            model_name,
        )
        return BenchResult(
            avg_tok_s=FALLBACK_RATES.get(model_name, 20.0),
            avg_tokens_per_call=50,
        )

    assert ChatOllama is not None  # pyright: ignore[reportOptionalCall]
    assert HumanMessage is not None  # pyright: ignore[reportOptionalCall]
    trials: list[dict[str, float]] = []
    for trial in range(num_trials):
        try:
            llm = ChatOllama(
                model=model_name,
                base_url=ollama_url,
                temperature=0.0,
                num_predict=256,
            )
            t0 = time.time()
            msg = llm.invoke([HumanMessage(content=BENCHMARK_PROMPT)])
            if msg is None:
                continue
            elapsed = time.time() - t0
            usage = msg.usage_metadata or {}
            tok_count = usage.get("output_tokens", 0) or msg.response_metadata.get("eval_count", 0)
            if tok_count > 0:
                trials.append(
                    {
                        "tokens": tok_count,
                        "elapsed_s": elapsed,
                        "tok_s": tok_count / elapsed,
                    }
                )
        except Exception as exc:
            logger.debug("Trial %d error for %s: %s", trial, model_name, exc)

    if not trials:
        logger.warning("No successful trials for %s — using fallback rate", model_name)
        return BenchResult(
            avg_tok_s=FALLBACK_RATES.get(model_name, 20.0),
            avg_tokens_per_call=50,
        )

    avg_tok_s = sum(t["tok_s"] for t in trials) / len(trials)
    avg_tokens = sum(t["tokens"] for t in trials) / len(trials)
    return BenchResult(
        avg_tok_s=round(avg_tok_s, 1),
        avg_tokens_per_call=round(avg_tokens),
    )


def discover_and_benchmark(ollama_url: str = "") -> BenchData:
    """Auto-discover Windows ollama and benchmark all relevant models.

    Args:
        ollama_url: Explicit ollama URL. Auto-detect if empty.

    Returns:
        ``BenchData`` with measured results + fallbacks for failed models.
    """
    url = ollama_url or detect_ollama_url()
    if not url:
        logger.warning("No local ollama detected — using fallback token rates")
        return _fallback_data()

    # Get model list from ollama
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "5", f"{url}/api/tags"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        available = set()
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for m in data.get("models", []):
                available.add(m["name"])
    except Exception:
        available = set()

    # Benchmark each relevant model
    models: dict[str, BenchResult] = {}
    ollama_models_to_bench = sorted(
        {names[0] for names in MODEL_MAP.values() if any(n in available for n in names)}
    )

    if not ollama_models_to_bench:
        # No matching models found — benchmark anything that's available
        ollama_models_to_bench = sorted(available)[:5]

    for ollama_name in ollama_models_to_bench:
        logger.info("Benchmarking %s ...", ollama_name)
        bench_result = run_benchmark(ollama_name, url)
        models[ollama_name] = bench_result
        logger.info("  → %.1f tok/s", bench_result.avg_tok_s)

    # Fill any missing with fallbacks
    for _, names in MODEL_MAP.items():
        for n in names:
            if n not in models:
                models[n] = BenchResult(
                    avg_tok_s=FALLBACK_RATES.get(n, 20.0),
                    avg_tokens_per_call=50,
                )

    return BenchData(
        models=models,
        measured_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        hardware=f"ollama@{url}",
    )


# ── Cache ────────────────────────────────────────────────────────


_bench_cache: BenchData | None = None


def load_benchmark(refresh: bool = False) -> BenchData:
    """Load cached benchmark data, optionally re-running.

    Args:
        refresh: If True, re-run benchmarks instead of loading cache.

    Returns:
        ``BenchData`` with tok/s rates for each known model.
    """
    global _bench_cache

    if refresh:
        data = discover_and_benchmark()
        data.save()
        _bench_cache = data
        return data

    if _bench_cache is not None:
        return _bench_cache

    if BENCHMARK_PATH.is_file():
        try:
            raw = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
            data = BenchData.from_dict(raw)
            _bench_cache = data
            return data
        except Exception as exc:
            logger.warning("Corrupt benchmark cache: %s", exc)

    # Run on first access
    data = discover_and_benchmark()
    data.save()
    _bench_cache = data
    return data


def reset_benchmark_cache() -> None:
    """Clear the benchmark cache (useful in tests)."""
    global _bench_cache
    _bench_cache = None


def _fallback_data() -> BenchData:
    models = {}
    for name, rate in FALLBACK_RATES.items():
        models[name] = BenchResult(avg_tok_s=rate, avg_tokens_per_call=50)
    return BenchData(models=models, measured_at="fallback", hardware="estimated")


# ── Convenience ──────────────────────────────────────────────────


def get_tok_s(model_id: str) -> float:
    """One-shot: get estimated tok/s for a model ID (e.g. ``ollama/deepseek-r1:8b``).

    Strips prefix and matches against benchmarked names.
    """
    data = load_benchmark()
    # Strip known prefixes
    for prefix in ["ollama/", "local/"]:
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix) :]
    return data.get_tok_s(model_id)

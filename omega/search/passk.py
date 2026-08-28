#!/usr/bin/env python3
"""OmegaPassKManager — unified pass@k measurement across 3 backends.

Backend routing:
  auto  → deepseek (if API key) → vllm (if local GPU) → ollama (fallback)
  deepseek → DeepSeek API with n=k + JSON mode
  vllm    → local vLLM with logprobs=16 beam ranking
  ollama  → local CPU fallback

Usage:
    from omega.search.passk import OmegaPassKManager

    mgr = OmegaPassKManager(backend="auto")
    report = mgr.run(theorem_header="theorem add_zero (n : ℕ) : n + 0 = n :=", k=8)
    print(report.pass_rate, report.best_proof)
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("omega.search.passk")


# ── Data models ─────────────────────────────────────────────────


@dataclass
class PassKCandidate:
    """A single candidate proof attempt in a pass@k measurement.

    Attributes
    ----------
    lean_code : str
        The complete Lean 4 code for this candidate.
    confidence : float
        Confidence score from the backend (0.0-1.0).
        - DeepSeek: LLM self-reported confidence (from JSON mode)
        - vLLM: normalized cumulative logprob
        - Ollama: fixed 0.5
    backend : str
        Which backend produced this candidate.
    verified : bool
        Whether T2 compilation passed.
    elapsed_s : float
        Time spent on this candidate.
    errors : list[str]
        T2 compilation errors (empty if verified).
    strategy : str
        Which strategy was used (induction, simp, calc, etc.)
    """
    lean_code: str
    confidence: float = 0.5
    backend: str = "auto"
    verified: bool = False
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)
    strategy: str = "unknown"


@dataclass
class PassKReport:
    """Report from a pass@k measurement.

    Attributes
    ----------
    theorem_header : str
        The theorem that was attempted.
    k : int
        Number of samples requested.
    n_passed : int
        Number of candidates that passed T2.
    pass_rate : float
        n_passed / k (nan if k=0).
    candidates : list[PassKCandidate]
        All candidates, ordered by confidence descending.
    best_proof : str or None
        The first T2-passing proof (or None).
    backend : str
        Which backend was used.
    total_elapsed_s : float
        Total wall time.
    cost_usd : float
        Estimated API cost (0 for local backends).
    k_values_tested : list[int]
        Which k values were measured (for cumulative reporting).
    """
    theorem_header: str = ""
    k: int = 0
    n_passed: int = 0
    pass_rate: float = 0.0
    candidates: list[PassKCandidate] = field(default_factory=list)
    best_proof: str | None = None
    backend: str = "auto"
    total_elapsed_s: float = 0.0
    cost_usd: float = 0.0
    k_values_tested: list[int] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """Whether at least one T2-passing proof was found."""
        return self.best_proof is not None

    @property
    def summary(self) -> str:
        """One-line summary."""
        if self.succeeded:
            return (
                f"[OK] pass@{self.k}={self.pass_rate:.1%} "
                f"({self.n_passed}/{self.k}) via {self.backend} "
                f"in {self.total_elapsed_s:.1f}s"
            )
        return (
            f"[FAIL] pass@{self.k}=0/{self.k} via {self.backend} "
            f"in {self.total_elapsed_s:.1f}s"
        )


# ── Backend detection ───────────────────────────────────────────


def _has_deepseek_key() -> bool:
    """Check if DeepSeek API key is available."""
    return bool(os.environ.get("DEEPSEEK_API_KEY") or
                os.environ.get("DEEPSEEK_API_KEY"))


def _has_vllm() -> bool:
    """Check if vLLM is available (installed + GPU)."""
    try:
        import vllm  # noqa: F401
        # Check if GPU is available
        import torch
        return torch.cuda.is_available()
    except (ImportError, Exception):
        return False


def _has_ollama() -> bool:
    """Check if Ollama is available."""
    try:
        import ollama  # noqa: F401
        # Quick connectivity check
        import urllib.request
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _has_vllm_server() -> bool:
    """Check if Goedel vLLM server is running at :8001."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:8001/health", method="GET")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


# ── Backend strategies ──────────────────────────────────────────


def _deepseek_strategy(prompt: str, k: int, compile_fn,
                       theorem_header: str = "") -> PassKReport:
    """DeepSeek API pass@k: n parallel calls + JSON mode + dedup → full T2."""
    from openai import OpenAI

    report = PassKReport(backend="deepseek", k=k)
    t0 = time.perf_counter()

    client = OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(k, 8)) as executor:
            futures = []
            for _ in range(k):
                futures.append(executor.submit(
                    client.chat.completions.create,
                    model="deepseek-v4-flash",
                    messages=[
                        {"role": "system", "content":
                         "You are proving a Lean 4 theorem. First reason step by step "
                         "about the proof strategy, then provide the complete Lean 4 code. "
                         "Output as JSON: {\"reasoning\": \"step-by-step plan\", "
                         "\"proof\": \"complete Lean 4 code\", \"confidence\": 0.0-1.0, \"strategy\": \"nlinarith|induction|simp|ring|omega|calc|aesop|cases\"}"},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    n=1,
                    temperature=0.7,
                    max_tokens=4096,
                ))
            responses = [f.result() for f in concurrent.futures.as_completed(futures)]
    except Exception as e:
        logger.error(f"DeepSeek API call failed: {e}")
        report.total_elapsed_s = time.perf_counter() - t0
        return report

    # Parse candidates — each response has 1 choice with a JSON proof object
    import json
    candidates: list[PassKCandidate] = []
    for response in responses:
        choice = response.choices[0]
        text = choice.message.content or "{}"
        try:
            data = json.loads(text)
            # Single object: {"proof": "...", "confidence": ..., "strategy": "..."}
            if isinstance(data, dict):
                candidates.append(PassKCandidate(
                    lean_code=data.get("proof", ""),
                    confidence=float(data.get("confidence", 0.5)),
                    backend="deepseek",
                    strategy=data.get("strategy", "unknown"),
                ))
            elif isinstance(data, list):
                for att in data:
                    candidates.append(PassKCandidate(
                        lean_code=att.get("proof", ""),
                        confidence=float(att.get("confidence", 0.5)),
                        backend="deepseek",
                        strategy=att.get("strategy", "unknown"),
                    ))
        except (json.JSONDecodeError, ValueError, TypeError):
            # Non-JSON response — wrap raw text as single candidate
            candidates.append(PassKCandidate(
                lean_code=text,
                confidence=0.5,
                backend="deepseek",
            ))

    # Dedup by lean_code before T2
    seen: set[str] = set()
    deduped: list[PassKCandidate] = []
    for c in candidates:
        key = c.lean_code.strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(c)
    candidates = deduped

    logger.info(
        "DeepSeek pass@%d: %d raw → %d unique after dedup",
        k, len(candidates) + len(seen) - len(deduped) if seen else k,
        len(candidates),
    )

    # T2 compile ALL candidates (no T1 filter — confidence is uncalibrated)
    for cand in candidates:
        t_start = time.perf_counter()
        try:
            formatted = _format_proof(cand.lean_code, theorem_header)
            t2_result = compile_fn(formatted)
            cand.verified = bool(t2_result.verified)
            cand.errors = list(getattr(t2_result, "errors", []))
        except Exception as e:
            cand.errors = [str(e)]
        cand.elapsed_s = time.perf_counter() - t_start
        if cand.verified and report.best_proof is None:
            report.best_proof = cand.lean_code

    report.candidates = candidates
    report.n_passed = sum(1 for c in candidates if c.verified)
    report.pass_rate = report.n_passed / max(k, 1)
    report.total_elapsed_s = time.perf_counter() - t0

    # Estimate cost: ~$0.15/1M input tokens, ~$0.60/1M output tokens (DeepSeek V4 pricing)
    total_chars = sum(len(c.lean_code) for c in candidates)
    report.cost_usd = (len(prompt) / 1e6 * 0.15) + (total_chars / 1e6 * 0.60)

    return report


def _goedel_local_strategy(prompt: str, k: int, compile_fn,
                           theorem_header: str = "") -> PassKReport:
    """Goedel local vLLM pass@k via OpenAI-compatible API at :8001.

    Uses the running vLLM server (Goedel-Prover-V2-8B) with Goedel's
    official CoT prompt format. Output is extracted from the last
    ```lean4 code block.

    Parameters as _deepseek_strategy.
    """
    import json
    import requests

    report = PassKReport(backend="goedel_local", k=k)
    t0 = time.perf_counter()

    API_URL = "http://localhost:8001/v1/chat/completions"
    API_KEY = "goe@local"
    MODEL = os.environ.get(
        "GOEDEL_MODEL",
        os.path.expanduser(
            "~/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B"
        )
        + "/snapshots/dfd02e6271a58375dfbf3ece0175277cf6b6a89a",
    )
    IMPORT_BLOCK = "import Mathlib\nimport Aesop\n\nset_option maxHeartbeats 0\n\nopen BigOperators Real Nat Topology Rat\n\n"

    # Build Goedel prompt
    formal = _goedel_formal_statement(theorem_header)
    goedel_prompt = (
        f"Complete the following Lean 4 code:\n\n"
        f"```lean4\n{formal}```\n\n"
        f"Before producing the Lean 4 code to formally prove the given theorem, "
        f"provide a detailed proof plan outlining the main proof steps and strategies. "
        f"The plan should highlight key ideas, intermediate lemmas, and proof structures "
        f"that will guide the construction of the final formal proof."
    )
    messages = [{"role": "user", "content": goedel_prompt}]

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.6,
        "top_p": 0.95,
        "n": 1,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    candidates: list[PassKCandidate] = []
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(k, 4)) as executor:
            futures = [executor.submit(requests.post, API_URL, json=payload, headers=headers, timeout=300)
                       for _ in range(k)]
            for fut in concurrent.futures.as_completed(futures):
                resp = fut.result()
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
                lean_code = _extract_goedel_code(raw, IMPORT_BLOCK)
                candidates.append(PassKCandidate(
                    lean_code=lean_code,
                    confidence=0.5,
                    backend="goedel_local",
                    strategy="goedel_cot",
                ))
    except Exception as e:
        logger.warning(f"Goedel local API failed after {k} calls: {e}")

    if not candidates:
        report.total_elapsed_s = time.perf_counter() - t0
        return report

    # Dedup
    seen: set[str] = set()
    deduped: list[PassKCandidate] = []
    for c in candidates:
        key = c.lean_code.strip()
        if key and key not in seen and key != "None":
            seen.add(key)
            deduped.append(c)
    candidates = deduped

    logger.info("Goedel local pass@%d: %d raw → %d unique", k,
                k - (k - len(candidates)), len(candidates))

    # T2 compile all
    for cand in candidates:
        t_start = time.perf_counter()
        try:
            formatted = _format_proof(cand.lean_code, theorem_header)
            t2_result = compile_fn(formatted)
            cand.verified = bool(t2_result.verified)
            cand.errors = list(getattr(t2_result, "errors", []))
        except Exception as e:
            cand.errors = [str(e)]
        cand.elapsed_s = time.perf_counter() - t_start
        if cand.verified and report.best_proof is None:
            report.best_proof = cand.lean_code

    report.candidates = candidates
    report.n_passed = sum(1 for c in candidates if c.verified)
    report.pass_rate = report.n_passed / max(k, 1)
    report.total_elapsed_s = time.perf_counter() - t0
    return report


def _goedel_formal_statement(header: str) -> str:
    """Convert theorem header to `:= by sorry` format (Goedel expects this)."""
    h = header.strip()
    if h.endswith(":="):
        return h + " by sorry"
    elif ":=" not in h:
        return h + " := by sorry"
    else:
        parts = h.split(":=", 1)
        return parts[0] + ":= by sorry" + parts[1]


def _extract_goedel_code(raw_text: str, import_block: str) -> str:
    """Extract last ```lean4 block from Goedel CoT output."""
    # Try ```lean4
    pattern = r'```lean4\n(.*?)\n```'
    matches = re.findall(pattern, raw_text, re.DOTALL)
    if matches:
        return import_block + matches[-1]
    # Try ```lean
    pattern = r'```lean\n(.*?)\n```'
    matches = re.findall(pattern, raw_text, re.DOTALL)
    if matches:
        return import_block + matches[-1]
    # Try ```lean4 without trailing newline
    pattern = r'```lean4\n(.*?)```'
    matches = re.findall(pattern, raw_text, re.DOTALL)
    if matches:
        return import_block + matches[-1]
    return "None"


def _hybrid_strategy(prompt: str, k: int, compile_fn,
                     theorem_header: str = "") -> PassKReport:
    """Hybrid: Goedel local (k=8) → DeepSeek API with error feedback.

    Layer 1: Goedel-Prover-V2 local at :8001, k_local=8, free.
    Layer 2: If all fail → DeepSeek API with T2 error context, k_api=4.

    total_k = k_local + k_api but the DeepSeek API call is only made
    when Goedel fails entirely.
    """
    import json
    from openai import OpenAI

    t0 = time.perf_counter()
    K_LOCAL = 4
    K_API = max(k - K_LOCAL, 4)

    # Layer 1: Goedel local
    report_local = _goedel_local_strategy(prompt, K_LOCAL, compile_fn, theorem_header)
    if report_local.succeeded:
        report_local.k = k  # report total k, not just local
        report_local.total_elapsed_s = time.perf_counter() - t0
        return report_local

    # Layer 2: DeepSeek with error feedback
    error_context = ""
    for c in report_local.candidates[:4]:
        if c.errors:
            snippet = c.lean_code[:200]
            err_text = "; ".join(c.errors[:3])
            error_context += f"  • {snippet}\n    Errors: {err_text}\n"

    enhanced_prompt = (
        f"{prompt}\n\n"
        f"[Previous local attempts failed with these errors]\n"
        f"{error_context}\n"
        f"Avoid these specific errors. Provide a correct Lean 4 proof."
    )

    client = OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )
    api_candidates: list[PassKCandidate] = []
    for _ in range(K_API):
        try:
            resp = client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[
                    {"role": "system", "content":
                     "You are proving a Lean 4 theorem. "
                     "Output as JSON: {\"proof\": \"...\", \"confidence\": 0.0-1.0, \"strategy\": \"...\"}"},
                    {"role": "user", "content": enhanced_prompt},
                ],
                response_format={"type": "json_object"},
                n=1, temperature=0.7, max_tokens=4096,
            )
            text = resp.choices[0].message.content or "{}"
            data = json.loads(text)
            if isinstance(data, dict):
                api_candidates.append(PassKCandidate(
                    lean_code=data.get("proof", ""),
                    confidence=float(data.get("confidence", 0.5)),
                    backend="deepseek_with_error",
                    strategy=data.get("strategy", "error_fix"),
                ))
        except Exception as e:
            logger.warning(f"Hybrid DeepSeek API call failed: {e}")

    # Dedup API candidates
    seen = set()
    deduped_api = []
    for c in api_candidates:
        key = c.lean_code.strip()
        if key and key not in seen:
            seen.add(key)
            deduped_api.append(c)

    # T2 compile API candidates
    for cand in deduped_api:
        t_start = time.perf_counter()
        try:
            formatted = _format_proof(cand.lean_code, theorem_header)
            t2_result = compile_fn(formatted)
            cand.verified = bool(t2_result.verified)
            cand.errors = list(getattr(t2_result, "errors", []))
        except Exception as e:
            cand.errors = [str(e)]
        cand.elapsed_s = time.perf_counter() - t_start

    n_api_pass = sum(1 for c in deduped_api if c.verified)

    # Combine: dedup across both layers
    all_candidates = report_local.candidates + deduped_api
    seen_combined = set()
    combined = []
    for c in all_candidates:
        key = c.lean_code.strip()
        if key and key not in seen_combined:
            seen_combined.add(key)
            combined.append(c)

    best = None
    for c in combined:
        if c.verified:
            best = c.lean_code
            break

    report = PassKReport(
        backend="hybrid",
        k=k,
        theorem_header=theorem_header,
        candidates=combined,
        best_proof=best,
        n_passed=n_api_pass + report_local.n_passed,
        total_elapsed_s=time.perf_counter() - t0,
    )
    report.pass_rate = report.n_passed / max(k, 1)
    return report


def _vllm_strategy(prompt: str, k: int, compile_fn,
                   theorem_header: str = "") -> PassKReport:
    """vLLM local pass@k: logprobs=16, use n=k batch sampling, rank by logprob.

    logprobs=16 adds ZERO extra GPU compute — the model always calculates
    the full [batch, vocab_size] logit matrix. Setting logprobs=16 just
    returns the top-16 instead of top-1; additional sorting <0.1ms/token.
    RTX 4500 Ada (24GB) can handle this for any k ≤ 32.
    """
    from vllm import LLM, SamplingParams

    report = PassKReport(backend="vllm", k=k)
    t0 = time.perf_counter()

    try:
        llm = LLM(model=os.environ.get("VLLM_MODEL", "deepseek-prover-v2-7b"),
                  gpu_memory_utilization=0.45,
                  max_model_len=4096)
    except Exception as e:
        logger.error(f"vLLM init failed: {e}")
        report.total_elapsed_s = time.perf_counter() - t0
        return report

    # Use batch sampling n=k for breadth coverage — NOT serial beam search.
    # Beam search is slower (serial forward passes per beam).
    # n=k runs k sequences in parallel on GPU (much faster).
    params = SamplingParams(
        n=k,
        temperature=0.7,
        top_p=0.95,
        max_tokens=1024,
        logprobs=16,           # Get top-16 logprobs (GPU cost ≈ zero)
    )

    try:
        outputs = llm.generate(prompt, params)
    except Exception as e:
        logger.error(f"vLLM generate failed: {e}")
        report.total_elapsed_s = time.perf_counter() - t0
        return report

    candidates: list[PassKCandidate] = []
    for output in outputs:
        for seq in output.outputs:
            # cumulative logprob = natural confidence
            cum_logprob = sum(logp for logp in seq.cumulative_logprob if logp < 0)
            # Normalize to 0-1 via sigmoid-like: confidence = 1/(1+e^(cum_logprob/len))
            # (more negative = more surprised = lower confidence)
            norm_conf = 1.0 / (1.0 + (abs(cum_logprob) / max(len(seq.token_ids), 1)) / 5.0)

            candidates.append(PassKCandidate(
                lean_code=seq.text.strip(),
                confidence=round(norm_conf, 3),
                backend="vllm",
                strategy="llm_generated",
            ))

    # Sort by confidence
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    # Dedup before T2
    seen = set()
    deduped = []
    for c in candidates:
        key = c.lean_code.strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(c)
    candidates = deduped

    # T2 compile ALL candidates
    for cand in candidates:
        t_start = time.perf_counter()
        try:
            t2_result = compile_fn(_format_proof(cand.lean_code, theorem_header))
            cand.verified = bool(t2_result.verified)
            cand.errors = list(getattr(t2_result, "errors", []))
        except Exception as e:
            cand.errors = [str(e)]
        cand.elapsed_s = time.perf_counter() - t_start
        if cand.verified and report.best_proof is None:
            report.best_proof = cand.lean_code

    report.candidates = candidates
    report.n_passed = sum(1 for c in t1_filtered if c.verified)
    report.pass_rate = report.n_passed / max(k, 1)
    report.total_elapsed_s = time.perf_counter() - t0

    return report


def _ollama_strategy(prompt: str, k: int, compile_fn,
                     theorem_header: str = "") -> PassKReport:
    """Ollama local fallback: serial CPU sampling, k ≤ 4 recommended."""
    import ollama

    report = PassKReport(backend="ollama", k=k)
    t0 = time.perf_counter()
    candidates: list[PassKCandidate] = []

    for _ in range(min(k, 4)):  # k ≤ 4 for CPU
        t_start = time.perf_counter()
        try:
            resp = ollama.chat(
                model=os.environ.get("OLLAMA_MODEL", "deepseek-prover-v2:7b"),
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.7},
            )
            code = resp.message.content.strip()
        except Exception as e:
            logger.warning(f"Ollama call failed: {e}")
            continue

        cand = PassKCandidate(lean_code=code, confidence=0.5,
                              backend="ollama",
                              elapsed_s=time.perf_counter() - t_start)
        # T2 compile
        try:
            t2r = compile_fn(_format_proof(code, theorem_header))
            cand.verified = bool(t2r.verified)
            cand.errors = list(getattr(t2r, "errors", []))
        except Exception as e:
            cand.errors = [str(e)]
        cand.elapsed_s = time.perf_counter() - t_start

        candidates.append(cand)
        if cand.verified and report.best_proof is None:
            report.best_proof = code

    report.candidates = candidates
    report.n_passed = sum(1 for c in candidates if c.verified)
    report.pass_rate = report.n_passed / max(k, 1)
    report.total_elapsed_s = time.perf_counter() - t0

    return report


# ── Main manager ────────────────────────────────────────────────


class OmegaPassKManager:
    """Unified pass@k measurement across 3 backends.

    Parameters
    ----------
    backend : str
        One of "auto" (default), "deepseek", "vllm", "ollama".
    compile_fn : callable or None
        T2 compile callback. If None, uses real_compile_callback.
        Signature: fn(lean_code: str) -> T2Result
    """

    def __init__(
        self,
        backend: str = "auto",
        compile_fn: Any | None = None,
    ):
        self.backend = self._resolve_backend(backend)
        self.compile_fn = compile_fn or self._default_compile_fn()

    def _resolve_backend(self, backend: str) -> str:
        if backend != "auto":
            return backend
        # Auto-detect: hybrid > deepseek > vllm > ollama
        if _has_deepseek_key() and _has_vllm_server():
            logger.info("Auto-selected backend: hybrid")
            return "hybrid"
        if _has_deepseek_key():
            logger.info("Auto-selected backend: deepseek")
            return "deepseek"
        if _has_vllm():
            logger.info("Auto-selected backend: vllm")
            return "vllm"
        if _has_ollama():
            logger.info("Auto-selected backend: ollama")
            return "ollama"
        logger.warning("No backend available — using mock (all passes fail)")
        return "mock"

    def _default_compile_fn(self):
        """Try to import real compile callback, wrapped with T2Result and formatting."""
        try:
            from omega.verify.t2_real import make_real_compile_callback
            from omega.verify.t2_lean import T2Result, format_code
            raw_compile = make_real_compile_callback()

            def _wrapped(lean_code: str, theorem_header: str = "") -> T2Result:
                # Format: if code is just a proof body, wrap with theorem header
                formatted = _format_proof(lean_code, theorem_header) if theorem_header else lean_code
                t0 = time.perf_counter()
                result = raw_compile(formatted)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                diagnostics = result.get("diagnostics", [])
                exit_code = result.get("exit_code", -1)
                errors: list[str] = []
                for d in diagnostics:
                    if d.get("severity") in ("error",):
                        msg = d.get("message", "")
                        line = d.get("line", 0)
                        errors.append(f"L{line}: {msg}" if line else msg)
                return T2Result(
                    verified=(exit_code == 0 and not errors),
                    errors=errors,
                    elapsed_ms=elapsed_ms,
                )
            return _wrapped
        except Exception:
            # Fallback: mock compile (always fails)
            from omega.verify.t2_lean import T2Result
            @dataclass
            class MockT2Result:
                verified: bool = False
                errors: list = field(default_factory=list)
                warnings: list = field(default_factory=list)

            def mock_compile(code: str):
                return MockT2Result()
            return mock_compile

    def run(
        self,
        theorem_header: str,
        k: int = 8,
    ) -> PassKReport:
        """Run pass@k measurement.

        Parameters
        ----------
        theorem_header : str
            Lean 4 theorem header, e.g. ``theorem add_zero (n : ℕ) : n + 0 = n :=``
        k : int
            Number of samples.

        Returns
        -------
        PassKReport
        """
        prompt = _build_prompt(theorem_header)

        backend_map = {
            "deepseek": _deepseek_strategy,
            "vllm": _vllm_strategy,
            "ollama": _ollama_strategy,
            "goedel": _goedel_local_strategy,
            "hybrid": _hybrid_strategy,
        }

        strategy = backend_map.get(self.backend)
        if strategy is None:
            # Mock backend
            report = PassKReport(theorem_header=theorem_header, k=k,
                                 backend="mock")
            report.total_elapsed_s = 0.0
            return report

        report = strategy(prompt, k, self.compile_fn,
                           theorem_header=theorem_header)
        report.theorem_header = theorem_header
        report.k_values_tested = [k]
        return report

    def run_cumulative(
        self,
        theorem_header: str,
        k_values: list[int] | None = None,
    ) -> dict[int, PassKReport]:
        """Run pass@k for multiple k values.

        Parameters
        ----------
        theorem_header : str
        k_values : list[int] or None
            Default: [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

        Returns
        -------
        dict[int, PassKReport]
            k → report
        """
        if k_values is None:
            k_values = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

        results = {}
        for k in k_values:
            report = self.run(theorem_header, k=k)
            results[k] = report
            logger.info(f"  pass@{k}={report.pass_rate:.1%} ({report.backend})")

        return results


# ── Helpers ─────────────────────────────────────────────────────



# ── Few-shot proof schemas (for enhanced prompt) ────────────────
FEW_SHOT_SCHEMAS = [
    {
        "header": "theorem mathd_algebra_148 (x : ℝ) : x * (-2) + 8 = x :=",
        "proof": "by nlinarith",
        "strategy": "nlinarith"
    },
    {
        "header": "theorem algebra_absxm1pabsxpabsxp1eqxp2_0leqxleq1 (x : ℝ) (h0 : 0 ≤ x) (h1 : x ≤ 1) : |x - 1| + |x| + |x + 1| = x + 2 :=",
        "proof": "by\n  have hx_nonneg : 0 ≤ x := h0\n  have hx_le1 : x ≤ 1 := h1\n  rw [abs_of_nonpos (sub_nonpos.mpr hx_le1), abs_of_nonneg hx_nonneg, abs_of_nonneg (by nlinarith : 0 ≤ x + 1)]\n  nlinarith",
        "strategy": "abs_simp"
    },
    {
        "header": "theorem aime_1983_p3 (x : ℝ) : x^2 + x + 1 > 0 :=",
        "proof": "by\n  have h : x^2 + x + 1 = (x + 1/2)^2 + 3/4 := by ring\n  rw [h]\n  nlinarith",
        "strategy": "calc_chain"
    },
]

_USE_ENHANCED_PROMPT = False  # toggle flag for experiments


def _build_enhanced_prompt(theorem_header: str) -> str:
    """Build prompt with few-shot schema examples."""
    schemas_block = "\n\n".join(
        f"Example {i+1} ({s['strategy']}):\n{s['header']}\n{s['proof']}"
        for i, s in enumerate(FEW_SHOT_SCHEMAS)
    )
    return (
        "You are a Lean 4 proof specialist. Study these correct proofs:\n\n"
        f"{schemas_block}\n\n"
        f"Now prove the following theorem. Always include `import Mathlib` if needed.\n\n"
        f"{theorem_header}\n\n"
        'Output as JSON: {"proof": "...", "confidence": 0.0-1.0, "strategy": "..."}'
    )


def _search_matlas(theorem_header: str, top_k: int = 3) -> str:
    """Disabled — search injection did not improve pass rates."""
    return ""


def _build_prompt(theorem_header: str) -> str:
    """Build the prompt for a theorem proving task."""
    if _USE_ENHANCED_PROMPT:
        return _build_enhanced_prompt(theorem_header)

    # Search Matlas for relevant mathematical context
    matlas_context = _search_matlas(theorem_header, top_k=3)

    prompt = (
        f"Prove the following Lean 4 theorem. "
        f"Always include `import Mathlib` if needed.\n\n"
        f"{theorem_header}\n\n"
        f"Provide the complete proof code."
    )

    if matlas_context:
        prompt = matlas_context + "\n\n" + prompt

    return prompt


def _format_proof(code: str, theorem_header: str) -> str:
    """Ensure the code is a valid Lean theorem, not just a proof body.

    If the code looks like a proof body (starts with ``by``, ``:=``,
    or a tactic), prepend the theorem header and ``:=``.

    If it's already a full theorem, return as-is (format_code will add
    ``import Mathlib`` if needed).
    """
    stripped = code.strip()
    if not stripped:
        return theorem_header + " :=\n  sorry"

    # Check if it's already a full theorem/def/lemma/example
    # Handle leading modifiers: private, protected, noncomputable, etc.
    starts_with_command = bool(
        re.search(
            r"^\s*(?:private\s+|protected\s+|noncomputable\s+)*"
            r"(theorem|lemma|def|example|instance|axiom|inductive|structure)\b",
            stripped,
        )
    )
    if starts_with_command:
        from omega.verify.t2_lean import format_code
        return format_code(stripped)

    # It's a proof body — wrap with theorem header
    # Strip trailing ":=" from header if present (headers often include it)
    header = theorem_header
    if header.strip().endswith(":="):
        header = header.strip()[:-2].strip()
    # Remove trailing ":=" if model output included it
    body = stripped
    if body.endswith(":="):
        body = body[:-2].strip()
    full = f"{header} :=\n{body}"
    from omega.verify.t2_lean import format_code
    return format_code(full)

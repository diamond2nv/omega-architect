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


# ── Backend strategies ──────────────────────────────────────────


def _deepseek_strategy(prompt: str, k: int, compile_fn,
                       theorem_header: str = "") -> PassKReport:
    """DeepSeek API pass@k: n=k + JSON mode + T1 filter to top-4 → T2."""
    from openai import OpenAI

    report = PassKReport(backend="deepseek", k=k)
    t0 = time.perf_counter()

    client = OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )

    try:
        # DeepSeek only supports n=1 per call — make k parallel calls
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(k, 8)) as executor:
            futures = []
            for _ in range(k):
                futures.append(executor.submit(
                    client.chat.completions.create,
                    model="deepseek-v4-flash",
                    messages=[
                        {"role": "system", "content":
                         "You are proving a Lean 4 theorem. "
                         "Output as JSON: {\"proof\": \"...\", \"confidence\": 0.0-1.0, \"strategy\": \"...\"}"},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    n=1,
                    temperature=0.7,
                    max_tokens=2048,
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

    # Sort by confidence descending
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    # T1 filter: only compile top-4 candidates (T2 is expensive)
    t1_filtered = candidates[:4]

    # T2 compile each
    for cand in t1_filtered:
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
    report.n_passed = sum(1 for c in t1_filtered if c.verified)
    report.pass_rate = report.n_passed / max(k, 1)
    report.total_elapsed_s = time.perf_counter() - t0

    # Estimate cost: ~$0.15/1M input tokens, ~$0.60/1M output tokens (DeepSeek V4 pricing)
    total_chars = sum(len(c.lean_code) for c in candidates)
    report.cost_usd = (len(prompt) / 1e6 * 0.15) + (total_chars / 1e6 * 0.60)

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

    # T1 filter: compile top-4
    t1_filtered = candidates[:4]
    for cand in t1_filtered:
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
        # Auto-detect: deepseek > vllm > ollama
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


def _build_prompt(theorem_header: str) -> str:
    """Build the prompt for a theorem proving task."""
    return (
        f"Prove the following Lean 4 theorem. "
        f"Always include `import Mathlib` if needed.\n\n"
        f"{theorem_header}\n\n"
        f"Provide the complete proof code."
    )


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
    starts_with_command = bool(
        re.search(r"^(theorem|lemma|def|example|instance|axiom|inductive|structure)",
                  stripped)
    )
    if starts_with_command:
        # Already a full theorem — let format_code handle imports
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

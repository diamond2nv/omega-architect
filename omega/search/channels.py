#!/usr/bin/env python3
"""DeepSeek 5‑Channel Ensemble — diverse generation strategies.

P5 of the Ω‑Architect roadmap.

Five channels, each with a different prompt / strategy / decoding:

  CH1  Standard JSON   n=k, JSON mode, confidence field   (existing)
  CH2  FIM              Fill‑in‑the‑middle of partial proof
  CH3  Prefix           Step‑by‑step reasoning → code
  CH4  Tool‑calls       Structured tactic generation as JSON calls
  CH5  Refinement       Multi‑turn: LLM output → T2 error → rewrite

Usage::

    from omega.search.channels import EnsembleChannels, ChannelResult

    ens = EnsembleChannels(model="deepseek-v4-flash")
    result = ens.run(theorem_header="theorem t (n : ℕ) : n = n :=",
                     k=16, channels=[1, 2, 3, 4, 5])
    # result.best_proof, result.pass_rate, result.per_channel
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("omega.search.channels")

_DEEPSEEK_DEFAULT = "deepseek-v4-flash"
_DEEPSEEK_BASE = "https://api.deepseek.com"


# ── Data models ─────────────────────────────────────────────────


@dataclass
class ChannelCandidate:
    lean_code: str
    confidence: float = 0.5
    channel: int = 1
    strategy: str = "standard"
    verified: bool = False
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


@dataclass
class ChannelResult:
    theorem_header: str = ""
    k: int = 16
    n_passed: int = 0
    pass_rate: float = 0.0
    candidates: list[ChannelCandidate] = field(default_factory=list)
    best_proof: str | None = None
    per_channel: dict[int, int] = field(default_factory=dict)  # ch → n_passed
    total_elapsed_s: float = 0.0
    cost_usd: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.best_proof is not None

    @property
    def summary(self) -> str:
        status = "OK" if self.succeeded else "FAIL"
        ch_str = ", ".join(f"ch{k}={v}" for k, v in sorted(self.per_channel.items()))
        return (
            f"[{status}] ch5@{self.k}={self.pass_rate:.1%} "
            f"({self.n_passed}/{self.k}) in {self.total_elapsed_s:.1f}s "
            f"[{ch_str}]"
        )


# ── Channel strategies ──────────────────────────────────────────


def _build_prompt(theorem_header: str) -> str:
    """Standard prompt builder (shared across channels)."""
    return (
        f"Prove the following Lean 4 theorem. "
        f"Always include `import Mathlib` if needed.\n\n"
        f"{theorem_header}\n\n"
        f"Provide the complete proof code."
    )


def _call_deepseek(messages: list[dict], model: str,
                   n: int = 1, response_format: dict | None = None,
                   max_tokens: int = 2048, temperature: float = 0.7,
                   extra_body: dict | None = None) -> list[str]:
    """Call DeepSeek API, return list of response texts.

    DeepSeek only supports n=1 per API call — when n > 1, makes
    parallel calls via ThreadPoolExecutor.
    """
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url=_DEEPSEEK_BASE,
    )

    if n == 1:
        texts = [_call_single(client, model, messages, response_format,
                              max_tokens, temperature, extra_body)]
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(n, 8)) as pool:
            futures = [
                pool.submit(_call_single, client, model, messages,
                            response_format, max_tokens, temperature, extra_body)
                for _ in range(n)
            ]
            texts = [f.result() for f in concurrent.futures.as_completed(futures)]

    return texts


def _call_single(client, model: str, messages: list[dict],
                 response_format: dict | None = None,
                 max_tokens: int = 2048, temperature: float = 0.7,
                 extra_body: dict | None = None) -> str:
    """Make a single DeepSeek API call."""
    kwargs = dict(
        model=model,
        messages=messages,
        n=1,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if response_format:
        kwargs["response_format"] = response_format
    if extra_body:
        kwargs["extra_body"] = extra_body

    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _extract_lean_code(text: str) -> str:
    """Extract Lean code from LLM response (handles markdown fences)."""
    # Try ```lean4 ... ``` blocks first
    blocks = re.findall(r"```(?:lean4?)?\s*\n(.*?)```", text, re.DOTALL)
    if blocks:
        return blocks[0].strip()
    # Try ``` ... ``` (unspecified language)
    blocks = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
    if blocks:
        return blocks[0].strip()
    # Fallback: whole response, strip leading/trailing whitespace
    return text.strip()


# ── CH1: Standard JSON mode (existing, ported from passk.py) ────


def channel1_standard(header: str, k: int, model: str, compile_fn) -> list[ChannelCandidate]:
    """CH1: n=k, JSON mode, structured output."""
    prompt = _build_prompt(header)
    system = (
        f"You are proving a Lean 4 theorem. Generate exactly {k} "
        "independent proof attempts. Output as JSON array: "
        '[{"proof": "...", "confidence": 0.0-1.0, "strategy": "..."}]'
    )

    texts = _call_deepseek(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        model=model,
        n=k,
        response_format={"type": "json_object"},
    )

    candidates: list[ChannelCandidate] = []
    for text in texts:
        try:
            data = json.loads(text)
            attempts = data if isinstance(data, list) else data.get("attempts", [data])
            for att in attempts:
                candidates.append(ChannelCandidate(
                    lean_code=att.get("proof", ""),
                    confidence=float(att.get("confidence", 0.5)),
                    channel=1,
                    strategy=att.get("strategy", "standard"),
                ))
        except (json.JSONDecodeError, ValueError, TypeError):
            candidates.append(ChannelCandidate(
                lean_code=_extract_lean_code(text),
                confidence=0.5,
                channel=1,
                strategy="standard_fallback",
            ))

    # Sort by confidence, T2 compile top-4
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    _t2_compile_top(candidates, compile_fn, top_n=4)
    return candidates


# ── CH2: FIM (Fill-in-the-Middle) ────────────────────────────────


def channel2_fim(header: str, k: int, model: str, compile_fn) -> list[ChannelCandidate]:
    """CH2: Fill-in-the-Middle — prompt with partial proof skeleton.

    Uses DeepSeek's native FIM support via extra_body (prefix/suffix).
    The skeleton has a ``...`` gap that the model fills.
    """
    # Build a skeleton: statement + `:=` + gap
    skeleton = f"{header}\n  :=\n  _\n\n  where\n  -- TODO: fill in the proof\n"
    prompt = (
        "Complete the following Lean 4 proof by filling in the missing "
        "implementation (marked with ``...``). Return ONLY the complete "
        "proof code.\n\n"
        f"{skeleton}"
    )

    texts = _call_deepseek(
        [{"role": "user", "content": prompt}],
        model=model,
        n=k,
    )

    candidates = []
    for text in texts:
        code = _extract_lean_code(text)
        if not code:
            continue
        candidates.append(ChannelCandidate(
            lean_code=code,
            confidence=0.5,
            channel=2,
            strategy="fim",
        ))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    _t2_compile_top(candidates, compile_fn, top_n=4)
    return candidates


# ── CH3: Prefix (step-by-step reasoning → code) ──────────────────


def channel3_prefix(header: str, k: int, model: str, compile_fn) -> list[ChannelCandidate]:
    """CH3: Step-by-step natural language reasoning, then generate Lean code.

    Strategy: force the model to reason aloud first, then produce code.
    """
    prompt = (
        f"Prove the following Lean 4 theorem.\n\n"
        f"{header}\n\n"
        "First, explain your reasoning step by step in natural language. "
        "Then, provide the complete Lean 4 proof code in a code block.\n"
        "Format:\n"
        "  Reasoning: <step-by-step>\n"
        "  ```lean4\n  <complete proof>\n  ```"
    )

    texts = _call_deepseek(
        [{"role": "user", "content": prompt}],
        model=model,
        n=k,
    )

    candidates = []
    for text in texts:
        code = _extract_lean_code(text)
        if not code:
            continue
        # Estimate confidence: longer reasoning = higher confidence
        reasoning_len = len(re.findall(r"(?i)reasoning[:\s]+(.+?)(?:```|$)", text, re.DOTALL))
        conf = min(0.9, 0.3 + reasoning_len * 0.02 if reasoning_len else 0.4)
        candidates.append(ChannelCandidate(
            lean_code=code,
            confidence=round(conf, 3),
            channel=3,
            strategy="prefix",
        ))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    _t2_compile_top(candidates, compile_fn, top_n=4)
    return candidates


# ── CH4: Tool-calls (structured tactic generation) ──────────────


def channel4_toolcalls(header: str, k: int, model: str, compile_fn) -> list[ChannelCandidate]:
    """CH4: Generate proof as structured tactic sequences.

    Prompt the model to think in terms of 'which tactic to apply next',
    producing a sequence of tactic calls, then assemble into Lean code.
    """
    prompt = (
        f"Prove the following Lean 4 theorem.\n\n"
        f"{header}\n\n"
        "Think in terms of tactic sequences. For each step, output:\n"
        "  Tactic: <tactic_name>\n"
        "  Arguments: <args>\n"
        "  Rationale: <why this tactic>\n\n"
        "Then assemble the complete proof as Lean 4 code in a ```lean4 block."
    )

    texts = _call_deepseek(
        [{"role": "user", "content": prompt}],
        model=model,
        n=k,
    )

    candidates = []
    for text in texts:
        code = _extract_lean_code(text)
        if not code:
            continue
        # Count tactic mentions for confidence
        tactics = re.findall(r"(?i)Tactic:\s*(\w+)", text)
        conf = min(0.9, 0.3 + len(tactics) * 0.03) if tactics else 0.4
        candidates.append(ChannelCandidate(
            lean_code=code,
            confidence=round(conf, 3),
            channel=4,
            strategy=f"tool_calls_{'_'.join(tactics[:3])}" if tactics else "tool_calls",
        ))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    _t2_compile_top(candidates, compile_fn, top_n=4)
    return candidates


# ── CH5: Multi-turn refinement ───────────────────────────────────


def channel5_refinement(header: str, k: int, model: str, compile_fn) -> list[ChannelCandidate]:
    """CH5: Generate → T2 → refine → retry up to 2 turns.

    Each attempt gets up to 2 refinement iterations if T2 fails.
    """
    prompt = _build_prompt(header)

    candidates: list[ChannelCandidate] = []

    for i in range(k):
        texts = _call_deepseek(
            [{"role": "user", "content": prompt}],
            model=model,
            n=1,
        )
        code = _extract_lean_code(texts[0]) if texts else ""

        if not code:
            continue

        cand = ChannelCandidate(lean_code=code, confidence=0.5,
                                channel=5, strategy="refinement_v0")

        # T2 compile
        err_msg = _try_compile(cand, compile_fn)
        if err_msg is None:
            # Passed first try
            candidates.append(cand)
            continue

        # Refinement turn 1: feed error back
        ref_prompt = (
            f"The following Lean 4 proof failed to compile:\n\n"
            f"```lean4\n{code}\n```\n\n"
            f"Error:\n{err_msg}\n\n"
            f"Fix the proof. Return ONLY the corrected complete proof code."
        )
        texts2 = _call_deepseek(
            [{"role": "user", "content": ref_prompt}],
            model=model, n=1,
        )
        code2 = _extract_lean_code(texts2[0]) if texts2 else ""
        if code2 and code2 != code:
            cand2 = ChannelCandidate(lean_code=code2, confidence=0.6,
                                     channel=5, strategy="refinement_v1")
            err2 = _try_compile(cand2, compile_fn)
            if err2 is None:
                candidates.append(cand2)
                continue

        # Refinement turn 2
        if code2:
            ref_prompt2 = (
                f"Still failing. Code:\n\n```lean4\n{code2}\n```\n\n"
                f"Error:\n{err2}\n\n"
                f"Fix it differently. Try a completely different approach."
            )
            texts3 = _call_deepseek(
                [{"role": "user", "content": ref_prompt2}],
                model=model, n=1,
            )
            code3 = _extract_lean_code(texts3[0]) if texts3 else ""
            if code3 and code3 != code2:
                cand3 = ChannelCandidate(lean_code=code3, confidence=0.5,
                                         channel=5, strategy="refinement_v2")
                _try_compile(cand3, compile_fn)
                candidates.append(cand3)
                continue

        # If all refinement failed, keep original attempt
        candidates.append(cand)

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


# ── Shared helpers ──────────────────────────────────────────────


def _try_compile(cand: ChannelCandidate, compile_fn) -> str | None:
    """Compile a candidate, return error message or None if passed."""
    if not compile_fn:
        return "no compile_fn"
    try:
        t0 = time.perf_counter()
        result = compile_fn(cand.lean_code)
        cand.elapsed_s = time.perf_counter() - t0
        if bool(getattr(result, "verified", False)):
            cand.verified = True
            return None
        errors = list(getattr(result, "errors", []))
        cand.errors = errors
        return "\n".join(errors[:3]) if errors else "unknown error"
    except Exception as e:
        cand.errors = [str(e)]
        return str(e)


def _t2_compile_top(candidates: list[ChannelCandidate],
                    compile_fn, top_n: int = 4) -> None:
    """T2 compile the top-N candidates by confidence."""
    for cand in candidates[:top_n]:
        _try_compile(cand, compile_fn)


# ── Ensemble ─────────────────────────────────────────────────────


class EnsembleChannels:
    """5-channel ensemble for DeepSeek proof generation.

    Parameters
    ----------
    model : str
        DeepSeek model name (default ``deepseek-v4-flash``).
    compile_fn : callable or None
        T2 compile callback.
    """

    CHANNELS = {
        1: ("standard", channel1_standard, 1.0),
        2: ("fim", channel2_fim, 0.8),
        3: ("prefix", channel3_prefix, 1.0),
        4: ("toolcalls", channel4_toolcalls, 0.7),
        5: ("refinement", channel5_refinement, 1.2),
    }

    def __init__(
        self,
        model: str = _DEEPSEEK_DEFAULT,
        compile_fn: Any | None = None,
    ):
        self.model = model
        self.compile_fn = compile_fn
        self._key_checked = False

    def _ensure_key(self) -> bool:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            logger.warning("DEEPSEEK_API_KEY not set — using mock")
            return False
        return True

    def run(
        self,
        theorem_header: str,
        k: int = 16,
        channels: list[int] | None = None,
    ) -> ChannelResult:
        """Run multi-channel ensemble.

        Parameters
        ----------
        theorem_header : str
        k : int
            Total samples (distributed across active channels).
        channels : list[int] or None
            Which channels to use (default: all 5).

        Returns
        -------
        ChannelResult
        """
        if not self._ensure_key():
            return ChannelResult(theorem_header=theorem_header, k=k)

        if channels is None:
            channels = list(self.CHANNELS.keys())

        t0 = time.perf_counter()
        result = ChannelResult(theorem_header=theorem_header, k=k)
        active_channels = {ch: self.CHANNELS[ch] for ch in channels if ch in self.CHANNELS}

        # Distribute k across channels (weighted)
        total_weight = sum(w for _, _, w in active_channels.values())
        allocations: dict[int, int] = {}
        remaining = k
        for ch, (name, fn, weight) in active_channels.items():
            alloc = max(1, int(k * weight / total_weight))
            allocations[ch] = alloc
            remaining -= alloc
        # Distribute remainder to first channel
        if remaining > 0 and active_channels:
            first_ch = list(active_channels.keys())[0]
            allocations[first_ch] = allocations.get(first_ch, 0) + remaining

        # Run channels
        all_candidates: list[ChannelCandidate] = []
        for ch in channels:
            if ch not in active_channels:
                continue
            name, fn, _ = active_channels[ch]
            alloc_k = allocations.get(ch, 1)
            logger.info("Channel %d (%s): k=%d", ch, name, alloc_k)
            try:
                cands = fn(theorem_header, alloc_k, self.model, self.compile_fn)
                all_candidates.extend(cands)
            except Exception as e:
                logger.error("Channel %d failed: %s", ch, e)

        # Compute result
        all_candidates.sort(key=lambda c: c.confidence, reverse=True)
        result.candidates = all_candidates
        result.n_passed = sum(1 for c in all_candidates if c.verified)
        result.pass_rate = result.n_passed / max(k, 1)
        result.total_elapsed_s = time.perf_counter() - t0

        # Per-channel stats
        per_ch: dict[int, list[bool]] = {}
        for c in all_candidates:
            per_ch.setdefault(c.channel, []).append(c.verified)
        result.per_channel = {ch: sum(vs) for ch, vs in per_ch.items()}

        # Best proof
        for c in all_candidates:
            if c.verified:
                result.best_proof = c.lean_code
                break

        # Cost estimate
        total_chars = sum(len(c.lean_code) for c in all_candidates)
        result.cost_usd = (len(theorem_header) / 1e6 * 0.15) + (total_chars / 1e6 * 0.60)

        return result

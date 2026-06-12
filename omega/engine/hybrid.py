"""Mode C v2: Hybrid Proof Engine — Dialogue first, Sampling fallback.

Design (v2, refined from experiment findings)
----------------------------------------------
Experiment showed v1 (Sampling → Dialogue) was pure overhead:
- Phase 1 Sampling never found a proof directly
- All successes came from Phase 2 Dialogue
- Hybrid took 2× the time of pure Dialogue

v2 flips the order: Dialogue first, Sampling as second opinion only when stuck.

Pipeline
--------
    Phase 1: Dialogue (fast path, limited rounds)
        │
        ├── success → ✅ return proof
        └── stuck / dead-loop / diverging → enter Phase 2
    
    Phase 2: Sampling (second opinion)
        │
        ├── success → ✅ return proof
        └── all fail → extract best attempt(s)
    
    Phase 3: Second Dialogue (with best candidate + error context)
        │
        ├── success → ✅ return proof
        └── fail → return best result with diagnostics

Rationale
---------
- Easy theorems pass quickly in Phase 1 (3-5 rounds, ~30s), no overhead
- Medium theorems may pass in Phase 1 (5-20 rounds, ~90s)
- Hard theorems get stuck in Phase 1 → Phase 2 sampling provides
  fresh perspectives → Phase 3 injects the best new attempt
- Total cost bounded by: 2× Dialogue + 1× Sampling (still cheaper than
  old v1 order where Sampling always ran first)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from omega.loop.inner import inner_loop, InnerLoopConfig, InnerLoopResult
from omega.resource.config import BudgetConfig
from omega.resource.budget import BudgetTracker
from omega.resource.tracker import ConvergenceTracker

logger = logging.getLogger("omega.engine.hybrid")

# ── Stuck detection threshold ───────────────────────────────────

# If Phase 1 terminates with one of these reasons, trigger Phase 2
_STUCK_TERMINATIONS = {"stuck", "diverging", "dead_loop"}


# ── Config ──────────────────────────────────────────────────────


@dataclass
class HybridV2Config:
    """Configuration for Hybrid v2 (Dialogue-first + Sampling fallback).

    Parameters
    ----------
    phase1_rounds : int
        Max rounds for Phase 1 Dialogue (default 20).
        Easy theorems pass in 3-5; medium in 5-15.
    phase1_timeout_s : float
        Per-phase timeout in seconds (default 120).
    phase2_samples : int
        Number of sampling candidates (default 8).
    phase2_corrections : int
        Sampling self-correction rounds (default 2).
    phase3_rounds : int
        Max rounds for Phase 3 fallback Dialogue (default 30).
    budget_usd : float
        Total budget cap in USD (default 0.50).
    budget_time_s : float
        Total budget cap in seconds (default 300).
    """

    phase1_rounds: int = 20
    phase1_timeout_s: float = 120.0
    phase2_samples: int = 8
    phase2_corrections: int = 2
    phase3_rounds: int = 30
    budget_usd: float = 0.50
    budget_time_s: float = 300.0

    @property
    def phase1_dialogue_config(self) -> InnerLoopConfig:
        return InnerLoopConfig(
            max_rounds=self.phase1_rounds,
            budget_model_id="deepseek/deepseek-v4-flash",
        )

    @property
    def phase3_dialogue_config(self) -> InnerLoopConfig:
        return InnerLoopConfig(
            max_rounds=self.phase3_rounds,
            budget_model_id="deepseek/deepseek-v4-flash",
        )

    def budget_config(self) -> BudgetConfig:
        return BudgetConfig(
            max_cost_usd=self.budget_usd,
            max_time_s=self.budget_time_s,
            max_attempts=self.phase1_rounds
                        + self.phase2_samples * (self.phase2_corrections + 1)
                        + self.phase3_rounds,
        )


# ── Result ──────────────────────────────────────────────────────


@dataclass
class HybridV2Result:
    """Result from Hybrid v2 run.

    Attributes
    ----------
    success : bool
        Whether a passing proof was found.
    proof : str or None
        The passing Lean code, or None if failed.
    error : str or None
        Error message if failed.
    source : str
        Which phase succeeded: "phase1", "phase2", "phase3", or "failed".
    phase1_result : InnerLoopResult or None
        Raw result from Phase 1 Dialogue.
    phase2_result : dict or None
        Raw summary from Phase 2 Sampling.
    phase3_result : InnerLoopResult or None
        Raw result from Phase 3 Dialogue.
    n_attempts : int
        Total attempts across all phases.
    elapsed_s : float
        Total wall-clock time.
    stuck_reason : str or None
        Why Phase 1 triggered Phase 2 (if applicable).
    """

    success: bool = False
    proof: str | None = None
    error: str | None = None
    source: str = "failed"
    phase1_result: InnerLoopResult | None = None
    phase2_result: dict | None = None
    phase3_result: InnerLoopResult | None = None
    n_attempts: int = 0
    elapsed_s: float = 0.0
    stuck_reason: str | None = None

    @property
    def summary(self) -> str:
        if self.success:
            return (
                f"Hybrid v2 | {self.source} |"
                f" proof={len(self.proof or ''):,}b |"
                f" {self.n_attempts} attempts |"
                f" {self.elapsed_s:.1f}s"
            )
        return (
            f"Hybrid v2 | failed |"
            f" {self.n_attempts} attempts |"
            f" {self.elapsed_s:.1f}s |"
            f" {self.error or self.stuck_reason or 'unknown'}"
        )


# ── Helpers ─────────────────────────────────────────────────────


def _is_stuck(result: InnerLoopResult) -> tuple[bool, str | None]:
    """Check if an InnerLoopResult indicates the solver is stuck.

    Returns (is_stuck, reason).
    """
    if result.dead_loop:
        return True, result.termination or "dead_loop"
    if result.termination in _STUCK_TERMINATIONS:
        return True, result.termination
    if result.rounds >= 10 and not result.success:
        # Lots of rounds with no sign of convergence
        return True, "no_progress"
    return False, None


def _extract_best_attempt(attempts: list[dict]) -> str | None:
    """Extract best Lean code from sampling attempts."""
    if not attempts:
        return None
    for a in attempts:
        if a.get("verified") and a.get("lean_code"):
            return a["lean_code"]
    best = None
    best_n = float("inf")
    for a in attempts:
        code = a.get("lean_code")
        if not code:
            continue
        n_err = len(a.get("errors") or [])
        if n_err < best_n:
            best_n = n_err
            best = code
    return best


def _get_errors_for_code(attempts: list[dict], code: str) -> list[str]:
    """Get compile errors for a specific code block."""
    for a in attempts:
        if a.get("lean_code") == code:
            return a.get("errors") or []
    return []


# ── Phase 1: Dialogue (fast path) ───────────────────────────────


def _run_phase1(
    theorem_header: str,
    config: HybridV2Config,
) -> InnerLoopResult:
    """Run Phase 1: fast Dialogue with limited rounds."""
    return inner_loop(
        theorem_header=theorem_header,
        config=config.phase1_dialogue_config,
    )


# ── Phase 2: Sampling (second opinion) ─────────────────────────


def _run_phase2(
    theorem_header: str,
    config: HybridV2Config,
) -> dict[str, Any]:
    """Run Phase 2: GoedelProver sampling as second opinion."""
    from omega.prover.go_prover import GoedelProver
    from omega.verify import t2_real

    compile_fn = t2_real.make_real_compile_callback()
    prover = GoedelProver(
        compile_fn=compile_fn,
        num_samples=config.phase2_samples,
        max_correction_rounds=config.phase2_corrections,
    )

    t0 = time.perf_counter()
    result = prover.run(theorem_header=theorem_header)
    elapsed = time.perf_counter() - t0

    return {
        "proof": result.proof,
        "succeeded": result.succeeded,
        "attempts": result.attempts,
        "n_attempts": result.n_attempts,
        "n_passed": result.n_passed,
        "elapsed_s": elapsed,
    }


# ── Phase 3: Second Dialogue (with context) ─────────────────────


def _run_phase3(
    theorem_header: str,
    best_code: str,
    best_errors: list[str],
    phase1_info: str,
    config: HybridV2Config,
) -> InnerLoopResult:
    """Run Phase 3: Dialogue with rich context from Phases 1 + 2.

    Injects:
    - The Phase 1 conversation history (status, attempts made)
    - The best candidate from Phase 2 sampling
    - Compile errors from both phases
    """
    context_header = (
        f"{theorem_header}\n\n"
        f"[Previous attempts — Phase 1 Dialogue]\n"
        f"Status: {phase1_info}\n\n"
        f"[Best candidate from Phase 2 Sampling]\n"
        f"```lean4\n{best_code}\n```\n\n"
        f"Compile errors from best candidate:\n"
        f"{' '.join(best_errors[:5])}\n\n"
        f"You have fresh attempts to prove this theorem. "
        f"Consider the best candidate above as a starting point, "
        f"but feel free to rewrite entirely."
    )

    return inner_loop(
        theorem_header=context_header,
        config=config.phase3_dialogue_config,
    )


# ── Main entry ──────────────────────────────────────────────────


def run_hybrid_v2(
    theorem_header: str,
    config: HybridV2Config | None = None,
) -> HybridV2Result:
    """Run Hybrid v2 on a theorem: Dialogue first, Sampling fallback.

    Parameters
    ----------
    theorem_header : str
        The Lean 4 theorem header.
    config : HybridV2Config or None
        Configuration. Uses defaults if omitted.

    Returns
    -------
    HybridV2Result
        Result with proof (if found) and detailed phase diagnostics.
    """
    cfg = config or HybridV2Config()
    t_start = time.perf_counter()

    # ══════════════════════════════════════════════════════════════
    # Phase 1: Dialogue (fast path)
    # ══════════════════════════════════════════════════════════════
    logger.info("Hybrid v2 Phase 1: Dialogue (max %d rounds)", cfg.phase1_rounds)

    p1 = _run_phase1(theorem_header, cfg)
    result = HybridV2Result(
        phase1_result=p1,
        n_attempts=p1.rounds,
    )

    if p1.success and p1.code:
        result.success = True
        result.proof = p1.code
        result.source = "phase1"
        result.elapsed_s = time.perf_counter() - t_start
        logger.info("Hybrid v2 Phase 1 success (dialogue, %d rounds)", p1.rounds)
        return result

    # Check if we should enter Phase 2
    stuck, reason = _is_stuck(p1)
    if not stuck:
        # Phase 1 failed but not stuck — might just need more rounds
        # Return as-is with Dialogue's result
        result.error = p1.error or "Phase 1 Dialogue failed (not stuck)"
        result.elapsed_s = time.perf_counter() - t_start
        logger.info("Hybrid v2 Phase 1 failed but not stuck: %s", result.error)
        return result

    result.stuck_reason = reason

    # ══════════════════════════════════════════════════════════════
    # Phase 2: Sampling (second opinion)
    # ══════════════════════════════════════════════════════════════
    logger.info("Hybrid v2 Phase 2: Sampling (stuck: %s)", reason)

    p2 = _run_phase2(theorem_header, cfg)
    result.phase2_result = p2
    result.n_attempts += p2["n_attempts"]

    if p2["succeeded"] and p2["proof"]:
        result.success = True
        result.proof = p2["proof"]
        result.source = "phase2"
        result.elapsed_s = time.perf_counter() - t_start
        logger.info("Hybrid v2 Phase 2 success (sampling)")
        return result

    # ══════════════════════════════════════════════════════════════
    # Phase 3: Second Dialogue (with best candidate context)
    # ══════════════════════════════════════════════════════════════
    best_code = _extract_best_attempt(p2.get("attempts", []))
    if not best_code:
        # Sampling produced nothing useful — exit
        result.error = p1.error or "Both phases failed, no candidate from sampling"
        result.elapsed_s = time.perf_counter() - t_start
        logger.warning("Hybrid v2 Phase 2 produced no candidates")
        return result

    best_errors = _get_errors_for_code(p2.get("attempts", []), best_code)
    p1_info = (
        f"tried {p1.rounds} rounds, "
        f"termination={p1.termination}, "
        f"last_error={p1.error or 'none'}"
    )

    logger.info("Hybrid v2 Phase 3: Second Dialogue (injecting best candidate)")
    p3 = _run_phase3(theorem_header, best_code, best_errors, p1_info, cfg)
    result.phase3_result = p3
    result.n_attempts += p3.rounds

    if p3.success and p3.code:
        result.success = True
        result.proof = p3.code
        result.source = "phase3"
        logger.info("Hybrid v2 Phase 3 success (second dialogue)")
    else:
        result.error = p3.error or "All 3 phases failed"
        logger.info("Hybrid v2 all phases failed: %s", result.error)

    result.elapsed_s = time.perf_counter() - t_start
    return result

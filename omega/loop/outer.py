"""Outer Loop — Orchestrator for multi-theorem proving with re-plan.

Phase B architecture:
  Outer Loop (this module)
    ├── PlanManager — decompose theorem
    ├── FilePipeline — init .lean file + HashIndex
    ├── Inner Loop — per-theorem edit cycle
    ├── Re-plan — adjust strategy on failure, retry
    └── VerifierAgent — final verification

Usage:
    from omega.loop.outer import outer_loop, OuterLoopConfig

    config = OuterLoopConfig(max_replan_attempts=3)
    results = outer_loop(theorems, config)
    print(results.summary())
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from omega.loop.inner import inner_loop, InnerLoopConfig, InnerLoopResult
from omega.loop.verifier import VerifierAgent, Verdict
from omega.loop.file_pipeline import FilePipeline

logger = logging.getLogger("omega.loop.outer")


# ── Termination reasons that trigger re-plan ──────────────────

_REPLAN_TRIGGERS = {
    "stuck",           # ConvergenceTracker: errors not decreasing
    "diverging",       # ConvergenceTracker: errors increasing
    "verifier_failed", # Compile passed but Verifier found sorry
    "max_rounds",      # Hit round limit without proving
    "budget_exhausted",# BudgetTracker limit reached
    "dead_loop",       # Error class repeated 3+ times
}


@dataclass
class ReplanConfig:
    """Strategy adjustments for each re-plan attempt."""
    max_rounds_multiplier: float = 1.0
    parallel_candidates: int = 3
    enable_beam_search: bool = False
    enable_file_pipeline: bool = False
    enable_proof_sketch: bool = True
    adaptive_strategy: bool = False
    max_search_rounds: int = 3
    description: str = "default"


# Predefined re-plan strategies
_REPLAN_STRATEGIES = [
    ReplanConfig(
        description="default",
        max_rounds_multiplier=1.0,
        parallel_candidates=3,
        enable_beam_search=False,
        enable_file_pipeline=True,
        enable_proof_sketch=True,
        adaptive_strategy=False,
        max_search_rounds=3,
    ),
    ReplanConfig(
        description="beam_search_3x",
        max_rounds_multiplier=1.5,
        parallel_candidates=3,
        enable_beam_search=True,
        enable_proof_sketch=True,
        adaptive_strategy=True,
        max_search_rounds=4,
    ),
    ReplanConfig(
        description="deep_dive_5x",
        max_rounds_multiplier=2.0,
        parallel_candidates=5,
        enable_beam_search=True,
        enable_file_pipeline=True,
        enable_proof_sketch=True,
        adaptive_strategy=True,
        max_search_rounds=6,
    ),
    ReplanConfig(
        description="file_pipeline_fallback",
        max_rounds_multiplier=2.0,
        parallel_candidates=3,
        enable_beam_search=True,
        enable_file_pipeline=True,
        enable_proof_sketch=False,  # skip sketch, go straight to code
        adaptive_strategy=True,
        max_search_rounds=8,
    ),
]


@dataclass
class TheoremAttempt:
    """Record of one theorem attempt (may include re-plans)."""
    theorem_name: str
    theorem_header: str
    tier: str = "?"
    success: bool = False
    attempts: list[InnerLoopResult] = field(default_factory=list)
    final_termination: str = "unknown"
    total_rounds: int = 0
    total_cost: float = 0.0
    total_elapsed_s: float = 0.0

    def add_attempt(self, result: InnerLoopResult, elapsed_s: float):
        self.attempts.append(result)
        self.total_rounds += result.rounds
        self.total_cost += result.budget_used_cost
        self.total_elapsed_s += elapsed_s

    @property
    def n_attempts(self) -> int:
        return len(self.attempts)

    @property
    def last_termination(self) -> str:
        if self.attempts:
            return self.attempts[-1].termination
        return "unknown"

    def to_dict(self) -> dict:
        return {
            "name": self.theorem_name,
            "tier": self.tier,
            "success": self.success,
            "n_attempts": self.n_attempts,
            "termination": self.final_termination,
            "total_rounds": self.total_rounds,
            "total_cost": round(self.total_cost, 6),
            "total_elapsed_s": round(self.total_elapsed_s, 1),
        }


@dataclass
class OuterLoopResult:
    """Aggregate results from the outer loop."""
    n_theorems: int = 0
    n_succeeded: int = 0
    results: list[TheoremAttempt] = field(default_factory=list)
    total_elapsed_s: float = 0.0
    total_cost: float = 0.0

    def summary(self) -> str:
        lines = [
            "═" * 60,
            f"OUTER LOOP RESULTS",
            "═" * 60,
            f"  ✅ {self.n_succeeded}/{self.n_theorems} proved",
            f"  ⏱  {self.total_elapsed_s:.0f}s total",
            f"  💰 ${self.total_cost:.6f} total",
            "",
            f"  {'Tier':6s} {'Name':36s} {'Result':12s} {'Att':3s} {'Rnd':4s} {'Cost':>10s} {'Time':>6s}",
            f"  {'-'*6} {'-'*36} {'-'*12} {'-'*3} {'-'*4} {'-'*10} {'-'*6}",
        ]
        for r in self.results:
            icon = "✅" if r.success else "❌"
            c = f"${r.total_cost:.4f}"
            t = f"{r.total_elapsed_s:.0f}s"
            lines.append(
                f"  {r.tier:6s} {icon} {r.theorem_name:34s} {r.final_termination:12s} "
                f"{r.n_attempts:2d}a {r.total_rounds:3d}r {c:>10s} {t:>6s}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "n_theorems": self.n_theorems,
            "n_succeeded": self.n_succeeded,
            "total_elapsed_s": round(self.total_elapsed_s, 2),
            "total_cost": round(self.total_cost, 6),
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class OuterLoopConfig:
    """Configuration for the Outer Loop (Orchestrator).

    Attributes
    ----------
    max_replan_attempts : int
        Max times to retry a failed theorem with adjusted strategy (default: 3).
    base_max_rounds : int
        Base max_rounds for the first attempt (auto-scaled by theorem count).
    n_theorems : int
        Total theorems in the run (for auto-scaling max_rounds).
    budget_model_id : str
        Model ID for BudgetTracker pricing.
    compile_timeout : int
        Compile timeout in seconds per inner_loop call.
    """
    max_replan_attempts: int = 3
    base_max_rounds: int = 512
    n_theorems: int = 10
    budget_model_id: str = "deepseek/deepseek-v4-flash"
    compile_timeout: int = 60

    def auto_scale_rounds(self) -> int:
        """Auto-scale max_rounds based on theorem count.
        
        10 theorems → 512 rounds (deep exploration)
        100+ theorems → 64 rounds (cost-controlled)
        Formula: 512 * 10 / n, clamped to [64, 512]
        """
        return max(64, min(512, round(self.base_max_rounds * 10 / max(1, self.n_theorems))))


def _build_inner_config(outer_cfg: OuterLoopConfig, replan: ReplanConfig,
                        theorem_name: str) -> InnerLoopConfig:
    """Build InnerLoopConfig from OuterLoopConfig + ReplanConfig."""
    from omega.resource.model_registry import resolve_api_name

    base_rounds = outer_cfg.auto_scale_rounds()
    return InnerLoopConfig(
        max_rounds=max(8, int(base_rounds * replan.max_rounds_multiplier)),
        compile_timeout=outer_cfg.compile_timeout,
        # model is for DeepSeekClient API — must NOT have deepseek/ prefix
        model=resolve_api_name(outer_cfg.budget_model_id),
        # budget_model_id keeps the prefix so BudgetTracker prefix matching works
        budget_model_id=outer_cfg.budget_model_id,
        max_search_rounds=replan.max_search_rounds,
        adaptive_strategy=replan.adaptive_strategy,
        proof_sketch=replan.enable_proof_sketch,
        file_pipeline=replan.enable_file_pipeline,
        parallel_candidates=replan.parallel_candidates,
        run_verifier=True,
        goal_extraction=True,
        memory_processor=True,
    )


def outer_loop(
    theorem_headers: list[dict],
    config: OuterLoopConfig | None = None,
) -> OuterLoopResult:
    """Run the outer loop over a list of theorems.

    Args:
        theorem_headers: List of dicts with keys 'name', 'formal_statement', 'tier'.
        config: Outer loop configuration.

    Returns:
        OuterLoopResult with aggregated results.
    """
    cfg = config or OuterLoopConfig()
    cfg.n_theorems = len(theorem_headers)
    result = OuterLoopResult(n_theorems=len(theorem_headers))
    t_start = time.perf_counter()

    for i, theorem in enumerate(theorem_headers):
        name = theorem.get("name", f"theorem_{i}")
        header = theorem.get("formal_statement", "")
        tier = theorem.get("tier", "?")
        attempt = TheoremAttempt(theorem_name=name, theorem_header=header, tier=tier)

        t0 = time.perf_counter()
        logger.info("[%d/%d] %s (%s)", i + 1, len(theorem_headers), name, tier)

        for attempt_idx in range(cfg.max_replan_attempts + 1):
            replan = _REPLAN_STRATEGIES[min(attempt_idx, len(_REPLAN_STRATEGIES) - 1)]
            inner_cfg = _build_inner_config(cfg, replan, name)

            if attempt_idx > 0:
                logger.info("  Re-plan attempt %d/%d: %s",
                             attempt_idx, cfg.max_replan_attempts, replan.description)

            try:
                inner = inner_loop(
                    theorem_header=header,
                    theorem_name=name,
                    config=inner_cfg,
                    plan=None,  # bypass PlanManager for now
                )
                elapsed = time.perf_counter() - t0
                attempt.add_attempt(inner, elapsed)

                if inner.success:
                    attempt.success = True
                    attempt.final_termination = "proved"
                    break  # proved — no more re-plans

                # Check if we should re-plan
                if inner.termination not in _REPLAN_TRIGGERS:
                    attempt.final_termination = inner.termination
                    break  # non-retryable termination

            except Exception as e:
                elapsed = time.perf_counter() - t0
                logger.error("  Exception on %s: %s", name, e)
                inner = InnerLoopResult(
                    theorem_header=header,
                    success=False,
                    error=str(e),
                    termination="exception",
                )
                attempt.add_attempt(inner, elapsed)
                break  # exception — don't retry

        else:
            # All re-plan attempts exhausted
            attempt.final_termination = attempt.last_termination or "max_replans"

        # Update aggregate
        result.results.append(attempt)
        if attempt.success:
            result.n_succeeded += 1
        result.total_cost += attempt.total_cost

        icon = "✅" if attempt.success else "❌"
        logger.info("  %s %s (%da, %dr, $%.4f, %.1fs)",
                     icon, attempt.final_termination,
                     attempt.n_attempts, attempt.total_rounds,
                     attempt.total_cost, attempt.total_elapsed_s)

    result.total_elapsed_s = time.perf_counter() - t_start
    return result

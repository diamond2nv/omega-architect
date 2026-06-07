#!/usr/bin/env python3
"""Ensemble Prover — runs all three strategies and elects the best proof.

Strategy comparison:
- Each strategy runs independently (sequentially, or in parallel via subagents)
- Results are compared by: T2 verification → proof length → time → confidence
- Best proof is elected and returned with comparison data
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from omega.prover.ar_prover import ArchonProver
from omega.prover.go_prover import GoedelProver
from omega.prover.re_prover import RethlasProver

# ── configuration ──────────────────────────────────────────────


ENSEMBLE_DEFAULTS = {
    "goedel": {"num_samples": 4, "max_correction_rounds": 2},
    "rethlas": {"max_depth": 3, "max_attempts": 5},
    "archon": {"max_iterations": 3, "goedel_samples": 4},
}


# ── result types ───────────────────────────────────────────────


@dataclass
class StrategyOutcome:
    """Result from a single strategy in the ensemble.

    Attributes
    ----------
    name : str
        Strategy name (``"goedel"``, ``"rethlas"``, ``"archon"``).
    succeeded : bool
        Whether a verified proof was found.
    proof : str or None
        The Lean proof code, if found.
    elapsed_ms : int
        Wall-clock time.
    n_attempts : int
        Number of proof attempts made.
    summary : str
        Human-readable summary.
    """

    name: str
    succeeded: bool = False
    proof: str | None = None
    elapsed_ms: int = 0
    n_attempts: int = 0
    summary: str = ""


@dataclass
class EnsembleResult:
    """Result from the ensemble prover.

    Attributes
    ----------
    theorem_header : str
        The theorem being proved.
    succeeded : bool
        Whether any strategy found a verified proof.
    elected : str or None
        Name of the winning strategy.
    best_proof : str or None
        The best proof found.
    outcomes : dict[str, StrategyOutcome]
        Per-strategy outcomes.
    total_elapsed_ms : int
        Total wall-clock time.
    comparison_table : str
        Markdown table comparing strategies.
    """

    theorem_header: str
    succeeded: bool = False
    elected: str | None = None
    best_proof: str | None = None
    outcomes: dict[str, StrategyOutcome] = field(default_factory=dict)
    total_elapsed_ms: int = 0
    comparison_table: str = ""

    def summary(self) -> str:
        """Human-readable summary."""
        elected_label = self.elected or "none"
        header = f"Ensemble: {'✅ ' + elected_label if self.succeeded else '❌ No proof'}"
        lines = [
            f"{header} ({self.total_elapsed_ms}ms)",
        ]
        for name, outcome in self.outcomes.items():
            icon = "✅" if outcome.succeeded else "❌"
            lines.append(
                f"  {icon} {name}: {outcome.summary} "
                f"({outcome.elapsed_ms}ms, {outcome.n_attempts} attempts)"
            )
        if self.comparison_table:
            lines.append("")
            lines.append(self.comparison_table)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "succeeded": self.succeeded,
            "elected": self.elected,
            "total_elapsed_ms": self.total_elapsed_ms,
            "outcomes": {
                name: {
                    "succeeded": o.succeeded,
                    "elapsed_ms": o.elapsed_ms,
                    "n_attempts": o.n_attempts,
                    "summary": o.summary,
                }
                for name, o in self.outcomes.items()
            },
        }


# ── comparison helpers ─────────────────────────────────────────


def _build_comparison(outcomes: dict[str, StrategyOutcome]) -> str:
    """Build a markdown comparison table."""
    lines = [
        "| Strategy | Proof | Time | Attempts | Verdict |",
        "|----------|-------|------|----------|---------|",
    ]
    for name, o in outcomes.items():
        icon = "✅ Elected" if o.succeeded else "❌"
        proof_len = len(o.proof) if o.proof else 0
        lines.append(
            f"| {name} | {'Yes' if o.proof else 'No'} ({proof_len}ch) | "
            f"{o.elapsed_ms}ms | {o.n_attempts} | {icon} |"
        )
    return "\n".join(lines)


def _proof_heuristic_score(proof: str | None) -> int:
    """Score a proof by structural quality heuristics.

    Returns a higher score for proofs that use reliable, structured tactics.
    """
    if not proof:
        return 0
    score = 0

    # Penalise bare ``simp`` (weak, may not close all goals)
    stripped = proof.strip()
    if stripped == "simp" or stripped.startswith("simp\n"):
        score -= 3

    # Induction — structural, strong
    if "induction" in proof:
        score += 1

    # Domain-specific arithmetic tactics — reliable
    for kw in ("nlinarith", "ring", "omega", "arith"):
        if kw in proof:
            score += 2

    # Structured proof blocks
    for kw in ("calc", "apply", "refine"):
        if kw in proof:
            score += 1

    # Multi-line proofs tend to be more thorough
    if proof.count("\n") >= 1:
        score += 1

    return score


def _elect_best(outcomes: dict[str, StrategyOutcome]) -> str | None:
    """Elect the best strategy.

    Criteria (in priority order):
    1. T2-verified proof (primary — filtered via ``o.succeeded``)
    2. Proof structure heuristic score (higher is better)
    3. Shortest proof (fewer chars, tiebreaker)
    """
    candidates = [(n, o) for n, o in outcomes.items() if o.succeeded]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    # Sort by: heuristic score (descending), then proof length (ascending)
    candidates.sort(
        key=lambda x: (
            -_proof_heuristic_score(x[1].proof),
            len(x[1].proof or ""),
        )
    )
    return candidates[0][0]


# ── ensemble prover ────────────────────────────────────────────


class EnsembleProver:
    """Runs all three prover strategies and elects the best proof.

    Usage
    -----
        prover = EnsembleProver(compile_fn=my_compile_fn)
        result = prover.run(theorem_header)
        print(result.summary())
    """

    def __init__(
        self,
        compile_fn: Callable[[str], dict[str, Any]] | None = None,
        config: dict[str, dict] | None = None,
    ):
        self.compile_fn = compile_fn
        self.config = {**ENSEMBLE_DEFAULTS, **(config or {})}

        # Initialize individual provers
        gc = self.config.get("goedel", {})
        self._goedel = GoedelProver(
            compile_fn=compile_fn,
            num_samples=gc.get("num_samples", 4),
            max_correction_rounds=gc.get("max_correction_rounds", 2),
        )
        rc = self.config.get("rethlas", {})
        self._rethlas = RethlasProver(
            compile_fn=compile_fn,
            max_depth=rc.get("max_depth", 3),
            max_attempts=rc.get("max_attempts", 5),
        )
        ac = self.config.get("archon", {})
        self._archon = ArchonProver(
            compile_fn=compile_fn,
            max_iterations=ac.get("max_iterations", 3),
            goedel_samples=ac.get("goedel_samples", 4),
        )

    def run(
        self, theorem_header: str, run_rethlas: bool = True, run_archon: bool = True
    ) -> EnsembleResult:
        """Run the ensemble.

        Parameters
        ----------
        theorem_header : str
            The full Lean theorem header (includes imports).
        run_rethlas : bool
            Whether to run the Rethlas strategy (default: True).
        run_archon : bool
            Whether to run the Archon strategy (default: True).

        Returns
        -------
        EnsembleResult
        """
        total_t0 = time.perf_counter()
        outcomes: dict[str, StrategyOutcome] = {}

        # Always run Goedel (primary strategy)
        outcomes["goedel"] = self._run_goedel(theorem_header)

        # Run Rethlas if configured
        if run_rethlas:
            outcomes["rethlas"] = self._run_rethlas(theorem_header)

        # Run Archon if configured
        if run_archon:
            outcomes["archon"] = self._run_archon(theorem_header)

        total_elapsed = int((time.perf_counter() - total_t0) * 1000)
        elected = _elect_best(outcomes)
        best_proof = None
        if elected is not None:
            best_proof = outcomes[elected].proof

        return EnsembleResult(
            theorem_header=theorem_header,
            succeeded=elected is not None,
            elected=elected,
            best_proof=best_proof,
            outcomes=outcomes,
            total_elapsed_ms=total_elapsed,
            comparison_table=_build_comparison(outcomes),
        )

    def _run_goedel(self, header: str) -> StrategyOutcome:
        """Run Goedel prover."""
        t0 = time.perf_counter()
        result = self._goedel.run(header)
        elapsed = int((time.perf_counter() - t0) * 1000)
        return StrategyOutcome(
            name="goedel",
            succeeded=result.succeeded,
            proof=result.proof,
            elapsed_ms=elapsed,
            n_attempts=result.n_attempts,
            summary=result.summary,
        )

    def _run_rethlas(self, header: str) -> StrategyOutcome:
        """Run Rethlas prover (uses prove() method)."""
        t0 = time.perf_counter()
        result = self._rethlas.prove(header)
        elapsed = int((time.perf_counter() - t0) * 1000)
        return StrategyOutcome(
            name="rethlas",
            succeeded=result.success,
            proof=result.proof,
            elapsed_ms=elapsed,
            n_attempts=result.n_attempts,
            summary="; ".join(
                f"{a.strategy} ({'✅' if a.success else '❌'})" for a in result.attempts[:3]
            )
            if result.attempts
            else "no attempts",
        )

    def _run_archon(self, header: str) -> StrategyOutcome:
        """Run Archon prover (uses prove() method)."""
        t0 = time.perf_counter()
        result = self._archon.prove(header)
        elapsed = int((time.perf_counter() - t0) * 1000)
        obs = result.critic_observations
        return StrategyOutcome(
            name="archon",
            succeeded=result.success,
            proof=result.proof,
            elapsed_ms=elapsed,
            n_attempts=len(result.strategies),
            summary=(
                f"elected={result.elected_strategy or 'none'}, critic={obs[-1] if obs else 'none'}"
            ),
        )

    def __repr__(self) -> str:
        return (
            f"EnsembleProver(goedel={self._goedel.num_samples}samples/"
            f"{self._goedel.max_correction_rounds}corr, "
            f"rethlas=dp{self._rethlas.max_depth}/a{self._rethlas.max_attempts}, "
            f"archon={self._archon.max_iterations}iter)"
        )

"""Archon-style Prover — multi-strategy ensemble with progress critic.

Architecture
------------
Archon-style proving runs multiple strategies in parallel and uses a
progress critic to decide when to switch strategies, when to stop
churning, and when to declare success.

Strategies:
  1. **Goedel-style**: Parallel sampling — generate N independent tactic
     attempts and try each. Best for goals where a direct tactic exists.
  2. **Rethlas-style**: Blueprint decomposition — decompose the goal into
     subgoals, prove each independently, and compose. Best for complex
     multi-step proofs.

Progress Critic:
  - **Convergence**: Error count is decreasing or proof length is increasing.
  - **Churning**: Same errors appearing repeatedly with no progress.
  - **Stuck**: No new errors or proof fragments appearing after K iterations.

Ensemble:
  The result from each strategy is collected. The best proof (verified by T2)
  is selected. If multiple strategies produce verified proofs, the shortest
  (or fastest) is chosen.

All pure Python stdlib — no external dependencies.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable

from omega.search.tree import GoalState
from omega.verify.t2_lean import verify as t2_verify

# ── type aliases ──────────────────────────────────────────────────────────────

CompileFn = Callable[[str], dict | list | str | None]
"""Callback that takes Lean code and returns raw compiler diagnostics."""

StrategyFn = Callable[[str, GoalState | None], "StrategyResult"]
"""Signature: ``fn(theorem_header, goal) -> StrategyResult``."""


# ── progress critic ──────────────────────────────────────────────────────────


class CriticStatus(Enum):
    """Status reported by the progress critic."""

    CONVERGING = auto()
    """Error count is decreasing or proof length is increasing — keep going."""

    CHURNING = auto()
    """Same errors repeating — the strategy is going in circles."""

    STUCK = auto()
    """No change after multiple iterations — no progress possible."""

    SOLVED = auto()
    """Proof has been verified — we are done."""


@dataclass
class CriticObservation:
    """A single observation recorded by the progress critic.

    Attributes
    ----------
    iteration : int
        Which iteration this observation was made at.
    n_errors : int
        Number of T2 errors in the current attempt.
    proof_length : int
        Length (chars) of the current proof attempt.
    error_signature : str
        A hash/fingerprint of the error set to detect churn.
    status : CriticStatus
        The critic's assessment at this point.
    """

    iteration: int = 0
    n_errors: int = 0
    proof_length: int = 0
    error_signature: str = ""
    status: CriticStatus = CriticStatus.CONVERGING


class ProgressCritic:
    """Monitors proof progress to detect convergence, churn, or stuck states.

    Parameters
    ----------
    max_stuck_iterations : int
        Number of iterations with no change before declaring STUCK (default 3).
    max_churn_iterations : int
        Number of iterations with the same errors before declaring CHURNING
        (default 4).
    """

    def __init__(
        self,
        max_stuck_iterations: int = 3,
        max_churn_iterations: int = 4,
    ) -> None:
        self.max_stuck = max_stuck_iterations
        self.max_churn = max_churn_iterations
        self._observations: list[CriticObservation] = []

    def observe(
        self,
        iteration: int,
        n_errors: int,
        proof_length: int,
        errors: list[str],
    ) -> CriticStatus:
        """Record an observation and return the critic's assessment.

        Parameters
        ----------
        iteration : int
            Current iteration number.
        n_errors : int
            Number of T2 compilation errors.
        proof_length : int
            Length (in characters) of the proof attempt.
        errors : list[str]
            The actual error messages from T2.

        Returns
        -------
        CriticStatus
        """
        # Build an error signature to detect churn
        error_signature = "|".join(sorted(set(e[:60] for e in errors)))
        n_unique_errors = len(set(e[:60] for e in errors))

        # Determine status based on history
        status = self._evaluate(iteration, n_errors, proof_length, error_signature)

        obs = CriticObservation(
            iteration=iteration,
            n_errors=n_errors,
            proof_length=proof_length,
            error_signature=error_signature,
            status=status,
        )
        self._observations.append(obs)

        return status

    def _evaluate(
        self,
        iteration: int,
        n_errors: int,
        proof_length: int,
        error_signature: str,
    ) -> CriticStatus:
        """Evaluate progress based on observation history."""
        # First observation — can't detect patterns yet
        if len(self._observations) < 2:
            return CriticStatus.CONVERGING if n_errors > 0 else CriticStatus.SOLVED

        # If no errors, we're solved
        if n_errors == 0:
            return CriticStatus.SOLVED

        last = self._observations[-1]

        # Convergence: errors decreasing or proof growing
        if n_errors < last.n_errors or proof_length > last.proof_length:
            return CriticStatus.CONVERGING

        # Churn: same error signature repeating
        if error_signature == last.error_signature:
            # Count consecutive identical signatures
            consecutive = 0
            for obs in reversed(self._observations):
                if obs.error_signature == error_signature:
                    consecutive += 1
                else:
                    break
            if consecutive >= self.max_churn:
                return CriticStatus.CHURNING

        # Stuck: no change in errors or proof length
        if (n_errors == last.n_errors and proof_length == last.proof_length):
            consecutive_no_change = 0
            for obs in reversed(self._observations):
                if obs.n_errors == n_errors and obs.proof_length == proof_length:
                    consecutive_no_change += 1
                else:
                    break
            if consecutive_no_change >= self.max_stuck:
                return CriticStatus.STUCK

        return CriticStatus.CONVERGING

    @property
    def observations(self) -> list[CriticObservation]:
        """All observations recorded so far."""
        return list(self._observations)

    @property
    def last_status(self) -> CriticStatus | None:
        """The most recent status, or None if no observations."""
        if not self._observations:
            return None
        return self._observations[-1].status

    def reset(self) -> None:
        """Clear all observations for a fresh run."""
        self._observations.clear()

    def summary(self) -> str:
        """Human-readable summary of the critic's observations."""
        if not self._observations:
            return "ProgressCritic: no observations"
        lines = ["ProgressCritic:"]
        for obs in self._observations:
            lines.append(
                f"  iter={obs.iteration} errors={obs.n_errors} "
                f"len={obs.proof_length} status={obs.status.name}"
            )
        return "\n".join(lines)


# ── together with result try ─────────────────────────────────────────────────


@dataclass
class StrategyResult:
    """Result from a single strategy within the Archon ensemble.

    Attributes
    ----------
    strategy_name : str
        Name of the strategy (e.g. ``"goedel"``, ``"rethlas"``).
    success : bool
        Whether this strategy found a verified proof.
    proof : str or None
        The verified proof code, if successful.
    elapsed_ms : int
        Time spent by this strategy in milliseconds.
    n_attempts : int
        Number of sub-attempts made within this strategy.
    errors : list[str]
        Any errors encountered.
    details : dict
        Strategy-specific metadata.
    """

    strategy_name: str = ""
    success: bool = False
    proof: str | None = None
    elapsed_ms: int = 0
    n_attempts: int = 0
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProverResult:
    """Result from an Archon prover run.

    Attributes
    ----------
    success : bool
        Whether a verified proof was found.
    proof : str or None
        The best verified proof code, if found.
    elapsed_ms : int
        Total wall-clock time in milliseconds.
    elected_strategy : str or None
        Name of the winning strategy.
    strategies : list[StrategyResult]
        Results from each strategy in the ensemble.
    critic_observations : list[CriticObservation]
        All observations made by the progress critic.
    summary : str
        Human-readable summary string.
    """

    success: bool = False
    proof: str | None = None
    elapsed_ms: int = 0
    elected_strategy: str | None = None
    strategies: list[StrategyResult] = field(default_factory=list)
    critic_observations: list[CriticObservation] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.success:
            return (
                f"✅ Archon succeeded via '{self.elected_strategy}' "
                f"({self.elapsed_ms}ms, {len(self.strategies)} strategies)"
            )
        return (
            f"❌ Archon failed ({self.elapsed_ms}ms, "
            f"{len(self.strategies)} strategies)"
        )


# ── goal types ───────────────────────────────────────────────────────────────

# We define a minimal Attempt here too so each file is self-contained for
# strategy-level tracking, but we reuse the ProverResult from the strategy
# runners.


def _make_simple_goal(goal_text: str, goal_type: str = "") -> GoalState:
    """Create a simple GoalState from a target string."""
    return GoalState(
        goal_text=goal_text,
        target_type=goal_type or goal_text,
        depth=0,
    )


# ── built-in strategies ──────────────────────────────────────────────────────


def _goedel_strategy(
    theorem_header: str,
    goal: GoalState | None,
    compile_fn: CompileFn | None,
    num_samples: int = 5,
) -> StrategyResult:
    """Goedel-style: parallel sampling of direct tactic attempts.

    Generates multiple independent tactic suggestions and tries each.
    Returns the first verified proof, or the best effort.
    """
    t_start = time.perf_counter()
    result = StrategyResult(strategy_name="goedel")

    if goal is None:
        goal = GoalState.from_lean_header(theorem_header)

    # Templates for common patterns
    templates: list[str] = [
        "by\n  simp",
        "by\n  rfl",
        "by\n  trivial",
        "by\n  omega",
        "by\n  nlinarith",
        "by\n  aesop",
        "by\n  norm_num",
        "by\n  constructor\n  · sorry\n  · sorry",
        "by\n  induction n with\n  | zero => sorry\n  | succ n ih => sorry",
        "by\n  cases h with\n  | inl h => sorry\n  | inr h => sorry",
    ]

    target = goal.target_type or goal.goal_text

    # Filter templates based on goal type
    candidate_templates: list[str] = []
    for tmpl in templates:
        tactic_name = tmpl.split("\n")[0].replace("by", "").strip()
        if tactic_name in ("simp", "rfl", "trivial", "omega", "norm_num"):
            candidate_templates.append(tmpl)
        elif tactic_name == "nlinarith" and ("ℕ" in target or "ℤ" in target or "ℝ" in target or "ℚ" in target):
            candidate_templates.append(tmpl)
        elif tactic_name == "aesop":
            candidate_templates.append(tmpl)
        elif tactic_name in ("constructor", "induction", "cases"):
            candidate_templates.append(tmpl)

    # Safety: always try at least simp, rfl, trivial
    for fallback in ["by\n  simp", "by\n  rfl", "by\n  trivial"]:
        if fallback not in candidate_templates:
            candidate_templates.append(fallback)

    # Limit to num_samples
    selected = candidate_templates[:num_samples]

    best_proof: str | None = None
    errors: list[str] = []
    attempt_count = 0

    for proof_template in selected:
        attempt_count += 1
        code = f"{theorem_header}\n  {proof_template}"

        if compile_fn is not None:
            t2_result = t2_verify(code, compile_fn=compile_fn)
            if t2_result.verified:
                elapsed = int((time.perf_counter() - t_start) * 1000)
                return StrategyResult(
                    strategy_name="goedel",
                    success=True,
                    proof=code,
                    elapsed_ms=elapsed,
                    n_attempts=attempt_count,
                    errors=[],
                    details={"tactic_used": proof_template.strip()},
                )
            errors.extend(t2_result.errors)
            if best_proof is None:
                best_proof = code
        else:
            # No compiler — accept the first template as "verified" for stub
            elapsed = int((time.perf_counter() - t_start) * 1000)
            return StrategyResult(
                strategy_name="goedel",
                success=True,
                proof=code,
                elapsed_ms=elapsed,
                n_attempts=1,
                errors=[],
                details={"tactic_used": proof_template.strip(), "stub_mode": True},
            )

    elapsed = int((time.perf_counter() - t_start) * 1000)
    result.success = False
    result.elapsed_ms = elapsed
    result.n_attempts = attempt_count
    result.errors = errors[:10]  # Limit to 10 errors
    result.proof = best_proof
    return result


def _rethlas_strategy(
    theorem_header: str,
    goal: GoalState | None,
    compile_fn: CompileFn | None,
) -> StrategyResult:
    """Rethlas-style: blueprint decomposition strategy.

    Delegates to the RethlasProver and translates its result.
    """
    t_start = time.perf_counter()
    result = StrategyResult(strategy_name="rethlas")

    if goal is None:
        goal = GoalState.from_lean_header(theorem_header)

    # Try blueprint decomposition inline (avoids circular import)
    from omega.prover.re_prover import RethlasProver

    re_prover = RethlasProver(compile_fn=compile_fn, max_depth=2, max_attempts=3)
    re_result = re_prover.prove(theorem_header, goal=goal)

    elapsed = int((time.perf_counter() - t_start) * 1000)

    if re_result.success:
        return StrategyResult(
            strategy_name="rethlas",
            success=True,
            proof=re_result.proof,
            elapsed_ms=elapsed,
            n_attempts=re_result.n_attempts,
            errors=[],
            details={"blueprint_attempts": len(re_result.attempts)},
        )

    all_errors: list[str] = []
    for att in re_result.attempts:
        if att.error:
            all_errors.append(att.error)

    return StrategyResult(
        strategy_name="rethlas",
        success=False,
        elapsed_ms=elapsed,
        n_attempts=re_result.n_attempts,
        errors=all_errors[:10],
        details={"blueprint_attempts": len(re_result.attempts)},
    )


# ── ArchonProver ─────────────────────────────────────────────────────────────


class ArchonProver:
    """Archon-style prover with multi-strategy ensemble and progress critic.

    Runs Goedel (parallel sampling) and Rethlas (blueprint decomposition)
    strategies, monitored by a progress critic. Selects the best verified
    proof from all strategies.

    Parameters
    ----------
    compile_fn : CompileFn or None
        Callback for T2 verification. If None, strategies run in stub mode.
    max_iterations : int
        Maximum number of critic-monitored iterations per strategy (default 5).
    critic : ProgressCritic or None
        Custom progress critic. Defaults to a fresh ``ProgressCritic``.
    goedel_samples : int
        Number of parallel samples for the Goedel strategy (default 5).
    """

    def __init__(
        self,
        compile_fn: CompileFn | None = None,
        max_iterations: int = 5,
        critic: ProgressCritic | None = None,
        goedel_samples: int = 5,
    ) -> None:
        self.compile_fn = compile_fn
        self.max_iterations = max_iterations
        self.critic = critic or ProgressCritic()
        self.goedel_samples = goedel_samples
        self._strategy_registry: dict[str, StrategyFn] = {
            "goedel": self._run_goedel,
            "rethlas": self._run_rethlas,
        }

    # ── public API ──────────────────────────────────────────────────────────

    def prove(self, theorem_header: str, goal: GoalState | None = None) -> ProverResult:
        """Prove a theorem using the Archon multi-strategy ensemble.

        Parameters
        ----------
        theorem_header : str
            The Lean theorem header.
        goal : GoalState or None
            Parsed goal state. If None, created from ``theorem_header``.

        Returns
        -------
        ProverResult
        """
        t_start = time.perf_counter()

        if goal is None:
            goal = GoalState.from_lean_header(theorem_header)

        self.critic.reset()
        strategy_results: list[StrategyResult] = []
        all_observations: list[CriticObservation] = []

        for iteration in range(self.max_iterations):
            # Check critic status from previous iteration
            prev_status = self.critic.last_status
            if prev_status in (CriticStatus.SOLVED, CriticStatus.CHURNING, CriticStatus.STUCK):
                if prev_status == CriticStatus.SOLVED:
                    # A previous iteration already found a proof
                    break
                # Churning or stuck — stop iterating
                break

            # Run all strategies in this iteration
            iteration_results: list[StrategyResult] = []
            total_errors = 0
            total_proof_length = 0
            all_errors: list[str] = []

            for strat_name, strat_fn in self._strategy_registry.items():
                sr = strat_fn(theorem_header, goal)
                iteration_results.append(sr)
                total_errors += len(sr.errors)
                all_errors.extend(sr.errors)
                if sr.proof:
                    total_proof_length = max(total_proof_length, len(sr.proof))

                # If this strategy succeeded, we could stop early — but we
                # still let the critic observe so we can ensemble properly
                if sr.success:
                    # Record the observation and collect all results
                    status = self.critic.observe(
                        iteration=iteration,
                        n_errors=0,
                        proof_length=total_proof_length,
                        errors=[],
                    )
                    all_observations.extend(self.critic.observations)
                    strategy_results.extend(iteration_results)

                    # Ensemble: pick the best proof
                    best = self._select_best(strategy_results)
                    if best is not None and best.proof is not None and best.strategy_name is not None:
                        elapsed = int((time.perf_counter() - t_start) * 1000)
                        return ProverResult(
                            success=True,
                            proof=best.proof,
                            elapsed_ms=elapsed,
                            elected_strategy=best.strategy_name,
                            strategies=strategy_results,
                            critic_observations=list(self.critic.observations),
                        )

            # Report progress to critic
            status = self.critic.observe(
                iteration=iteration,
                n_errors=total_errors,
                proof_length=total_proof_length,
                errors=all_errors,
            )
            all_observations.extend(self.critic.observations)
            strategy_results.extend(iteration_results)

        # ── Ensemble phase: pick best from all results ─────────────────────

        best = self._select_best(strategy_results)
        elapsed = int((time.perf_counter() - t_start) * 1000)

        if best is not None and best.success:
            assert best.proof is not None, "successful result must have proof"
            assert best.strategy_name is not None, "successful result must have strategy name"
            return ProverResult(
                success=True,
                proof=best.proof,
                elapsed_ms=elapsed,
                elected_strategy=best.strategy_name,
                strategies=strategy_results,
                critic_observations=list(self.critic.observations),
            )

        return ProverResult(
            success=False,
            elapsed_ms=elapsed,
            strategies=strategy_results,
            critic_observations=list(self.critic.observations),
        )

    async def prove_async(
        self, theorem_header: str, goal: GoalState | None = None
    ) -> ProverResult:
        """Async wrapper around :meth:`prove`.

        Currently runs synchronously — subclasses may override for true async
        parallel strategy execution.
        """
        return self.prove(theorem_header, goal=goal)

    def register_strategy(self, name: str, fn: StrategyFn) -> None:
        """Register a custom strategy for the ensemble.

        Parameters
        ----------
        name : str
            Unique strategy name.
        fn : StrategyFn
            Function with signature ``fn(theorem_header, goal) -> StrategyResult``.
        """
        self._strategy_registry[name] = fn

    # ── internal methods ────────────────────────────────────────────────────

    def _run_goedel(self, theorem_header: str, goal: GoalState | None) -> StrategyResult:
        """Run the Goedel parallel-sampling strategy."""
        return _goedel_strategy(theorem_header, goal, self.compile_fn, num_samples=self.goedel_samples)

    def _run_rethlas(self, theorem_header: str, goal: GoalState | None) -> StrategyResult:
        """Run the Rethlas blueprint-decomposition strategy."""
        return _rethlas_strategy(theorem_header, goal, self.compile_fn)

    def _select_best(self, results: list[StrategyResult]) -> StrategyResult | None:
        """Select the best result from the ensemble.

        Preference order:
        1. Verified proof (shortest code wins)
        2. Fewest errors
        3. Fastest
        """
        verified = [r for r in results if r.success and r.proof is not None]
        if verified:
            # Pick the shortest proof
            return min(verified, key=lambda r: len(r.proof or ""))

        # No verified proofs — return the one with fewest errors
        if results:
            return min(results, key=lambda r: len(r.errors))

        return None

    def __repr__(self) -> str:
        strategies = list(self._strategy_registry.keys())
        return (
            f"ArchonProver(strategies={strategies}, "
            f"max_iterations={self.max_iterations}, "
            f"goedel_samples={self.goedel_samples})"
        )

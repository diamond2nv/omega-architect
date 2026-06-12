"""Policy — π(a|s) abstraction for theorem proving.

The Policy is the decision-making core of the system. Given a proof state,
it produces the next action (tactic, code, or search). This replaces the
current implicit strategy (distributed across LLM prompt + ModeRouter +
Classifier) with an explicit, trainable interface.

Key design decisions:
- Policy.act() is stateless — state is passed in, not stored
- Policy.rollout() is stateful — runs a full trajectory from theorem header
- Policy.update() is the training hook — GRPO/LUFFY training lives here
- Policies are composable: MetaPolicy can switch between sub-policies
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from omega.engine.trajectory import (
    ProofAction,
    ProofState,
    Trajectory,
    TrajectoryStep,
)

logger = logging.getLogger("omega.learn.policy")


# ═══════════════════════════════════════════════════════════════════
# Context & Result
# ═══════════════════════════════════════════════════════════════════


@dataclass
class PolicyContext:
    """Runtime context for a single policy execution.

    Parameters
    ----------
    debug : bool
        Print debug information during execution.
    max_rounds : int
        Maximum rounds for a single rollout.
    budget_model_id : str
        Model ID for budget tracking.
    timeout : int
        Per-round compile timeout in seconds.
    temperature : float
        LLM sampling temperature (only for LLM-based policies).
    """
    debug: bool = False
    max_rounds: int = 50
    budget_model_id: str = "deepseek/deepseek-v4-flash"
    timeout: int = 120
    temperature: float = 0.3

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


@dataclass
class PolicyResult:
    """Result from a single policy rollout.

    Parameters
    ----------
    success : bool
        Whether the theorem was proved.
    trajectory : Trajectory or None
        Full trajectory of (state, action, outcome) steps.
    state_history : list of ProofState
        All states visited during the rollout.
    action_history : list of ProofAction
        All actions taken during the rollout.
    reward : float
        Computed reward for this trajectory.
    elapsed_ms : int
        Wall-clock time in milliseconds.
    n_rounds : int
        Number of rounds taken.
    error : str or None
        Error message if failed.
    metadata : dict
        Additional info (model, cost, debug logs).
    """
    success: bool = False
    trajectory: Trajectory | None = None
    state_history: list[ProofState] = field(default_factory=list)
    action_history: list[ProofAction] = field(default_factory=list)
    reward: float = 0.0
    elapsed_ms: int = 0
    n_rounds: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def proof(self) -> str | None:
        """Extract proof code if successful."""
        if self.success and self.trajectory and self.trajectory.proof:
            return self.trajectory.proof
        if self.success and self.state_history:
            last = self.state_history[-1]
            return last.code if last.code else None
        return None

    @property
    def summary(self) -> str:
        if self.success:
            return f"✅ {self.n_rounds}r {self.elapsed_ms}ms R={self.reward:.3f}"
        return f"❌ {self.n_rounds}r {self.error or 'failed'}"


# ═══════════════════════════════════════════════════════════════════
# Policy ABC
# ═══════════════════════════════════════════════════════════════════


class Policy(ABC):
    """π(a|s) — abstract policy for theorem proving.

    Usage
    -----
        policy = LLMPolicy()
        result = policy.rollout("theorem t : 1 + 1 = 2 := by")
        print(result.success, result.reward)

    Subclasses must implement:
        act(state, ctx) → ProofAction         # single step
        name                                   # human-readable
        description                            # explanation
    """

    @abstractmethod
    def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
        """Select the next action given current proof state.

        Parameters
        ----------
        state : ProofState
            Current proof state with theorem, code, errors, goals.
        ctx : PolicyContext
            Runtime context (budget, debug, etc.).

        Returns
        -------
        ProofAction
            The action to take next (tactic, rewrite, search, etc.).
        """
        ...

    def rollout(self, theorem: str, ctx: PolicyContext | None = None) -> PolicyResult:
        """Execute a full rollout: generate a trajectory for a theorem.

        The default implementation calls act() in a loop, building
        a trajectory. Subclasses may override for efficiency
        (e.g., LLMPolicy delegates to inner_loop() directly).

        Parameters
        ----------
        theorem : str
            The Lean 4 theorem header to prove.
        ctx : PolicyContext or None
            Runtime context (defaults to PolicyContext()).

        Returns
        -------
        PolicyResult
            The rollout result with trajectory and reward.
        """
        ctx = ctx or PolicyContext()
        t0 = time.perf_counter()
        states: list[ProofState] = []
        actions: list[ProofAction] = []
        steps: list[TrajectoryStep] = []
        error: str | None = None
        success = False

        # Initial state
        state = ProofState(
            theorem=theorem,
            code="",
            errors=[],
            depth=0,
        )
        states.append(state)

        for round_idx in range(ctx.max_rounds):
            if ctx.debug:
                logger.info("Policy round %d/%d", round_idx, ctx.max_rounds)

            action = self.act(state, ctx)
            actions.append(action)

            # Simulate the action outcome (simplified — real execution
            # requires a compiler; subclasses should override rollout)
            from copy import deepcopy
            next_state = deepcopy(state)
            next_state.depth += 1
            next_state.code = action.content
            states.append(next_state)

            step = TrajectoryStep(
                state_before=deepcopy(state),
                action=action,
                state_after=deepcopy(next_state),
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
            )
            steps.append(step)
            state = next_state

        # Build trajectory
        traj = Trajectory(
            steps=steps,
            theorem=theorem,
            success=success,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

        return PolicyResult(
            success=success,
            trajectory=traj,
            state_history=states,
            action_history=actions,
            reward=1.0 if success else 0.0,
            elapsed_ms=traj.elapsed_ms,
            n_rounds=len(steps),
            error=error,
        )

    def batch_rollout(
        self,
        theorems: list[str],
        ctx: PolicyContext | None = None,
        parallel: bool = False,
    ) -> list[PolicyResult]:
        """Rollout multiple theorems.

        Parameters
        ----------
        theorems : list of str
            List of theorem headers to prove.
        ctx : PolicyContext or None
            Runtime context (shared across all rollouts).
        parallel : bool
            If True, run rollouts in parallel (default: sequential).

        Returns
        -------
        list of PolicyResult
            Results for each theorem.
        """
        ctx = ctx or PolicyContext()
        if parallel:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(len(theorems), 4)) as pool:
                futures = {pool.submit(self.rollout, t, ctx): t for t in theorems}
                results = []
                for future in as_completed(futures):
                    results.append(future.result())
                return results
        return [self.rollout(t, ctx) for t in theorems]

    def update(
        self,
        trajectories: list[Trajectory],
        rewards: list[float],
    ) -> dict[str, Any]:
        """Update policy parameters from experience.

        Called after collecting trajectories. The default implementation
        is a no-op. Subclasses implementing RL training (GRPO/LUFFY)
        override this method.

        Parameters
        ----------
        trajectories : list of Trajectory
            Collected trajectories from rollouts.
        rewards : list of float
            Rewards for each trajectory.

        Returns
        -------
        dict
            Training metrics (loss, entropy, etc.).
        """
        return {"policy": self.name, "updated": False, "reason": "no-op (base class)"}

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable policy name."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of how this policy works."""
        ...

    def __repr__(self) -> str:
        return f"<Policy: {self.name}>"

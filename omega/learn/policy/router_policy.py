"""RouterPolicy — wraps ModeRouter as a trainable Policy.

This policy delegates to the ModeRouter for strategy selection,
then executes the chosen strategy (DFS/Beam/Hybrid). It provides
a Policy interface over the existing routing infrastructure.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from omega.engine.trajectory import (
    ProofAction,
    ProofState,
    Trajectory,
    TrajectoryStep,
)
from omega.learn.policy.base import (
    Policy,
    PolicyContext,
    PolicyResult,
)

logger = logging.getLogger("omega.learn.policy.router")


class RouterPolicy(Policy):
    """Policy that uses ModeRouter for strategy selection.

    Wraps the existing ModeRouter as a Policy. This is useful for:
    - Comparing RouterPolicy vs LLMPolicy in experiments
    - Using ModeRouter as a fallback when LLMPolicy fails
    - Benchmarking routing decisions vs actual outcomes

    Parameters
    ----------
    mode : str
        Fixed mode override. If "auto", uses ModeRouter. If
        "dfs", "beam", or "hybrid", forces that strategy.
    """

    def __init__(self, mode: str = "auto") -> None:
        valid_modes = ("auto", "dfs", "beam", "hybrid")
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode '{mode}'. Choose from {valid_modes}")
        self._mode = mode
        self._n_calls = 0
        self._history: list[dict] = []

    @property
    def name(self) -> str:
        return f"RouterPolicy ({self._mode})"

    @property
    def description(self) -> str:
        if self._mode == "auto":
            return "ModeRouter auto-selection → DFS/Beam/Hybrid"
        return f"Forced strategy: {self._mode}"

    # ═══════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════

    def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
        """Select next action via ModeRouter.

        This is a simplified single-step version. For full rollouts,
        use rollout() which delegates to the chosen strategy.
        """
        from omega.engine.router import ModeRouter, RoutingContext, ResourceProfile
        router = ModeRouter()
        routing_ctx = RoutingContext(
            theorem_header=state.theorem,
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(routing_ctx)

        return ProofAction(
            type="strategy_switch",
            content=decision.strategy_name,
            confidence=decision.confidence,
            description=f"ModeRouter → {decision.strategy_name}: {decision.reasoning[:80]}",
            metadata={
                "scores": decision.scores,
                "reasoning": decision.reasoning,
            },
        )

    def rollout(self, theorem: str, ctx: PolicyContext | None = None) -> PolicyResult:
        """Run rollout using ModeRouter-selected strategy.

        Decision flow:
        1. ModeRouter selects strategy
        2. Strategy runs (DFS → inner_loop, Beam → GoedelProver, etc.)
        3. Results mapped to PolicyResult
        """
        from omega.engine.router import (
            ModeRouter,
            ResourceProfile,
            RoutingContext,
        )
        from omega.engine import get_strategy

        ctx = ctx or PolicyContext()
        t0 = time.perf_counter()
        self._n_calls += 1

        # Step 1: Route
        router = ModeRouter()
        routing_ctx = RoutingContext(
            theorem_header=theorem,
            resource_profile=ResourceProfile.API_ONLY,
        )
        decision = router.route(routing_ctx)

        # Override if fixed mode
        mode = decision.strategy_name if self._mode == "auto" else self._mode

        # Step 2: Execute
        try:
            strategy = get_strategy(mode)
            trajectory = strategy.run(theorem)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            success = trajectory.success

            result = PolicyResult(
                success=success,
                trajectory=trajectory,
                reward=1.0 if success else 0.0,
                elapsed_ms=elapsed_ms,
                n_rounds=trajectory.depth,
                error=None if success else "strategy failed",
                metadata={
                    "mode": mode,
                    "decision": decision.strategy_name,
                    "scores": decision.scores,
                    "reasoning": decision.reasoning,
                    "n_strategy_calls": self._n_calls,
                },
            )

            # Record history
            self._history.append({
                "theorem": theorem[:60],
                "mode": mode,
                "success": success,
                "elapsed_ms": elapsed_ms,
            })

            if ctx.debug:
                logger.info("RouterPolicy: %s → %s (%dms)", theorem[:40],
                            "✅" if success else "❌", elapsed_ms)
            return result

        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.error("RouterPolicy rollout failed: %s", e)
            return PolicyResult(
                success=False,
                error=str(e),
                elapsed_ms=elapsed_ms,
                metadata={"mode": mode, "exception": str(e)},
            )

    def update(
        self,
        trajectories: list[Trajectory],
        rewards: list[float],
    ) -> dict[str, Any]:
        """RouterPolicy training hook.

        ModeRouter weights are hard-coded; this update method records
        data for future weight tuning. When ModeRouter gains learnable
        weights (planned), this method will perform the update.
        """
        n = len(trajectories)
        metrics = {
            "policy": self.name,
            "updated": False,
            "n_trajectories": n,
            "mean_reward": sum(rewards) / max(n, 1),
            "n_success": sum(1 for t in trajectories if t.success),
            "note": "ModeRouter weights are hard-coded — update via weight tuning",
        }
        logger.info("RouterPolicy.update: %s", metrics)
        return metrics

    @property
    def history(self) -> list[dict]:
        """Rollout history for analysis."""
        return list(self._history)

    @property
    def stats(self) -> dict:
        """Simple statistics."""
        if not self._history:
            return {"n_calls": 0}
        n_success = sum(1 for h in self._history if h["success"])
        return {
            "n_calls": self._n_calls,
            "n_success": n_success,
            "success_rate": n_success / max(len(self._history), 1),
            "avg_elapsed_ms": sum(h["elapsed_ms"] for h in self._history) / max(len(self._history), 1),
        }

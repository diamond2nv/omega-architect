"""LLMPolicy — wraps existing inner_loop as a trainable Policy.

This is the primary policy for omega-architect. It uses the existing
inner_loop (DFS/Dialogue mode) as the rollout mechanism, mapping
its results to the Policy abstraction.

When LUFFY/GRPO training is active, this policy's update() method
performs LoRA fine-tuning on the underlying LLM.
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

logger = logging.getLogger("omega.learn.policy.llm")


class LLMPolicy(Policy):
    """Policy that uses an LLM (via inner_loop) for theorem proving.

    Wraps the existing omega.loop.inner.inner_loop() function as a
    Policy. This is a zero-cost abstraction — all existing behavior
    is preserved identically.

    Parameters
    ----------
    model_id : str
        Model ID for the LLM (default: deepseek/deepseek-v4-flash).
    use_sketch : bool
        Enable proof sketch phase (default: True).
    use_mcp : bool
        Enable MCP tools (leansearch, loogle, multi_attempt).
    use_error_memory : bool
        Enable ProofErrorMemory for cross-theorem fix reuse.
    """

    def __init__(
        self,
        model_id: str = "deepseek/deepseek-v4-flash",
        use_sketch: bool = True,
        use_mcp: bool = True,
        use_error_memory: bool = True,
    ) -> None:
        self._model_id = model_id
        self._use_sketch = use_sketch
        self._use_mcp = use_mcp
        self._use_error_memory = use_error_memory
        self._training_metrics: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "LLMPolicy"

    @property
    def description(self) -> str:
        parts = [
            f"LLM-based policy ({self._model_id})",
            "sketch" if self._use_sketch else "no-sketch",
            "mcp" if self._use_mcp else "no-mcp",
            "errmem" if self._use_error_memory else "no-errmem",
        ]
        return " | ".join(parts)

    def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
        """Select the next action given current state.

        This calls the LLM with the current state context. For full
        rollouts, use rollout() instead, which delegates to inner_loop.
        """
        # Build LLM prompt from state
        prompt = self._build_prompt(state)
        from omega.llm import resolve_generate_fn
        fn = resolve_generate_fn(model_id=self._model_id, temperature=ctx.temperature, max_tokens=4096)
        response = fn(prompt) if fn else "```lean4\n  sorry\n```"
        code = self._extract_code(response)

        return ProofAction(
            type="tactic",
            content=code or "",
            confidence=1.0 if code else 0.0,
            description=f"LLM generation (round {state.depth + 1})",
            metadata={"model": self._model_id, "raw_response": str(response)[:200]},
        )

    def rollout(self, theorem: str, ctx: PolicyContext | None = None) -> PolicyResult:
        """Full rollout via inner_loop.

        Delegates to the proven inner_loop() function, mapping its
        result to the Policy API. This is the zero-cost path — all
        existing compile/feedback/search/verifier logic is preserved.
        """
        from omega.loop.inner import inner_loop, InnerLoopConfig

        ctx = ctx or PolicyContext()
        cfg = InnerLoopConfig(
            max_rounds=ctx.max_rounds,
            compile_timeout=ctx.timeout,
            budget_model_id=ctx.budget_model_id,
            proof_sketch=self._use_sketch,
            error_memory=self._use_error_memory,
        )

        t0 = time.perf_counter()
        try:
            result = inner_loop(theorem, config=cfg)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)

            # Map InnerLoopResult → PolicyResult
            policy_result = self._map_result(result, theorem, elapsed_ms)
            if ctx.debug:
                logger.info("LLMPolicy rollout: %s", policy_result.summary)
            return policy_result

        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.error("LLMPolicy rollout failed: %s", e)
            return PolicyResult(
                success=False,
                error=str(e),
                elapsed_ms=elapsed_ms,
                n_rounds=0,
                metadata={"exception": str(e), "model": self._model_id},
            )

    def update(
        self,
        trajectories: list[Trajectory],
        rewards: list[float],
    ) -> dict[str, Any]:
        """Update the policy (LUFFY/GRPO training hook).

        Currently a no-op that records the data. Training integration
        (LoRA fine-tuning via LUFFY Mixed-Policy GRPO) is planned
        for W3+.

        Returns metrics that can be monitored during training.
        """
        n = len(trajectories)
        self._training_metrics = {
            "policy": self.name,
            "updated": False,
            "n_trajectories": n,
            "mean_reward": sum(rewards) / max(n, 1),
            "n_success": sum(1 for t in trajectories if t.success),
            "note": "Training not implemented yet — W3 LUFFY integration",
        }
        logger.info("LLMPolicy.update: %d trajectories, mean R=%.3f",
                     n, self._training_metrics["mean_reward"])
        return self._training_metrics

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _build_prompt(state: ProofState) -> str:
        """Build a prompt for the LLM from proof state."""
        lines = [f"Theorem: {state.theorem}"]
        if state.code:
            lines.append(f"\nCurrent code:\n{state.code}")
        if state.errors:
            lines.append(f"\nCompile errors:\n" + "\n".join(state.errors[:3]))
        if state.goals:
            lines.append(f"\nRemaining goals:\n" + "\n".join(state.goals[:3]))
        lines.append("\nProvide the next Lean 4 code or tactic:")
        return "\n".join(lines)

    @staticmethod
    def _extract_code(response: str) -> str | None:
        """Extract Lean code from LLM response."""
        import re
        for pat in [r'```lean4?\s*\n(.*?)```', r'```\s*\n(.*?)```']:
            m = re.search(pat, response, re.DOTALL)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def _map_result(
        result,
        theorem: str,
        elapsed_ms: int,
    ) -> PolicyResult:
        """Map InnerLoopResult to PolicyResult."""
        # Build action history from rounds
        actions = []
        states = [ProofState(theorem=theorem, code="", depth=0)]
        for i in range(min(result.rounds, 10)):  # limit to avoid explosion
            actions.append(ProofAction(
                type="tactic",
                content=result.code[:100] if result.code else "",
                description=f"round {i + 1}",
            ))
            states.append(ProofState(
                theorem=theorem,
                code=result.code or "",
                errors=([result.error] if result.error else []),
                depth=i + 1,
            ))

        # Build trajectory
        steps = []
        for i in range(len(actions)):
            step = TrajectoryStep(
                state_before=states[i],
                action=actions[i],
                state_after=states[min(i + 1, len(states) - 1)],
                elapsed_ms=elapsed_ms // max(len(actions), 1),
            )
            steps.append(step)

        traj = Trajectory(
            steps=steps,
            theorem=theorem,
            success=result.success,
            elapsed_ms=elapsed_ms,
        )

        return PolicyResult(
            success=result.success,
            trajectory=traj,
            state_history=states,
            action_history=actions,
            reward=1.0 if result.success else 0.0,
            elapsed_ms=elapsed_ms,
            n_rounds=result.rounds,
            error=result.error,
            metadata={
                "model": getattr(result, "_model_id", None),
                "termination": getattr(result, "termination", "unknown"),
                "cost": getattr(result, "budget_used_cost", 0.0),
            },
        )

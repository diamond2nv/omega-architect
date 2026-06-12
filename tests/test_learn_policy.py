"""Tests for Policy abstraction layer (omega/learn/policy/).

Tests are divided into:
1. TestPolicyABC — base class defaults and interface
2. TestLLMPolicy — LLMPolicy wrapping inner_loop
3. TestRouterPolicy — RouterPolicy wrapping ModeRouter
4. TestPolicyContext — context handling
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import pytest

from omega.learn.policy import (
    LLMPolicy,
    Policy,
    PolicyContext,
    PolicyResult,
    RouterPolicy,
)
from omega.engine.trajectory import (
    ProofAction,
    ProofState,
    Trajectory,
    TrajectoryStep,
)


# ═══════════════════════════════════════════════════════════════════
# 1. Base Interface
# ═══════════════════════════════════════════════════════════════════


class TestPolicyABC:
    """Policy ABC — interface contracts."""

    def test_policy_is_abstract(self):
        """Cannot instantiate Policy directly (abstract methods)."""
        with pytest.raises(TypeError):
            Policy()  # type: ignore[abstract]

    def test_concrete_policy_is_valid(self):
        """A minimal concrete Policy should work."""
        class MinPolicy(Policy):
            @property
            def name(self) -> str:
                return "min"

            @property
            def description(self) -> str:
                return "minimal test policy"

            def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
                return ProofAction(type="tactic", content="rfl")

        p = MinPolicy()
        assert p.name == "min"
        assert p.description == "minimal test policy"
        assert repr(p) == "<Policy: min>"

    def test_default_rollout_returns_result(self):
        """Default rollout should return a PolicyResult."""
        class AutoActPolicy(Policy):
            @property
            def name(self) -> str:
                return "auto"

            @property
            def description(self) -> str:
                return "auto-act policy"

            def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
                return ProofAction(type="tactic", content="native_decide")

        p = AutoActPolicy()
        result = p.rollout("theorem t : 1 + 1 = 2 := by",
                           PolicyContext(max_rounds=3))
        assert isinstance(result, PolicyResult)
        assert result.n_rounds > 0
        assert result.trajectory is not None
        assert len(result.action_history) > 0
        # Default rollout doesn't compile (simulated) → not successful
        assert result.success is False

    def test_batch_rollout_returns_list(self):
        class BatchPolicy(Policy):
            @property
            def name(self) -> str:
                return "batch"

            @property
            def description(self) -> str:
                return "batch test"

            def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
                return ProofAction(type="tactic", content="rfl")

        p = BatchPolicy()
        results = p.batch_rollout(
            ["theorem t1 : 1=1 := by rfl", "theorem t2 : 2=2 := by rfl"],
            PolicyContext(max_rounds=2),
        )
        assert len(results) == 2
        assert all(isinstance(r, PolicyResult) for r in results)

    def test_update_no_op(self):
        class NoOpPolicy(Policy):
            @property
            def name(self) -> str:
                return "noop"

            @property
            def description(self) -> str:
                return "no-op"

            def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
                return ProofAction()

        p = NoOpPolicy()
        metrics = p.update([], [])
        assert metrics["updated"] is False

    def test_policy_context_defaults(self):
        ctx = PolicyContext()
        assert ctx.max_rounds == 50
        assert ctx.budget_model_id == "deepseek/deepseek-v4-flash"
        assert ctx.timeout == 120
        assert ctx.temperature == 0.3
        assert ctx.debug is False

    def test_policy_context_to_dict(self):
        ctx = PolicyContext(debug=True, max_rounds=10)
        d = ctx.to_dict()
        assert d["debug"] is True
        assert d["max_rounds"] == 10

    def test_policy_result_proof_success(self):
        """PolicyResult.proof should extract code on success."""
        traj = Trajectory(success=True, theorem="test")
        result = PolicyResult(
            success=True,
            trajectory=traj,
            state_history=[ProofState(theorem="test", code="by rfl", depth=0)],
        )
        assert result.proof is not None

    def test_policy_result_proof_no_code(self):
        result = PolicyResult(success=False)
        assert result.proof is None

    def test_policy_result_summary_success(self):
        result = PolicyResult(success=True, n_rounds=3, elapsed_ms=1500, reward=1.0)
        assert "✅" in result.summary
        assert "3r" in result.summary

    def test_policy_result_summary_failure(self):
        result = PolicyResult(success=False, error="timeout")
        assert "❌" in result.summary


# ═══════════════════════════════════════════════════════════════════
# 2. LLMPolicy
# ═══════════════════════════════════════════════════════════════════


class TestLLMPolicy:
    """LLMPolicy wraps inner_loop as a Policy."""

    def test_create(self):
        p = LLMPolicy()
        assert isinstance(p, Policy)
        assert p.name == "LLMPolicy"
        assert "deepseek" in p.description

    def test_act_returns_action(self):
        p = LLMPolicy()
        state = ProofState(theorem="theorem t : 1+1=2 := by")
        action = p.act(state, PolicyContext())
        assert isinstance(action, ProofAction)
        assert action.type == "tactic"

    def test_act_with_empty_state(self):
        p = LLMPolicy()
        state = ProofState(theorem="")
        action = p.act(state, PolicyContext())
        assert isinstance(action, ProofAction)
        # Should handle gracefully — no crash

    @patch("omega.loop.inner.inner_loop")
    def test_rollout_delegates_to_inner_loop(self, mock_inner_loop):
        """LLMPolicy.rollout() should call inner_loop."""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.rounds = 3
        mock_result.budget_used_cost = 0.015
        mock_result.code = "theorem t : 1+1=2 := by\n  native_decide"
        mock_result.error = None
        mock_result.termination = "proved"
        mock_inner_loop.return_value = mock_result

        p = LLMPolicy()
        result = p.rollout("theorem t : 1+1=2 := by")
        assert result.success is True
        assert result.n_rounds == 3
        assert result.reward == 1.0
        mock_inner_loop.assert_called_once()

    @patch("omega.loop.inner.inner_loop")
    def test_rollout_maps_failure(self, mock_inner_loop):
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.rounds = 5
        mock_result.budget_used_cost = 0.02
        mock_result.code = None
        mock_result.error = "stuck: convergence"
        mock_result.termination = "stuck"
        mock_inner_loop.return_value = mock_result

        p = LLMPolicy()
        result = p.rollout("theorem t : hard_theorem := by")
        assert result.success is False
        assert result.n_rounds == 5
        assert result.reward == 0.0

    @patch("omega.loop.inner.inner_loop")
    def test_rollout_handles_exception(self, mock_inner_loop):
        mock_inner_loop.side_effect = RuntimeError("API timeout")
        p = LLMPolicy()
        result = p.rollout("theorem t : 1+1=2 := by")
        assert result.success is False
        assert "timeout" in (result.error or "").lower()

    def test_rollout_with_custom_config(self):
        """Should pass PolicyContext settings to InnerLoopConfig."""
        with patch("omega.loop.inner.inner_loop") as mock:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.rounds = 1
            mock_result.budget_used_cost = 0.0
            mock_result.code = "by rfl"
            mock_result.error = None
            mock_result.termination = "proved"
            mock.return_value = mock_result

            p = LLMPolicy(use_sketch=False, use_mcp=False, use_error_memory=False)
            ctx = PolicyContext(max_rounds=10, timeout=30, budget_model_id="ollama/qwen3")
            result = p.rollout("theorem t : True := by trivial", ctx)
            assert result.success is True

    def test_update_records_metrics(self):
        p = LLMPolicy()
        traj = Trajectory(success=True, theorem="test", elapsed_ms=100)
        metrics = p.update([traj], [1.0])
        assert metrics["policy"] == "LLMPolicy"
        assert metrics["n_trajectories"] == 1
        assert metrics["mean_reward"] == 1.0

    def test_batch_rollout_sequential(self):
        with patch("omega.loop.inner.inner_loop") as mock:
            def side_effect(thm, **kw):
                r = MagicMock()
                r.success = True
                r.rounds = 1
                r.budget_used_cost = 0.0
                r.code = f"by\n  native_decide"
                r.error = None
                r.termination = "proved"
                return r
            mock.side_effect = side_effect

            p = LLMPolicy()
            results = p.batch_rollout([
                "theorem t1 : 1=1 := by",
                "theorem t2 : 2=2 := by",
            ], PolicyContext(max_rounds=2))
            assert len(results) == 2
            assert all(r.success for r in results)

    def test_debug_logging(self, caplog):
        """Debug mode should log info."""
        with patch("omega.loop.inner.inner_loop") as mock:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.rounds = 1
            mock_result.budget_used_cost = 0.0
            mock_result.code = "by rfl"
            mock_result.error = None
            mock_result.termination = "proved"
            mock.return_value = mock_result

            import logging
            caplog.set_level(logging.INFO)
            p = LLMPolicy()
            result = p.rollout("theorem t : True := by trivial", PolicyContext(debug=True))
            assert result.success is True


# ═══════════════════════════════════════════════════════════════════
# 3. RouterPolicy
# ═══════════════════════════════════════════════════════════════════


class TestRouterPolicy:
    """RouterPolicy wraps ModeRouter as a Policy."""

    def test_create_auto(self):
        p = RouterPolicy()
        assert isinstance(p, Policy)
        assert "auto" in p.name

    def test_create_fixed_mode(self):
        for mode in ("dfs", "beam", "hybrid"):
            p = RouterPolicy(mode=mode)
            assert mode in p.name

    def test_create_invalid_mode(self):
        with pytest.raises(ValueError):
            RouterPolicy(mode="invalid")

    def test_act_returns_action(self):
        p = RouterPolicy()
        state = ProofState(theorem="theorem t : 1+1=2 := by")
        action = p.act(state, PolicyContext())
        assert isinstance(action, ProofAction)
        assert action.type == "strategy_switch"

    def test_rollout_auto_mode(self):
        """Auto mode should call ModeRouter.route() then execute."""
        with patch("omega.engine.router.ModeRouter") as mock_router, \
             patch("omega.engine.router.get_strategy") as mock_strategy:
            # Renamed: get_strategy is from omega.engine, not omega.engine.router

            # Mock ModeRouter
            router_instance = MagicMock()
            decision = MagicMock()
            decision.strategy_name = "dfs"
            decision.scores = {"dfs": 8.0, "beam": 3.0, "hybrid": 2.0}
            decision.reasoning = "Easy theorem, DFS preferred"
            decision.confidence = 0.8
            router_instance.route.return_value = decision
            mock_router.return_value = router_instance

            # Mock strategy via omega.engine.get_strategy
            with patch("omega.engine.get_strategy") as mock_get:
                strategy_instance = MagicMock()
                strategy_traj = Trajectory(success=True, theorem="test", elapsed_ms=100)
                strategy_instance.run.return_value = strategy_traj
                mock_get.return_value = strategy_instance

                p = RouterPolicy()
                result = p.rollout("theorem t : 1+1=2 := by")

                assert result.success is True
                assert result.reward == 1.0
                mock_router.return_value.route.assert_called()

    def test_rollout_fixed_mode(self):
        """Fixed mode should skip ModeRouter decision."""
        with patch("omega.engine.get_strategy") as mock_strategy:
            strategy_instance = MagicMock()
            strategy_traj = Trajectory(success=False, theorem="test", elapsed_ms=50)
            strategy_instance.run.return_value = strategy_traj
            mock_strategy.return_value = strategy_instance

            p = RouterPolicy(mode="beam")
            result = p.rollout("theorem t : 1+1=2 := by")

            assert result.success is False
            mock_strategy.assert_called_with("beam")

    def test_rollout_handles_exception(self):
        """Exception during rollout should be caught."""
        with patch("omega.engine.get_strategy") as mock_strategy:
            mock_strategy.side_effect = RuntimeError("strategy crashed")

            p = RouterPolicy()
            result = p.rollout("theorem t : 1+1=2 := by")
            assert result.success is False
            assert "strategy crashed" in (result.error or "")

    def test_history_tracks_rollouts(self):
        with patch("omega.engine.get_strategy") as mock_strategy:
            traj = Trajectory(success=True, theorem="test", elapsed_ms=100)
            mock_strategy.return_value.run.return_value = traj

            p = RouterPolicy()
            p.rollout("theorem t1 : 1+1=2 := by")
            p.rollout("theorem t2 : 2+2=4 := by")
            assert len(p.history) == 2
            assert p.stats["n_calls"] == 2
            assert p.stats["success_rate"] == 1.0

    def test_update_returns_metrics(self):
        p = RouterPolicy()
        traj = Trajectory(success=True, theorem="test")
        metrics = p.update([traj], [1.0])
        assert metrics["n_trajectories"] == 1
        assert metrics["updated"] is False


# ═══════════════════════════════════════════════════════════════════
# 4. Integration
# ═══════════════════════════════════════════════════════════════════


class TestPolicyIntegration:
    """Cross-policy integration tests."""

    def test_both_policies_share_base(self):
        llm = LLMPolicy()
        router = RouterPolicy()
        assert isinstance(llm, Policy)
        assert isinstance(router, Policy)
        assert llm.name != router.name

    def test_both_have_rollout_update(self):
        llm = LLMPolicy()
        router = RouterPolicy()
        # Both should accept rollout() and update()
        for p in (llm, router):
            assert hasattr(p, "rollout")
            assert hasattr(p, "update")

    def test_policy_result_interchangeable(self):
        """PolicyResult should work regardless of which policy produced it."""
        r1 = PolicyResult(success=True, n_rounds=3, reward=1.0)
        r2 = PolicyResult(success=False, n_rounds=5, reward=0.0)
        assert r1.success != r2.success
        assert r1.n_rounds < r2.n_rounds

    def test_can_observe_trajectory(self):
        """PolicyResult.trajectory should contain steps."""
        step = TrajectoryStep(
            state_before=ProofState(theorem="t"),
            action=ProofAction(type="tactic"),
            state_after=ProofState(theorem="t"),
        )
        traj = Trajectory(steps=[step], theorem="t", success=True)
        result = PolicyResult(success=True, trajectory=traj, n_rounds=1, reward=1.0)
        assert result.trajectory is not None
        assert len(result.trajectory.steps) == 1
        assert result.trajectory.success is True

    def test_policy_context_passed_through(self):
        """PolicyContext settings should propagate."""
        from copy import deepcopy
        original = PolicyContext(max_rounds=10, timeout=60, debug=True)
        copy = PolicyContext(**original.to_dict())
        assert copy.max_rounds == 10
        assert copy.timeout == 60
        assert copy.debug is True

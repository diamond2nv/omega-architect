"""Tests for budget tracking and convergence monitoring (dual-tier compatible)."""

from omega.resource import (
    BudgetTracker,
    BudgetConfig,
    BudgetTier,
    ConvergenceTracker,
    EpochSnapshot,
    get,
    reset_config_cache,
)


# ── Fixtures ────────────────────────────────────────────────────


def _small_cfg() -> BudgetConfig:
    """BudgetConfig with small remote limits for testing."""
    return BudgetConfig(
        max_tokens=1_000_000,
        max_cost_usd=0.50,
        max_time_s=300.0,
        max_attempts=50,
        remote=BudgetTier(
            max_tokens=1_000_000,
            max_cost_usd=0.50,
            max_time_s=300.0,
            max_attempts=50,
            min_confidence=0.3,
        ),
    )


# ── config access ───────────────────────────────────────────────


class TestConfigAccess:
    def setup_method(self):
        reset_config_cache()

    def test_get_budget_max_tokens(self):
        assert get("budget.max_tokens") == 1000000

    def test_get_budget_max_cost(self):
        assert get("budget.max_cost_usd") == 0.50

    def test_get_models(self):
        models = get("models")
        assert "free" in models

    def test_get_default(self):
        assert get("nonexistent.key", 42) == 42


# ── BudgetTracker ───────────────────────────────────────────────


class TestBudgetTracker:
    def test_init_defaults(self):
        bt = BudgetTracker(cfg=_small_cfg())
        remaining = bt.remaining("remote/deepseek")
        assert remaining["tokens"] == 1000000
        assert remaining["cost"] == 0.50
        assert remaining["time"] == 300.0
        assert remaining["attempts"] == 50

    def test_free_model_detection(self):
        bt = BudgetTracker()
        assert bt.is_free_model("ollama/llama3") is True
        assert bt.is_free_model("local/model") is True
        assert bt.is_free_model("deepseek/deepseek-chat") is False

    def test_estimate_cost_free(self):
        bt = BudgetTracker()
        cost = bt.estimate_cost("ollama/llama3", 100000, 50000)
        assert cost == 0.0

    def test_estimate_cost_paid(self):
        bt = BudgetTracker()
        cost = bt.estimate_cost("deepseek/deepseek-chat", 10000, 500)
        assert cost > 0
        assert cost < 0.01

    def test_check_token_within(self):
        bt = BudgetTracker(cfg=_small_cfg())
        assert bt.check_token(10000, 500, "local/model") is True

    def test_check_token_exceeded_remote(self):
        """Even with small config, remote tokens should be limited."""
        bt = BudgetTracker(cfg=_small_cfg())
        assert bt.check_token(2_000_000, 500_000, "deepseek/deepseek-chat") is False

    def test_check_cost_free_model(self):
        bt = BudgetTracker()
        assert bt.check_cost(100000, 50000, "ollama/llama3") is True

    def test_check_cost_exceeded(self):
        cfg = _small_cfg()
        cfg.remote.max_cost_usd = 0.01  # small remote budget
        cfg.model_prices = {
            "deepseek/deepseek-chat": {
                "input_per_token": 2.8e-7,
                "output_per_token": 4.2e-7,
            },
        }
        bt = BudgetTracker(cfg=cfg)
        assert bt.check_cost(500_000, 100_000, "deepseek/deepseek-chat") is False

    def test_check_time_within(self):
        bt = BudgetTracker(cfg=_small_cfg())
        assert bt.check_time(10.0, "remote/model") is True

    def test_check_time_exceeded(self):
        bt = BudgetTracker(cfg=_small_cfg())
        assert bt.check_time(1000.0, "remote/model") is False

    def test_check_attempts_within(self):
        bt = BudgetTracker(cfg=_small_cfg())
        assert bt.check_attempts(5, "remote/model") is True

    def test_check_attempts_exceeded(self):
        bt = BudgetTracker(cfg=_small_cfg())
        assert bt.check_attempts(100, "remote/model") is False

    def test_consume_deducts_budget(self):
        bt = BudgetTracker(cfg=_small_cfg())
        bt.consume(1000, 200, "deepseek/deepseek-chat", elapsed_s=2.5)
        remaining = bt.remaining("deepseek/deepseek-chat")
        assert remaining["tokens"] < 1000000
        assert remaining["cost"] < 0.50
        assert remaining["time"] < 300.0
        assert remaining["attempts"] == 49

    def test_consume_free_does_not_deduct_remote_cost(self):
        bt = BudgetTracker(cfg=_small_cfg())
        bt.consume(100000, 50000, "ollama/llama3", elapsed_s=10.0)
        remaining = bt.remaining("deepseek/deepseek-chat")
        assert remaining["cost"] == 0.50  # remote cost untouched

    def test_summary_non_empty(self):
        bt = BudgetTracker(cfg=_small_cfg())
        bt.consume(1000, 200, "deepseek/deepseek-chat", elapsed_s=1.5)
        summary = bt.summary("deepseek/deepseek-chat")
        assert len(summary) > 0

    def test_reset_restores_budget(self):
        bt = BudgetTracker(cfg=_small_cfg())
        bt.consume(100000, 50000, "deepseek/deepseek-chat", elapsed_s=50.0)
        bt.reset()
        remaining = bt.remaining("deepseek/deepseek-chat")
        assert remaining["tokens"] == 1000000
        assert remaining["attempts"] == 50

    def test_local_tier_large_limit(self):
        """Local tier should have generous defaults (12h time budget, 15+ tok/s)."""
        bt = BudgetTracker()
        remaining = bt.remaining("ollama/llama3")
        # Time-based: remaining_time should be 12h (43200s)
        assert remaining["time"] >= 43200 - 1  # ~12h
        # Token budget should be time-based: 12h × 15 tok/s = 648k (known hardware)
        assert remaining["tokens"] >= 500_000  # at least 500k tokens
        # Dynamic tok/s should be reasonable
        assert bt.dynamic_tok_s() >= 10.0  # at least 10 tok/s


# ── ConvergenceTracker ──────────────────────────────────────────


class TestConvergenceTracker:
    def test_init_defaults(self):
        ct = ConvergenceTracker()
        assert ct._window == 3
        assert ct._threshold == 0.1

    def test_initial_state(self):
        ct = ConvergenceTracker()
        assert ct.convergence_rate() == 0.0
        assert ct.is_converged() is False
        assert ct.is_stuck() is False
        assert ct.best_epoch() == 0

    def test_improving_convergence(self):
        ct = ConvergenceTracker(window=2)
        ct.record_epoch(10, 50, 2.5, ["error x"])
        ct.record_epoch(5, 60, 2.5, ["error y"])
        ct.record_epoch(2, 80, 2.5, [])
        rate = ct.convergence_rate()
        assert rate > 0.3

    def test_converged_when_zero_errors(self):
        ct = ConvergenceTracker()
        ct.record_epoch(5, 50, 2.5, ["error"])
        ct.record_epoch(0, 100, 2.5, [])
        assert ct.is_converged() is True

    def test_best_epoch(self):
        ct = ConvergenceTracker()
        ct.record_epoch(10, 50, 2.5, ["error 1"])
        ct.record_epoch(5, 60, 2.5, ["error 2"])
        ct.record_epoch(1, 80, 2.5, ["error 3"])
        assert ct.best_epoch() == 3

    def test_summary_string(self):
        ct = ConvergenceTracker()
        ct.record_epoch(5, 50, 2.5, ["error"])
        summary = ct.summary()
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_epoch_snapshot_create(self):
        snap = EpochSnapshot.create(
            epoch=1,
            n_errors=5,
            proof_length=100,
            elapsed_s=2.5,
            errors=["error msg"],
        )
        assert snap.n_errors == 5
        assert snap.error_rate == 5 / 100
        assert snap.error_signature is not None


# ── integration with GoedelProver ───────────────────────────────


class TestGoedelProverWithBudget:
    def test_budget_tracker_in_run(self):
        from omega.prover import GoedelProver

        bt = BudgetTracker(cfg=_small_cfg())
        # Override model_id to remote so we test remote budget
        gp = GoedelProver(compile_fn=None, budget_tracker=bt)
        gp._model_id = lambda: "deepseek/deepseek-chat"  # type: ignore[method-assign]
        result = gp.run("theorem t : True :=")

        remaining = bt.remaining("deepseek/deepseek-chat")
        assert remaining["attempts"] < 50
        assert result.budget_summary != ""

    def test_convergence_tracker_in_run(self):
        from omega.prover import GoedelProver

        ct = ConvergenceTracker(window=3)
        gp = GoedelProver(compile_fn=None, convergence_tracker=ct)
        result = gp.run("theorem t : True :=")

        assert result.convergence_summary != ""
        assert isinstance(result.convergence_rate, float)
        assert isinstance(result.stuck, bool)

    def test_budget_attempts_stop_early(self):
        from omega.prover import GoedelProver

        bt = BudgetTracker(cfg=_small_cfg())
        bt._remote_attempts = 1
        gp = GoedelProver(compile_fn=None, budget_tracker=bt)
        gp._model_id = lambda: "deepseek/deepseek-chat"  # type: ignore[method-assign]
        result = gp.run("theorem t : True :=")
        assert result.n_attempts <= 1

    def test_time_budget_stops_early(self):
        from omega.prover import GoedelProver

        bt = BudgetTracker(cfg=_small_cfg())
        bt._remote_time = 0.0
        gp = GoedelProver(compile_fn=None, budget_tracker=bt)
        gp._model_id = lambda: "deepseek/deepseek-chat"  # type: ignore[method-assign]
        result = gp.run("theorem t : True :=")
        assert result.n_attempts <= 3

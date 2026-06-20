"""Tests for LUFFY Mixed-Policy GRPO Trainer."""
from tests.helpers import needs_gpu

import importlib.util
import os
import sys
import tempfile
import json

import pytest
import torch

# Mark: skip GPU-heavy integration tests by default
integration = pytest.mark.skipif(
    not os.environ.get("LUFFY_INTEGRATION"),
    reason="Set LUFFY_INTEGRATION=1 to run GPU/model tests",
)

# Direct import: bypass the heavy omega.__init__ chain
_luffy_path = os.path.join(os.path.dirname(__file__), "..", "omega", "learn", "rl", "luffy_mixed_trainer.py")
_luffy_modname = "omega.learn.rl.luffy_mixed_trainer"

try:
    spec = importlib.util.spec_from_file_location(_luffy_modname, _luffy_path)
    luffy = importlib.util.module_from_spec(spec)
    sys.modules[_luffy_modname] = luffy
    spec.loader.exec_module(luffy)
    _LUFFY_AVAILABLE = True
except ImportError:
    _LUFFY_AVAILABLE = False
    luffy = None  # type: ignore[assignment]

if not _LUFFY_AVAILABLE:
    pytest.skip("peft/trl not available", allow_module_level=True)

LuffyTrainerConfig = luffy.LuffyTrainerConfig
LuffyMixedPolicyTrainer = luffy.LuffyMixedPolicyTrainer
compute_grpo_loss = luffy.compute_grpo_loss
policy_shaping_weight = luffy.policy_shaping_weight
LuffyLogger = luffy.LuffyLogger
compute_compile_reward = luffy.compute_compile_reward


# ── Fixtures ──

@pytest.fixture
def tmp_config():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = LuffyTrainerConfig(
            checkpoint_dir=os.path.join(tmp, "checkpoints"),
            log_dir=os.path.join(tmp, "logs"),
            jsonl_path=os.path.join(tmp, "logs/metrics.jsonl"),
            batch_size=2,
            grpo_group_size=4,
            gradient_accumulation_steps=2,
            max_steps=4,
            tensorboard=True,
        )
        yield cfg


@pytest.fixture
def sample_theorems():
    return [
        "∀ (n : ℕ), n + 0 = n",
        "∀ (n m : ℕ), n + m = m + n",
    ]


@pytest.fixture
def trainer(tmp_config):
    return LuffyMixedPolicyTrainer(tmp_config)


# ── Tests ──

class TestGRPOLoss:
    """GRPO loss computation (differentiable)."""

    def test_basic_loss_shape(self):
        """Loss returns scalar tensor, attached to computation graph."""
        log_probs = torch.randn(4, 50, requires_grad=True)
        ref_log_probs = torch.randn(4, 50)
        rewards = torch.tensor([1.0, 0.5, -0.5, -1.0])

        loss, info = compute_grpo_loss(log_probs, ref_log_probs, rewards)
        assert loss.ndim == 0  # scalar
        assert loss.requires_grad  # differentiable
        assert loss.item() != 0.0  # non-trivial

    def test_gradient_flows(self):
        """Gradient flows through loss to log_probs."""
        log_probs = torch.randn(4, 50, requires_grad=True)
        ref_log_probs = torch.randn(4, 50)
        rewards = torch.tensor([1.0, 0.5, -0.5, -1.0])

        loss, _ = compute_grpo_loss(log_probs, ref_log_probs, rewards)
        loss.backward()
        assert log_probs.grad is not None
        assert log_probs.grad.norm().item() > 0

    def test_higher_reward_higher_log_prob(self):
        """After gradient update, the policy gradient term should decrease for high-reward samples."""
        log_probs = torch.randn(2, 10, requires_grad=True)
        ref_log_probs = torch.randn(2, 10)
        rewards = torch.tensor([1.0, -1.0])

        loss, info = compute_grpo_loss(log_probs, ref_log_probs, rewards)
        # Gradient step: increase log prob for positive-reward sequence
        loss.backward()
        with torch.no_grad():
            # Move in the direction that reduces loss
            log_probs -= 0.5 * log_probs.grad

        after_loss, after_info = compute_grpo_loss(log_probs, ref_log_probs, rewards)
        # Both policy loss + KL penalty should have decreased overall
        assert after_loss.item() <= loss.item() + 1e-4  # allow epsilon for floating point

    def test_advantage_normalization(self):
        """Advantages are zero-mean, unit-variance."""
        log_probs = torch.randn(8, 30, requires_grad=True)
        ref_log_probs = torch.randn(8, 30)
        rewards = torch.tensor([2.0, 1.5, 1.0, 0.5, -0.5, -1.0, -1.5, -2.0])

        loss, info = compute_grpo_loss(log_probs, ref_log_probs, rewards)
        assert "advantage/mean" in info
        assert "advantage/std" in info
        assert abs(info["advantage/mean"]) < 0.1  # approx zero
        assert abs(info["advantage/std"] - 1.0) < 0.2  # approx unit

    def test_kl_penalty_effect(self):
        """Higher KL beta increases the penalty term."""
        log_probs = torch.randn(4, 50)
        ref_log_probs = log_probs * 0.5  # different from ref
        rewards = torch.ones(4)

        _, info_low = compute_grpo_loss(log_probs, ref_log_probs, rewards, kl_beta=0.01)
        _, info_high = compute_grpo_loss(log_probs, ref_log_probs, rewards, kl_beta=1.0)

        assert info_high["loss/kl_penalty"] > info_low["loss/kl_penalty"]

    def test_all_same_reward(self):
        """When all rewards are equal, advantages should be zero."""
        log_probs = torch.randn(4, 30, requires_grad=True)
        ref_log_probs = torch.randn(4, 30)
        rewards = torch.ones(4)  # all same

        loss, info = compute_grpo_loss(log_probs, ref_log_probs, rewards)
        # With all same rewards, only KL penalty remains
        assert abs(info["advantage/mean"]) < 0.01
        assert abs(info["advantage/std"] - 0.0) < 0.1

    def test_no_reduce_preserves_batch(self):
        """Without reduce, returns per-token loss."""
        log_probs = torch.randn(4, 50, requires_grad=True)
        ref_log_probs = torch.randn(4, 50)
        rewards = torch.tensor([1.0, 0.5, -0.5, -1.0])

        per_token_loss, _ = compute_grpo_loss(log_probs, ref_log_probs, rewards, reduce=False)
        assert per_token_loss.shape == (4, 50)
        assert per_token_loss.requires_grad


class TestPolicyShaping:
    """Policy shaping function f(x) = x/(x+γ)."""

    def test_zero_on_policy(self):
        assert policy_shaping_weight(0, 5) == 0.0

    def test_equal_ratio(self):
        # γ=3.0: at ratio=1, weight = 1/(1+3) = 0.25
        w = policy_shaping_weight(10, 10, gamma=3.0)
        assert abs(w - 0.25) < 0.01

    def test_more_off_than_on(self):
        w = policy_shaping_weight(5, 20, gamma=3.0)
        # ratio = 4, weight = 4/7 ≈ 0.57
        assert abs(w - 0.571) < 0.01

    def test_monotonic(self):
        """Weight increases as off-policy ratio increases."""
        ws = [policy_shaping_weight(10, k, gamma=3.0) for k in [1, 5, 10, 20, 50]]
        for i in range(len(ws) - 1):
            assert ws[i] <= ws[i + 1]

    def test_approaches_one(self):
        """As ratio → ∞, f(x) → 1."""
        w = policy_shaping_weight(1, 1000, gamma=3.0)
        assert w > 0.99

    def test_gamma_controls_steepness(self):
        """Lower gamma = faster approach to 1."""
        w_high = policy_shaping_weight(10, 10, gamma=5.0)
        w_low = policy_shaping_weight(10, 10, gamma=1.0)
        assert w_low > w_high


class TestLuffyLogger:
    """Dual-channel TensorBoard + JSONL logger."""

    def test_jsonl_writes(self, tmp_config):
        logger = LuffyLogger(tmp_config)
        logger.log(1, {"loss/total": 0.5, "reward/mean": 0.3})

        assert os.path.exists(tmp_config.jsonl_path)
        with open(tmp_config.jsonl_path) as f:
            record = json.loads(f.readline())
        assert record["step"] == 1
        assert record["loss/total"] == 0.5

    def test_multiple_writes(self, tmp_config):
        logger = LuffyLogger(tmp_config)
        for i in range(3):
            logger.log(i, {"loss": 1.0 / (i + 1)})

        with open(tmp_config.jsonl_path) as f:
            lines = f.readlines()
        assert len(lines) == 3

    def test_close(self, tmp_config):
        logger = LuffyLogger(tmp_config)
        logger.close()
        # No crash

    def test_histogram(self, tmp_config):
        logger = LuffyLogger(tmp_config)
        logger.log_histogram("test/hist", torch.randn(100), 0)
        logger.close()


class TestLuffyTrainer:
    """Integration tests for the LUFFY trainer."""

    def test_config_defaults(self):
        cfg = LuffyTrainerConfig()
        assert cfg.lora_r == 16
        assert cfg.grpo_group_size == 8
        assert cfg.policy_shaping_gamma == 3.0
        assert cfg.use_qlora is False

    def test_config_dirs_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LuffyTrainerConfig(
                checkpoint_dir=os.path.join(tmp, "ckpt"),
                log_dir=os.path.join(tmp, "logs"),
                jsonl_path=os.path.join(tmp, "logs/m.jsonl"),
            )
            assert os.path.isdir(cfg.checkpoint_dir)
            assert os.path.isdir(cfg.log_dir)

    def test_trainer_init(self, trainer):
        """Trainer initializes with CUDA detection."""
        assert trainer.config.grpo_group_size == 4
        assert trainer.logger is not None
        assert trainer.global_step == 0

    def test_grpo_loss_within_trainer(self, trainer):
        """GRPO loss computation via trainer's static method."""
        log_probs = torch.randn(4, 50, requires_grad=True)
        ref_log_probs = torch.randn(4, 50)
        rewards = torch.tensor([1.0, 0.5, -0.5, -1.0])

        loss, info = compute_grpo_loss(log_probs, ref_log_probs, rewards, kl_beta=0.01)
        loss.backward()
        assert loss.requires_grad
        assert log_probs.grad is not None

    @integration
    def test_train_step_metrics_shape(self, trainer, sample_theorems):
        """train_step returns all expected metrics."""
        metrics = trainer.train_step(sample_theorems)
        assert "loss/on_policy" in metrics
        assert "loss/total" in metrics
        assert "reward/on_mean" in metrics
        assert "timing/step_total_ms" in metrics

    @integration
    def test_evaluate_returns_expected(self, trainer, sample_theorems):
        """evaluate returns reward stats."""
        eval_metrics = trainer.evaluate(sample_theorems[:1])
        assert "reward/mean" in eval_metrics
        assert "reward/std" in eval_metrics
        assert "n_theorems" in eval_metrics

    def test_model_accessors(self, trainer):
        """get_model and get_tokenizer work without crash."""
        assert trainer.get_model() is None  # not loaded yet
        assert trainer.get_tokenizer() is None

    @integration
    def test_export_for_vllm_no_crash(self, trainer):
        """export_for_vllm produces instructions without crash."""
        with tempfile.TemporaryDirectory() as tmp:
            trainer.export_for_vllm(tmp)  # just prints instructions


class TestCompileReward:
    """Compile-based reward function."""

    def test_no_code_negative(self):
        rewards, details = compute_compile_reward(["I think the answer is 42"])
        assert rewards[0] <= -0.5
        assert details[0] == "no_code"

    def test_partial_proof_partial_reward(self):
        rewards, details = compute_compile_reward([
            "```lean4\n  simp\n```"
        ])
        assert rewards[0] > -0.5  # partial
        assert details[0] == "partial"

    def test_incomplete_sorry(self):
        rewards, details = compute_compile_reward([
            "```lean4\n  sorry\n```"
        ])
        assert rewards[0] <= -0.5
        assert details[0] == "incomplete"

    def test_multi_candidates(self):
        candidates = [
            "```lean4\n  by simp\n```",
            "",
            "```lean4\n  sorry\n```",
            "some text",
        ]
        rewards, details = compute_compile_reward(candidates)
        assert len(rewards) == 4
        assert len(details) == 4

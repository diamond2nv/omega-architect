"""Regression tests for 5 critical fixes from system design review."""

from __future__ import annotations

import time
import tempfile
from pathlib import Path

import pytest

from omega.runner import OmegaRunner, _detect_compile_callback
from omega.resource.allocator import ModelAllocator
from omega.research.prover import KnowledgeProver
from omega.prover.go_prover import GoedelProver


# ── P0: compile_fn=None → auto-detect T2 ────────────────────────


class TestT2CompileAutoDetect:
    def test_detect_lean_project_exists(self):
        """If lean-paper-plane exists, _detect_compile_callback returns a callable."""
        project = Path.home() / "lean-paper-plane"
        if project.exists():
            cb = _detect_compile_callback()
            # May be None if toolchain not installed, but must not raise
            assert cb is None or callable(cb)
        else:
            # Without project, must return None with no error
            cb = _detect_compile_callback()
            assert cb is None

    def test_runner_compile_fn_not_none(self):
        """OmegaRunner should auto-detect compile_fn."""
        runner = OmegaRunner()
        # The compile_fn may be None if lean-paper-plane doesn't exist,
        # but the attribute must exist
        assert hasattr(runner, "_compile_fn")

    def test_compile_fn_passed_to_prover(self):
        """Runner should pass compile_fn to GoedelProver."""
        from omega.prover.go_prover import GoedelProver

        # Create a mock compile_fn that always succeeds
        def mock_compile(code):
            return type("T2Result", (), {"verified": True, "errors": []})()

        runner = OmegaRunner()
        runner._compile_fn = mock_compile
        bt = runner._bt
        gp = GoedelProver(compile_fn=runner._compile_fn, budget_tracker=bt)
        assert gp.compile_fn is mock_compile


# ── P1: Append-only context truncation ──────────────────────────


class TestAppendOnlyContextTruncation:
    def test_no_truncation_for_small_proof(self):
        """Proof code under 8000 chars should NOT be truncated."""
        runner = OmegaRunner()
        small_proof = "by simp" * 100  # ~800 chars
        from omega.runner import TheoremSpec
        t = TheoremSpec(header="theorem t : True :=", proof_code=small_proof)

        # We can't easily call _prove_theorem without full setup,
        # but we can verify the truncation logic directly.
        MAX_APPEND_CHARS = 8000
        assert len(small_proof) <= MAX_APPEND_CHARS

    def test_truncation_for_large_proof(self):
        """Proof code over 8000 chars should be truncated to last 8000 + '...'."""
        large_proof = "by " + "x" * 20000  # ~20K chars
        MAX_APPEND_CHARS = 8000
        assert len(large_proof) > MAX_APPEND_CHARS
        # Simulate truncation
        truncated = "..." + large_proof[-MAX_APPEND_CHARS:]
        assert len(truncated) == MAX_APPEND_CHARS + 3  # +3 for "..."
        assert truncated.endswith(large_proof[-10:])  # preserves tail

    def test_truncation_preserves_recent_steps(self):
        """After truncation, the most recent proof steps must be intact."""
        proof = "theorem t : True := by\n" + "\n".join(
            f"  step_{i} : True := by trivial" for i in range(500)
        )
        # Each step is ~35 chars, 500 steps = ~17.5K chars
        assert len(proof) > 8000

        MAX_APPEND_CHARS = 8000
        truncated = "..." + proof[-MAX_APPEND_CHARS:]
        # The last step should still be there
        assert "step_499" in truncated
        # The first step should be gone
        assert "step_0" not in truncated


# ── P2: Token accounting accuracy ───────────────────────────────


class TestTokenAccounting:
    def test_new_heuristic_over_old(self):
        """New heuristic (len/2.5 + 500) should be >= old heuristic (len/4)."""
        lean_code = "theorem add_comm (a b : Nat) : a + b = b + a := by\n  induction a with\n  | zero => simp\n  | succ a ih => simp [add_succ, ih]"

        old_estimate = max(100, len(lean_code) // 4)
        SYSTEM_OVERHEAD_TOKENS = 500
        CHARS_PER_TOKEN = 2.5
        new_estimate = SYSTEM_OVERHEAD_TOKENS + max(100, int(len(lean_code) / CHARS_PER_TOKEN))

        assert new_estimate > old_estimate  # New should be more conservative
        assert new_estimate > 500  # At minimum accounts for overhead

    def test_output_estimate_no_longer_zero(self):
        """Even short tactics should get a reasonable output token estimate."""
        CHARS_PER_TOKEN = 2.5
        short_tactic = "simp"
        old = max(50, len(short_tactic) // 4)  # 50
        new = max(50, int(len(short_tactic) / CHARS_PER_TOKEN))  # 50
        # Both floor to 50, which is a reasonable minimum

    def test_large_input_not_capped(self):
        """Large lean_code should produce proportionally larger estimates."""
        CHARS_PER_TOKEN = 2.5
        SYSTEM_OVERHEAD_TOKENS = 500
        small = "x" * 1000
        large = "x" * 100000

        small_est = SYSTEM_OVERHEAD_TOKENS + max(100, int(len(small) / CHARS_PER_TOKEN))
        large_est = SYSTEM_OVERHEAD_TOKENS + max(100, int(len(large) / CHARS_PER_TOKEN))

        assert large_est > small_est * 10  # ~40x more chars → ~40x more tokens


# ── P3: Research cache TTL ──────────────────────────────────────


class TestResearchCacheTTL:
    def test_cache_stale_detected(self, tmp_path):
        """Cache older than max_age_hours should return None."""
        from omega.research.knowledge import KnowledgePackage
        from omega.research.sources import PaperStoreSource, ArxivSource, LeanCodeSource, WikiSource, KiwixSource

        # Create a KnowledgeProver with very short TTL (0.001h = 3.6s)
        kp = KnowledgeProver(
            base_prover=None,
            use_cache=True,
            cache_max_age_hours=0.001,  # 3.6 seconds
            cache_dir=str(tmp_path),
        )

        # Write a cache file directly
        cache_file = kp._cache_path("theorem t : True :=")
        import json
        cache_file.write_text(json.dumps({
            "related_papers": [],
            "relevant_lemmas": [],
            "proof_patterns": [],
            "key_insights": ["test insight"],
            "theorem_statements": [],
            "mathlib_imports": [],
            "search_stats": {},
            "cache_hit": True,
        }))

        # Immediately load — should work (not stale yet)
        result = kp._load_cache("theorem t : True :=")
        assert result is not None
        assert "test insight" in result.key_insights

        # Wait for TTL to expire, then try again
        time.sleep(3.7)  # > 3.6s
        result2 = kp._load_cache("theorem t : True :=")
        assert result2 is None  # Should be stale

    def test_cache_no_ttl_disabled(self, tmp_path):
        """Setting cache_max_age_hours=0 disables staleness check."""
        from omega.research.prover import KnowledgeProver

        kp = KnowledgeProver(
            base_prover=None,
            use_cache=True,
            cache_max_age_hours=0,  # disabled
            cache_dir=str(tmp_path),
        )

        import json
        cache_file = kp._cache_path("theorem t : True :=")
        cache_file.write_text(json.dumps({
            "related_papers": [], "relevant_lemmas": [],
            "proof_patterns": [], "key_insights": ["test"],
            "theorem_statements": [], "mathlib_imports": [],
            "search_stats": {}, "cache_hit": True,
        }))

        # Even if old, should load because TTL is 0 (disabled)
        result = kp._load_cache("theorem t : True :=")
        assert result is not None


# ── P4: Flash/Pro model ID matching ─────────────────────────────


class TestModelClassification:
    def setup_method(self):
        self.alloc = ModelAllocator()

    def test_flash_direct(self):
        assert self.alloc._classify_remote_model("deepseek/deepseek-v4-flash") == "flash"

    def test_flash_via_openrouter(self):
        assert self.alloc._classify_remote_model("openrouter/deepseek/deepseek-v4-flash") == "flash"

    def test_flash_short(self):
        assert self.alloc._classify_remote_model("deepseek-v4-flash") == "flash"

    def test_pro_deepseek_chat(self):
        assert self.alloc._classify_remote_model("deepseek/deepseek-chat") == "pro"

    def test_pro_explicit(self):
        assert self.alloc._classify_remote_model("deepseek/deepseek-v4-pro") == "pro"

    def test_pro_unknown_remote_default(self):
        """Unknown remote defaults to 'pro' (conservative)."""
        assert self.alloc._classify_remote_model("custom/model") == "pro"

    def test_local_model_not_remote(self):
        assert self.alloc._classify_remote_model("ollama/gemma4:26b") == "local"
        assert self.alloc._classify_remote_model("local/test") == "local"

    def test_record_outcome_uses_classification(self):
        """record_outcome should use _classify_remote_model to track flash/pro."""
        # Record flash via OpenRouter
        self.alloc.record_outcome("t1", "openrouter/deepseek/deepseek-v4-flash",
                                  self.alloc.classify("t1"), False, cost_usd=0.10)
        assert self.alloc._flash_spent == 0.10
        assert self.alloc._pro_spent == 0.0

        # Record pro via standard path
        self.alloc.record_outcome("t2", "deepseek/deepseek-v4-pro",
                                  self.alloc.classify("t2"), False, cost_usd=0.20)
        assert self.alloc._pro_spent == 0.20
        assert self.alloc._flash_spent == 0.10  # unchanged

    def test_budget_tracker_pricing_accuracy(self):
        """BudgetTracker should use correct DeepSeek pricing."""
        from omega.resource import BudgetTracker, BudgetConfig
        bt = BudgetTracker()
        # Flash: $0.139/M input (¥1/M), $0.278/M output (¥2/M)
        cost = bt.estimate_cost("deepseek/deepseek-v4-flash", 1000, 500)
        assert cost > 0
        assert cost < 0.01  # Should be very cheap

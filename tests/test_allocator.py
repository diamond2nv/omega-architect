"""Tests for allocator.py — strategic local/remote model routing."""

from __future__ import annotations

import pytest

from omega.resource.allocator import (
    ComplexityClass,
    ModelAllocator,
    ModelConfig,
    ProvenanceRecord,
)


class TestComplexityClassification:
    def setup_method(self):
        self.alloc = ModelAllocator()

    def test_easy_pattern(self):
        assert self.alloc.classify("theorem add_comm (a b : Nat) : a + b = b + a :=") == ComplexityClass.EASY

    def test_easy_simp(self):
        assert self.alloc.classify("theorem t : True := by simp") == ComplexityClass.EASY

    def test_easy_trivial(self):
        assert self.alloc.classify("theorem t : True := by trivial") == ComplexityClass.EASY

    def test_medium_is_default(self):
        assert self.alloc.classify("theorem mult_by_two (n : Nat) : n + n = 2 * n :=") == ComplexityClass.MEDIUM

    def test_hard_convergence(self):
        assert self.alloc.classify("theorem banach_fixed_point : ... :=") == ComplexityClass.HARD

    def test_hard_spectral(self):
        assert self.alloc.classify("theorem spectral_theorem : ... :=") == ComplexityClass.HARD

    def test_hard_long_header(self):
        long = "theorem t : " + "A" * 201 + " :="
        assert self.alloc.classify(long) == ComplexityClass.HARD

    def test_domain_based_hard(self):
        assert self.alloc.classify("theorem t : True :=", domain="quantum") == ComplexityClass.HARD
        assert self.alloc.classify("theorem t : True :=", domain="optics") == ComplexityClass.HARD
        # "algebra" is NOT in COMPLEX_DOMAINS, and "theorem t : True :=" has no HARD/EASY pattern
        assert self.alloc.classify("theorem t : True :=", domain="algebra") == ComplexityClass.MEDIUM

    def test_research_by_domain(self):
        """'Hamiltonian' contains HARD pattern 'hamiltoni' → HARD."""
        assert self.alloc.classify("Synthesis of NV Hamiltonian") == ComplexityClass.HARD


class TestModelSelection:
    def setup_method(self):
        self.alloc = ModelAllocator()

    def test_easy_uses_gemma4(self):
        alloc = self.alloc.select_model("theorem add_comm (a b : Nat) : a + b = b + a :=")
        assert alloc.complexity == ComplexityClass.EASY
        assert "gemma4" in alloc.model_id
        assert alloc.blueprint_mode is False

    def test_medium_uses_deepseek(self):
        alloc = self.alloc.select_model("theorem mult_by_two (n : Nat) : n + n = 2 * n :=")
        assert alloc.complexity == ComplexityClass.MEDIUM
        assert "deepseek" in alloc.model_id
        assert alloc.blueprint_mode is False

    def test_hard_first_attempt_local(self):
        alloc = self.alloc.select_model("theorem spectral_theorem (A : Matrix) : ... :=")
        assert alloc.complexity == ComplexityClass.HARD
        assert alloc.blueprint_mode is False  # first attempt local
        assert "qwen3.6" in alloc.model_id or "qwen3" in alloc.model_id

    def test_hard_escalates_to_remote(self):
        """After 3 failures, should escalate to remote for blueprint."""
        header = "theorem spectral_theorem (A : Matrix) : ... :="

        # 3 local failures with the SAME header
        for _ in range(3):
            self.alloc.record_outcome(header, "ollama/qwen3.6:latest", ComplexityClass.HARD, False)

        # 4th call: selects model — should see 3 local attempts and escalate
        alloc2 = self.alloc.select_model(header)
        assert alloc2.blueprint_mode is True  # now remote for blueprint
        assert "deepseek" in alloc2.model_id or "chat" in alloc2.model_id

    def test_hard_remote_budget_exhausted_falls_back(self):
        """When remote budget is exhausted, even hard theorems use local."""
        alloc = ModelAllocator(max_remote_budget_usd=0.01)
        alloc._remote_spent = 0.01  # exhaust budget
        result = alloc.select_model("theorem spectral_theorem : ... :=")
        assert result.blueprint_mode is False  # fallback to local

    def test_blueprint_model_selection(self):
        model = self.alloc.select_blueprint_model()
        assert isinstance(model, str)
        assert len(model) > 0


class TestProvenanceRecording:
    def setup_method(self):
        self.alloc = ModelAllocator()

    def test_record_outcome(self):
        self.alloc.record_outcome(
            theorem_header="theorem t : True :=",
            model_id="ollama/deepseek-r1:8b",
            complexity=ComplexityClass.EASY,
            succeeded=True,
            elapsed_s=5.0,
            n_attempts=1,
            tokens_consumed=500,
        )
        assert len(self.alloc._provenance) == 1
        assert self.alloc._provenance[0].succeeded is True
        assert self.alloc._provenance[0].tier == "local"

    def test_record_remote_tier(self):
        self.alloc.record_outcome(
            theorem_header="theorem hard : True :=",
            model_id="deepseek/deepseek-v4-flash",
            complexity=ComplexityClass.HARD,
            succeeded=True,
            cost_usd=0.05,
        )
        assert self.alloc._provenance[0].tier == "remote"
        assert self.alloc._remote_spent == 0.05

    def test_local_attempts_tracked(self):
        for i in range(3):
            self.alloc.record_outcome(
                theorem_header="theorem t : True :=",
                model_id="ollama/local-model",
                complexity=ComplexityClass.MEDIUM,
                succeeded=False,
            )
        assert self.alloc._local_attempts.get("theorem t : True :=") == 3

    def test_provenance_summary_nonempty(self):
        summary = self.alloc.provenance_summary()
        assert summary == "No provenance records."

        self.alloc.record_outcome("theorem t : True :=", "ollama/test", ComplexityClass.EASY, True)
        summary = self.alloc.provenance_summary()
        assert "Provenance" in summary

    def test_cost_report(self):
        self.alloc.record_outcome("t", "deepseek/deepseek-v4-pro", ComplexityClass.HARD, True, cost_usd=0.10)
        self.alloc.record_outcome("t2", "deepseek/deepseek-v4-flash", ComplexityClass.MEDIUM, True, cost_usd=0.02, append_only=True)
        report = self.alloc.cost_report()
        assert report["total_usd"] == pytest.approx(0.12)
        assert "deepseek/deepseek-v4-pro" in report["by_model"]
        assert "deepseek/deepseek-v4-flash" in report["by_model"]
        assert report["pro_spent"] == 0.10
        assert report["flash_spent"] == 0.02

    def test_reset_local_counters(self):
        self.alloc.record_outcome("t", "ollama/test", ComplexityClass.EASY, True)
        self.alloc.record_outcome("t", "ollama/test", ComplexityClass.EASY, True)
        assert self.alloc._local_attempts.get("t") == 2
        self.alloc.reset_local_counters()
        assert self.alloc._local_attempts.get("t") is None

    def test_select_continuation_model(self):
        model = self.alloc.select_continuation_model()
        assert "flash" in model or "deepseek" in model

    def test_append_only_routing_medium(self):
        """MEDIUM theorem after 3 local failures → flash append."""
        header = "theorem medium_test (n : Nat) : n + 0 = n :="
        for _ in range(3):
            self.alloc.record_outcome(header, "ollama/deepseek-r1:8b", ComplexityClass.MEDIUM, False)

        alloc = self.alloc.select_model(header)
        assert alloc.append_only is True
        assert "flash" in alloc.model_id

    def test_append_only_routing_hard_continuation(self):
        """HARD theorem after blueprint → flash append."""
        header = "theorem spectral_theorem (A : Matrix) : ... :="
        for _ in range(3):
            self.alloc.record_outcome(header, "ollama/qwen3.6:latest", ComplexityClass.HARD, False)
        # First remote = pro blueprint
        alloc1 = self.alloc.select_model(header)
        assert alloc1.blueprint_mode is True
        assert alloc1.append_only is False

        # Record pro outcome
        self.alloc.record_outcome(header, "deepseek/deepseek-v4-flash", ComplexityClass.HARD, False, cost_usd=0.05)

        # Second remote = flash append
        alloc2 = self.alloc.select_model(header)
        assert alloc2.append_only is True
        assert "flash" in alloc2.model_id

    def test_flash_budget_separate_from_pro(self):
        """Flash budget exhaustion doesn't affect pro budget (demo mode)."""
        assert self.alloc._max_flash_budget == 50.00
        assert self.alloc._max_pro_budget == 50.00
        # Spend all flash
        self.alloc.record_outcome("t", "deepseek/deepseek-v4-flash", ComplexityClass.MEDIUM, False,
                                  cost_usd=50.00, append_only=True)
        assert self.alloc._flash_spent >= 1.50
        # Pro should still be available
        assert self.alloc._pro_spent == 0.0
        assert self.alloc._max_pro_budget - self.alloc._pro_spent > 0


class TestModelConfig:
    def test_default_fields(self):
        m = ModelConfig("test/model", "local", 5, 30.0, 0.0, ["code"])
        assert m.model_id == "test/model"
        assert m.tier == "local"
        assert m.tok_s == 30.0
        assert m.cost_per_1k_tokens == 0.0
        assert m.strengths == ["code"]


class TestProvenanceRecord:
    def test_tier_inference_local(self):
        r = ProvenanceRecord(
            theorem_header="t",
            model_id="ollama/test-model",
            complexity=ComplexityClass.EASY,
            succeeded=True,
            elapsed_s=1.0,
            n_attempts=1,
            tokens_consumed=100,
            cost_usd=0.0,
        )
        assert r.tier == "local"

    def test_tier_inference_remote(self):
        r = ProvenanceRecord(
            theorem_header="t",
            model_id="deepseek/deepseek-v4-flash",
            complexity=ComplexityClass.HARD,
            succeeded=True,
            elapsed_s=1.0,
            n_attempts=1,
            tokens_consumed=100,
            cost_usd=0.05,
        )
        assert r.tier == "remote"

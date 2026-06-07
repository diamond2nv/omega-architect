"""Tests for DSPy template manager and compiler integration."""

import json
import shutil
from pathlib import Path

import pytest

from omega.prover.compiler import (
    ProofCompiler,
    ProofCompilerError,
    headers_to_trainset,
    results_to_trainset,
    theorem_to_example,
)
from omega.prover.go_prover import GoedelResult
from omega.prover.template_manager import (
    _HAS_DSPY,
    ProverModule,
    TacticModule,
    configure_dspy_lm,
    extract_optimized_demos,
    extract_optimized_prompt,
)

pytestmark = pytest.mark.skipif(
    not _HAS_DSPY,
    reason="DSPy is not installed (pip install dspy)",
)


# ── Template Manager ───────────────────────────────────────────────


class TestProverSignature:
    def test_signature_defined(self):
        """ProverSignature is defined when DSPy is available."""
        from omega.prover.template_manager import ProverSignature

        assert ProverSignature is not None
        assert "theorem_header" in ProverSignature.model_fields
        assert "playbook_context" in ProverSignature.model_fields
        assert "lean_code" in ProverSignature.model_fields

    def test_tactic_signature_defined(self):
        """TacticSignature is defined when DSPy is available."""
        from omega.prover.template_manager import TacticSignature

        assert TacticSignature is not None
        assert "goal" in TacticSignature.model_fields
        assert "next_tactic" in TacticSignature.model_fields


class TestProverModule:
    def test_module_creates(self):
        """ProverModule can be instantiated."""
        module = ProverModule()
        assert module is not None
        assert hasattr(module, "prover")

    def test_tactic_module_creates(self):
        """TacticModule can be instantiated."""
        module = TacticModule()
        assert module is not None
        assert hasattr(module, "tactic_gen")


class TestExtraction:
    def test_extract_prompt_from_uncompiled(self):
        """Empty prompt when module not compiled."""
        prompt = extract_optimized_prompt(None)
        assert prompt == ""

    def test_extract_demos_from_uncompiled(self):
        """Empty demos when module not compiled."""
        demos = extract_optimized_demos(None)
        assert demos == []


class TestConfigureLM:
    def test_configure_lm(self):
        """configure_dspy_lm returns True when DSPy is available."""
        result = configure_dspy_lm()
        assert result is True


# ── Compiler Data Preparation ──────────────────────────────────────


class TestDataPreparation:
    def test_theorem_to_example(self):
        """Single theorem converts to DSPy Example."""
        ex = theorem_to_example(
            theorem_header="theorem t : True :=",
            lean_code="theorem t : True := trivial",
        )
        assert ex is not None
        assert ex.theorem_header == "theorem t : True :="
        assert ex.lean_code == "theorem t : True := trivial"
        assert ex.playbook_context == ""

    def test_theorem_to_example_with_playbook(self):
        """Playbook context is preserved in example."""
        ex = theorem_to_example(
            theorem_header="theorem t : True :=",
            lean_code="theorem t : True := trivial",
            playbook_context="Use trivial for True goals.",
        )
        assert ex is not None
        assert "Use trivial" in ex.playbook_context

    def test_results_to_trainset_filters_success(self):
        """Only successful results become training examples."""
        success = GoedelResult(
            proof="theorem t : True := trivial",
            n_passed=1,
        )
        failure = GoedelResult(n_attempts=5)
        results = [
            ("theorem t1 : True :=", success),
            ("theorem t2 : False :=", failure),
        ]
        examples = results_to_trainset(results, max_examples=10)
        assert len(examples) == 1
        assert "t1" in examples[0].theorem_header

    def test_results_to_trainset_all_failures(self):
        """No training examples when all results fail."""
        results = [
            ("theorem t : True :=", GoedelResult(n_attempts=3)),
        ]
        examples = results_to_trainset(results)
        assert len(examples) == 0

    def test_headers_to_trainset(self):
        """Bare headers create examples with empty lean_code."""
        headers = ["theorem a : A :=", "theorem b : B :=", "theorem c : C :="]
        examples = headers_to_trainset(headers, max_examples=2)
        assert len(examples) == 2
        assert examples[0].lean_code == ""
        assert examples[1].lean_code == ""

    def test_theorem_to_example_inputs_marked(self):
        """Input fields are marked on the example."""
        ex = theorem_to_example(
            theorem_header="theorem t : True :=",
            lean_code="theorem t : True := trivial",
        )
        assert ex is not None
        inputs = ex.inputs()
        assert "theorem_header" in inputs
        assert "playbook_context" in inputs


# ── ProofCompiler ──────────────────────────────────────────────────


class TestProofCompiler:
    def test_init_defaults(self):
        """Compiler initializes with defaults."""
        compiler = ProofCompiler()
        assert compiler.optimizer_name == "MIPROv2"
        assert compiler.trainset == []
        assert compiler.valset == []

    def test_compile_no_dspy_raises(self, monkeypatch):
        """Without DSPy, compile raises ProofCompilerError."""
        monkeypatch.setattr("omega.prover.compiler._HAS_DSPY", False)
        monkeypatch.setattr("omega.prover.compiler.dspy", None)
        compiler = ProofCompiler(module=ProverModule())
        with pytest.raises(ProofCompilerError, match="DSPy is not installed"):
            compiler.compile()

    def test_compile_no_module_raises(self):
        """Without a module, compile raises."""
        compiler = ProofCompiler(trainset=[1])  # dummy
        with pytest.raises(ProofCompilerError, match="No DSPy module"):
            compiler.compile()

    def test_compile_no_trainset_raises(self):
        """Without trainset, compile raises."""
        compiler = ProofCompiler(module=ProverModule())
        with pytest.raises(ProofCompilerError, match="Training set is empty"):
            compiler.compile()

    def test_resolve_optimizer_miprov2(self):
        """MIPROv2 optimizer resolves to a callable factory."""
        compiler = ProofCompiler(optimizer="MIPROv2")
        fn = compiler._resolve_optimizer()
        assert callable(fn)

    def test_resolve_optimizer_gepa(self):
        """GEPA optimizer resolves to a callable factory."""
        compiler = ProofCompiler(optimizer="GEPA")
        fn = compiler._resolve_optimizer()
        assert callable(fn)

    def test_resolve_optimizer_bootstrap(self):
        """BootstrapFewShot resolves to a callable factory."""
        compiler = ProofCompiler(optimizer="BootstrapFewShot")
        fn = compiler._resolve_optimizer()
        assert callable(fn)

    def test_resolve_optimizer_unknown_raises(self):
        """Unknown optimizer raises error."""
        compiler = ProofCompiler(optimizer="UnknownOptimizer")
        with pytest.raises(ProofCompilerError, match="Unknown optimizer"):
            compiler._resolve_optimizer()

    def test_get_optimized_prompt_before_compile(self):
        """Before compile, prompt is empty."""
        compiler = ProofCompiler()
        assert compiler.get_optimized_prompt() == ""

    def test_get_demos_before_compile(self):
        """Before compile, demos are empty."""
        compiler = ProofCompiler()
        assert compiler.get_optimized_demos() == []

    def test_get_stats_before_compile(self):
        """Before compile, stats are empty."""
        compiler = ProofCompiler()
        assert compiler.get_stats() == {}

    def test_evaluate_before_compile(self):
        """Before compile, evaluation returns zeros."""
        compiler = ProofCompiler()
        result = compiler.evaluate()
        assert result == {"accuracy": 0.0, "num_correct": 0, "num_total": 0}

    def test_save_load_prompt(self, tmp_path):
        """Save and load work correctly."""
        compiler = ProofCompiler(compile_dir=tmp_path)
        compiler._compile_stats = {
            "optimizer": "MIPROv2", "train_size": 5, "success": True, "elapsed_s": 1.0,
        }
        # Manually create saved prompt for load test
        prompt_dir = tmp_path / "test_save"
        prompt_dir.mkdir(parents=True)
        (prompt_dir / "optimized_prompt.txt").write_text("optimized instruction")
        (prompt_dir / "compile_stats.json").write_text('{"test": true}')
        (prompt_dir / "optimized_demos.json").write_text('[]')

        loaded = compiler.load("test_save")
        assert loaded == "optimized instruction"

    def test_save_load_nonexistent(self):
        """Load from nonexistent path returns empty string."""
        compiler = ProofCompiler()
        result = compiler.load("nonexistent_compile")
        assert result == ""


# ── End-to-end compile (lightweight, no real LM) ───────────────────


class TestEndToEndCompile:
    """Lightweight end-to-end tests.

    These use a simple DSPy program with dummy data to verify the
    compile pipeline works without a real LLM.
    """

    def test_compile_with_mock_program(self):
        """Compile succeeds with minimal data and a mock metric.

        This tests the orchestration layer, not the LM optimization.
        """
        import dspy

        # Create a minimal trainset with real signatures.
        ex1 = dspy.Example(
            theorem_header="theorem t1 : True :=",
            playbook_context="",
            lean_code="theorem t1 : True := trivial",
        ).with_inputs("theorem_header", "playbook_context")

        module = ProverModule()
        compiler = ProofCompiler(
            module=module,
            optimizer="BootstrapFewShot",
            trainset=[ex1],
            valset=[ex1],
        )

        # BootstrapFewShot does NOT require an LM for its metric.
        compiled = compiler.compile()
        assert compiled is not None

        # After compile we can get stats.
        stats = compiler.get_stats()
        assert stats["success"] is True
        assert stats["train_size"] == 1
        assert "elapsed_s" in stats

    def test_evaluate_after_compile(self):
        """After compile, evaluation runs without errors."""
        import dspy

        ex = dspy.Example(
            theorem_header="theorem t1 : True :=",
            playbook_context="",
            lean_code="theorem t1 : True := trivial",
        ).with_inputs("theorem_header", "playbook_context")

        module = ProverModule()
        compiler = ProofCompiler(
            module=module,
            optimizer="BootstrapFewShot",
            trainset=[ex],
            valset=[ex],
        )
        compiler.compile()
        result = compiler.evaluate()
        assert "accuracy" in result
        assert "num_correct" in result
        assert "num_total" in result

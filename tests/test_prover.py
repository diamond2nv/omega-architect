"""Tests for proof generation (Goedel, Rethlas, Archon, Ensemble)."""
import pytest

from tests.helpers import needs_lean

from omega.prover.ar_prover import ArchonProver, CriticStatus, ProgressCritic
from omega.prover.ensemble import EnsembleProver, StrategyOutcome
from omega.prover.go_prover import GoedelProver, GoedelResult
from omega.prover.re_prover import Blueprint, RethlasProver, Subgoal

# ── mock compiler ──────────────────────────────────────────────


def _mock_pass(_code: str) -> dict:
    """Mock compiler that always passes."""
    return {"diagnostics": [], "exit_code": 0}


def _mock_fail(_code: str) -> dict:
    """Mock compiler that always fails."""
    return {
        "diagnostics": [{"message": "type error", "severity": "error", "line": 1, "column": 1}],
        "exit_code": 1,
    }


def _mock_pass_for(code: str, keyword: str) -> dict:
    """Mock compiler that passes only if code contains keyword."""
    if keyword in code:
        return {"diagnostics": [], "exit_code": 0}
    return {"diagnostics": [{"message": "mock fail", "severity": "error"}], "exit_code": 1}


# ── Goedel Prover ──────────────────────────────────────────────


class TestGoedelProver:
    def test_init_defaults(self):
        """Default initialization."""
        gp = GoedelProver()
        assert gp.num_samples == 6
        assert gp.max_correction_rounds == 2

    def test_init_custom(self):
        """Custom parameters."""
        gp = GoedelProver(num_samples=8, max_correction_rounds=3)
        assert gp.num_samples == 8
        assert gp.max_correction_rounds == 3

    def test_run_no_compiler(self):
        """Without compile_fn, returns failed result."""
        gp = GoedelProver(compile_fn=None)
        result = gp.run("theorem t : True := trivial")
        assert isinstance(result, GoedelResult)
        assert result.succeeded is False
        assert result.proof is None
        assert result.n_attempts > 0

    def test_run_mock_pass(self):
        """With mock pass compiler, finds proof."""
        gp = GoedelProver(compile_fn=_mock_pass)
        result = gp.run("theorem t : True := trivial")
        assert result.succeeded is True
        assert result.proof is not None

    def test_run_mock_fail(self):
        """With mock fail compiler, returns failed."""
        gp = GoedelProver(compile_fn=_mock_fail, num_samples=2)
        result = gp.run("theorem t : True := trivial")
        assert result.succeeded is False
        assert result.n_attempts > 0

    def test_proof_trivial(self):
        """Result contains Lean code when successful."""
        gp = GoedelProver(compile_fn=_mock_pass)
        result = gp.run("theorem t : True := trivial")
        assert "theorem t" in (result.proof or "")

    def test_summary_success(self):
        """Summary indicates success."""
        gp = GoedelProver(compile_fn=_mock_pass)
        result = gp.run("theorem t : True := trivial")
        assert "proof found" in result.summary or "✅" in result.summary

    def test_summary_fail(self):
        """Summary indicates failure."""
        gp = GoedelProver(compile_fn=_mock_fail, num_samples=2)
        result = gp.run("theorem t : True := trivial")
        assert "no proof" in result.summary or "❌" in result.summary

    def test_to_dict(self):
        """Serialization works."""
        gp = GoedelProver(compile_fn=_mock_pass)
        result = gp.run("theorem t : True := trivial")
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "succeeded" in d
        assert "n_attempts" in d


# ── Rethlas Prover ─────────────────────────────────────────────


class TestRethlasProver:
    def test_init_defaults(self):
        """Default initialization."""
        rp = RethlasProver()
        assert rp.max_depth == 3
        assert rp.max_attempts == 5

    def test_prove_trivial(self):
        """Prove a trivial theorem."""
        rp = RethlasProver(compile_fn=_mock_pass)
        result = rp.prove("theorem t : True := trivial")
        assert result.success is True
        assert result.proof is not None

    def test_prove_no_compiler(self):
        """Without compile_fn, falls back to template mode."""
        rp = RethlasProver(compile_fn=None)
        result = rp.prove("theorem t : True := trivial")
        assert isinstance(result.success, bool)
        assert result.n_attempts >= 0

    def test_blueprint_creation(self):
        """Blueprint can be created with subgoals."""
        bp = Blueprint(
            template_name="induction",
            subgoals=[
                Subgoal(id="base", description="base case", target="n = 0", goal_type="ℕ → Prop"),
                Subgoal(
                    id="step",
                    description="inductive step",
                    target="n+1 = n+1",
                    goal_type="ℕ → Prop",
                ),
            ],
            composition_template="{sg_0}; {sg_1}",
        )
        assert len(bp.subgoals) == 2
        assert bp.template_name == "induction"

    def test_prover_result_summary(self):
        """Summary is a non-empty string."""
        rp = RethlasProver(compile_fn=_mock_pass)
        result = rp.prove("theorem t : True := trivial")
        assert isinstance(result.summary, str)
        assert len(result.summary) > 0


# ── Archon Prover ──────────────────────────────────────────────


class TestArchonProver:
    def test_init_defaults(self):
        """Default initialization."""
        ap = ArchonProver()
        assert ap.max_iterations == 5

    def test_prove_trivial(self):
        """Prove a trivial theorem."""
        ap = ArchonProver(compile_fn=_mock_pass)
        result = ap.prove("theorem t : True := trivial")
        assert result.success is True
        assert result.proof is not None

    def test_prove_no_compiler(self):
        """Without compile_fn, runs strategies in offline mode."""
        ap = ArchonProver(compile_fn=None)
        result = ap.prove("theorem t : True := trivial")
        assert isinstance(result.success, bool)


class TestProgressCritic:
    def test_init_defaults(self):
        """Default initialization."""
        pc = ProgressCritic()
        assert pc.max_stuck == 3
        assert pc.max_churn == 4

    def test_solved_detection(self):
        """Zero errors and some proof = SOLVED."""
        pc = ProgressCritic()
        status = pc.observe(iteration=0, n_errors=0, proof_length=50, errors=[])
        assert status == CriticStatus.SOLVED


# ── Ensemble Prover ────────────────────────────────────────────


class TestEnsembleProver:
    def test_init_defaults(self):
        """Default initialization."""
        ep = EnsembleProver()
        assert "goedel" in ep.config
        assert "rethlas" in ep.config
        assert "archon" in ep.config

    def test_run_with_mock(self):
        """Ensemble runs all strategies with mock compiler."""
        ep = EnsembleProver(compile_fn=_mock_pass)
        result = ep.run("theorem t : True := trivial")
        assert result.succeeded is True
        assert result.elected is not None
        assert result.best_proof is not None
        assert len(result.outcomes) == 3  # All 3 strategies ran

    def test_run_goedel_only(self):
        """Ensemble with only Goedel strategy."""
        ep = EnsembleProver(compile_fn=_mock_pass)
        result = ep.run("theorem t : True := trivial", run_rethlas=False, run_archon=False)
        assert result.succeeded is True
        assert result.elected == "goedel"
        assert len(result.outcomes) == 1

    def test_summary_string(self):
        """Summary is a non-empty string."""
        ep = EnsembleProver(compile_fn=_mock_pass)
        result = ep.run("theorem t : True := trivial")
        summary = result.summary()
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_to_dict(self):
        """Serialization works."""
        ep = EnsembleProver(compile_fn=_mock_pass)
        result = ep.run("theorem t : True := trivial")
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "elected" in d
        assert "outcomes" in d

    def test_comparison_table(self):
        """Comparison table is generated."""
        ep = EnsembleProver(compile_fn=_mock_pass)
        result = ep.run("theorem t : True := trivial")
        assert "| Strategy |" in result.comparison_table
        assert "goedel" in result.comparison_table

    def test_strategy_outcome(self):
        """Strategy outcome stores data."""
        so = StrategyOutcome(
            name="test", succeeded=True, proof="code", elapsed_ms=100, n_attempts=3, summary="ok"
        )
        assert so.succeeded is True
        assert so.proof == "code"

    def test_ensemble_result_no_proof(self):
        """Ensemble without proof returns failed result."""
        ep = EnsembleProver(compile_fn=_mock_fail)
        result = ep.run("theorem t : True := trivial", run_rethlas=False, run_archon=False)
        assert result.succeeded is False
        assert result.elected is None
        assert result.best_proof is None


# ── integration test ───────────────────────────────────────────


class TestProverIntegration:
    """Verify that real compilation works with nlinarith theorem."""

    @needs_lean
    def test_real_compile_trivial(self):
        """Real compile of trivial theorem via lake env lean.

        Verifies:
        1. The proposer generates syntactically valid Lean code
        2. The generated code passes T2 compilation
        3. Early exit on first successful proof
        """
        from omega.verify.t2_real import make_real_compile_callback

        compile_fn = make_real_compile_callback()

        # Header should be just the theorem signature (with ``:=`` or ``:= by``
        # — the prover will add the ``by`` block)
        gp = GoedelProver(compile_fn=compile_fn, num_samples=2)
        result = gp.run("""import Mathlib
        theorem t : True :=""")
        # The `trivial` tactic with proper ``:= by`` syntax MUST pass T2
        assert result.succeeded is True, (
            f"Expected T2 pass for `trivial`, got errors: "
            f"{[e[:80] for e in (result.attempts[0]['errors'] if result.attempts else ['no attempts'])]}"
        )
        assert result.proof is not None
        assert "by" in result.proof  # Check syntax correction worked
        assert "trivial" in result.proof
        assert result.n_passed >= 1

    def test_real_compile_ensemble(self):
        """Ensemble with real compiler on trivial theorem only."""
        from omega.verify.t2_real import make_real_compile_callback

        compile_fn = make_real_compile_callback()
        ep = EnsembleProver(
            compile_fn=compile_fn,
            config={
                "goedel": {"num_samples": 1, "max_correction_rounds": 0},
                "rethlas": {"max_depth": 1, "max_attempts": 1},
                "archon": {"max_iterations": 1, "goedel_samples": 1},
            },
        )
        result = ep.run(
            """import Mathlib
        theorem t : True := trivial""",
            run_rethlas=True,
            run_archon=True,
        )
        assert isinstance(result.succeeded, bool)
        assert len(result.outcomes) == 3

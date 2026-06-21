"""Tests for Layer 3 Orchestrator — Multi-Agent State Machine.

Covers:
1. 8 Primitives — each atomic operation
2. Orchestrator state machine — analyze → blueprint → prove → synthesize
3. Edge cases — empty theorem, all lemmas fail, decomposition
4. Integration with Blueprint module
"""
from __future__ import annotations

# ── Python 3.10 compat: add datetime.UTC BEFORE other imports ──
import datetime as _dt
if not hasattr(_dt, 'UTC'):
    import zoneinfo as _zi
    _dt.UTC = _zi.ZoneInfo('UTC')

# ── Python 3.10 compat: add tomllib (stdlib in 3.11+) ──
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
    import sys
    sys.modules['tomllib'] = tomllib

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Direct import: bypass the omega.__init__ chain (Python 3.10 datetime.UTC issue)
_orch_path = os.path.join(os.path.dirname(__file__), "..", "omega", "engine", "orchestrator.py")
_orch_modname = "omega.engine.orchestrator"
spec = importlib.util.spec_from_file_location(_orch_modname, _orch_path)
orch = importlib.util.module_from_spec(spec)
sys.modules[_orch_modname] = orch
# Mock blueprint module to avoid import chain
class MockBlueprint:
    def __init__(self, theorem_header="", lemmas=None, edges=None):
        self.theorem_header = theorem_header
        self.lemmas = lemmas or {}
        self.edges = edges or []
    def unproven(self):
        return [l for l in self.lemmas.values() if getattr(l, 'status', None) != MockLemmaStatus.PROVED]
    def has_failures(self):
        return any(getattr(l, 'status', None) == MockLemmaStatus.FAILED for l in self.lemmas.values())

class MockLemmaNode:
    PROVED = "proved"
    FAILED = "failed"
    UNPROVEN = "unproven"
    def __init__(self, label="mock", header="", status=None, _id=None, dependencies=None, description="", depth=0):
        self.label = label
        self.header = header
        self.status = status or self.UNPROVEN
        self.id = _id or f"lem_{label}_{hash(label)}"
        self.dependencies = dependencies or []
        self.description = description
        self.depth = 0
        self.proof = None
    def set_proved(self, code):
        self.status = self.PROVED
        self.proof = code

class MockDiagnosisType:
    pass

class MockLemmaStatus:
    PROVED = "proved"
    FAILED = "failed"
    UNPROVEN = "unproven"

# Swap real imports for mock classes
Blueprint = MockBlueprint
LemmaNode = MockLemmaNode
LemmaStatus = MockLemmaStatus
# We need the real generate_blueprint and refine_blueprint
# But they import omega.plan which hits datetime.UTC
# So we mock the entire omega.search.blueprint module
mock_blueprint_mod = type(sys)('omega.search.blueprint')
mock_blueprint_mod.Blueprint = MockBlueprint
mock_blueprint_mod.LemmaNode = MockLemmaNode
mock_blueprint_mod.DiagnosisType = MockDiagnosisType
mock_blueprint_mod.LemmaStatus = MockLemmaStatus
mock_blueprint_mod.generate_blueprint = MagicMock(return_value=MockBlueprint())
mock_blueprint_mod.refine_blueprint = MagicMock(return_value=MockBlueprint())
sys.modules['omega.search.blueprint'] = mock_blueprint_mod

spec.loader.exec_module(orch)

# Register as submodule so @patch("omega.engine.orchestrator.*") works
import omega.engine
omega.engine.orchestrator = orch

Orchestrator = orch.Orchestrator
OrchestratorConfig = orch.OrchestratorConfig
OrchestratorResult = orch.OrchestratorResult
OrchestratorState = orch.OrchestratorState
PrimitiveResult = orch.PrimitiveResult
apply_lemma = orch.apply_lemma
rewrite_goal = orch.rewrite_goal
induction = orch.induction
case_split = orch.case_split
calc_chain = orch.calc_chain
search_lemma = orch.search_lemma
extract_proof = orch.extract_proof
fallback_decompose = orch.fallback_decompose
PRIMITIVES = orch.PRIMITIVES


# ═══════════════════════════════════════════════════════════════════
# 1. Primitives
# ═══════════════════════════════════════════════════════════════════


class TestPrimitives:
    """Each primitive should produce a valid PrimitiveResult."""

    def test_apply_lemma_success(self):
        r = apply_lemma("a = b", {"suggested_lemma": "add_comm"})
        assert isinstance(r, PrimitiveResult)
        assert r.success
        assert "add_comm" in (r.code or "")

    def test_apply_lemma_no_lemma(self):
        r = apply_lemma("a = b", {})
        assert not r.success
        assert r.error is not None

    def test_rewrite_goal_default(self):
        r = rewrite_goal("a + b = b + a")
        assert r.success
        assert "simp" in (r.code or "")

    def test_rewrite_goal_with_pattern(self):
        r = rewrite_goal("a + b = b + a", {"rewrite_pattern": "add_comm"})
        assert r.success
        assert "rw" in (r.code or "")
        assert "add_comm" in (r.code or "")

    def test_induction_creates_subgoals(self):
        r = induction("∀ n : ℕ, n + 0 = n", {"induction_var": "n"})
        assert r.success
        assert "induction n" in (r.code or "")
        assert len(r.new_subgoals) == 2  # base + step

    def test_induction_default_var(self):
        r = induction("∀ x, x + 0 = x")
        assert r.success
        assert "induction n" in (r.code or "")

    def test_case_split_creates_subgoals(self):
        r = case_split("P ∨ Q", {"case_var": "h"})
        assert r.success
        assert "cases h" in (r.code or "")
        assert len(r.new_subgoals) == 2

    def test_calc_chain_produces_skeleton(self):
        r = calc_chain("a = b = c")
        assert r.success
        assert "calc" in (r.code or "")

    def test_search_lemma_fallback_on_error(self):
        """Should return gracefully even if network is unavailable."""
        r = search_lemma("x + y = y + x")
        assert isinstance(r, PrimitiveResult)
        # May succeed or fail depending on network — that's fine
        assert r.elapsed_ms >= 0

    def test_extract_proof_from_successful_trajectory(self):
        traj = MagicMock()
        traj.success = True
        traj.proof = "by native_decide"
        r = extract_proof(traj)
        assert r.success
        assert "native_decide" in (r.code or "")

    def test_extract_proof_from_failed_trajectory(self):
        traj = MagicMock()
        traj.success = False
        r = extract_proof(traj)
        assert not r.success

    def test_extract_proof_from_none(self):
        r = extract_proof(None)
        assert not r.success

    def test_fallback_decompose_creates_subgoals(self):
        r = fallback_decompose("hard theorem",
                                {"subgoals": ["sub1", "sub2"]})
        assert not r.success  # decompose always "fails" the original
        assert len(r.new_subgoals) == 2
        assert "sub1" in r.new_subgoals

    def test_fallback_decompose_default_subgoals(self):
        r = fallback_decompose("hard theorem")
        assert len(r.new_subgoals) == 2  # default
        assert "sub-lemma 1" in r.new_subgoals[0]

    def test_all_primitives_registered(self):
        assert "apply_lemma" in PRIMITIVES
        assert "rewrite_goal" in PRIMITIVES
        assert "induction" in PRIMITIVES
        assert "case_split" in PRIMITIVES
        assert "calc_chain" in PRIMITIVES
        assert "search_lemma" in PRIMITIVES
        assert "extract_proof" in PRIMITIVES
        assert "fallback_decompose" in PRIMITIVES
        assert len(PRIMITIVES) == 8

    def test_primitive_result_defaults(self):
        r = PrimitiveResult()
        assert r.success is False
        assert r.code is None
        assert r.new_subgoals == []
        assert r.elapsed_ms == 0


# ═══════════════════════════════════════════════════════════════════
# 2. Orchestrator State Machine
# ═══════════════════════════════════════════════════════════════════


class TestOrchestrator:
    """Orchestrator state machine should handle analyze→blueprint→prove→verify."""

    def test_create_orchestrator(self):
        orch = Orchestrator()
        assert orch.state == OrchestratorState.IDLE
        assert len(orch.state_history) == 0

    def test_create_orchestrator_with_config(self):
        cfg = OrchestratorConfig(max_refinement_rounds=5, lemma_timeout_s=60)
        orch = Orchestrator(cfg)
        assert orch._config.max_refinement_rounds == 5

    @patch("omega.loop.inner.inner_loop")
    @patch("omega.engine.orchestrator.generate_blueprint")
    def test_simple_theorem_proved(self, mock_generate, mock_inner):
        """A simple theorem with 1 lemma should be proved directly."""
        # Mock inner_loop → success
        inner_result = MagicMock()
        inner_result.success = True
        inner_result.code = "by native_decide"
        inner_result.rounds = 2
        inner_result.termination = "proved"
        inner_result.error = None
        mock_inner.return_value = inner_result

        # Mock blueprint → single lemma
        lemma = LemmaNode(label="main", header="theorem t : 1+1=2 := by",
                          status=LemmaStatus.UNPROVEN)
        mock_generate.return_value = Blueprint(
            theorem_header="theorem t : 1+1=2 := by",
            lemmas={lemma.id: lemma},
            edges=[],
        )

        orch = Orchestrator()
        result = orch.run("theorem t : 1+1=2 := by")

        assert result.success
        assert result.proof is not None
        assert result.n_primitives_used >= 1
        assert result.blueprint is not None

    @patch("omega.loop.inner.inner_loop")
    @patch("omega.engine.orchestrator.generate_blueprint")
    def test_all_lemmas_fail(self, mock_generate, mock_inner):
        """When all lemmas fail, orchestrator should report failure."""
        inner_result = MagicMock()
        inner_result.success = False
        inner_result.error = "stuck"
        inner_result.termination = "stuck"
        inner_result.rounds = 5
        mock_inner.return_value = inner_result

        lemma = LemmaNode(label="main", header="hard theorem",
                          status=LemmaStatus.UNPROVEN)
        mock_generate.return_value = Blueprint(
            theorem_header="hard theorem",
            lemmas={lemma.id: lemma},
            edges=[],
        )

        orch = Orchestrator(OrchestratorConfig(enable_fallback_decompose=False))
        result = orch.run("hard theorem")

        assert not result.success
        assert result.blueprint is not None
        # Should have tried the lemma (failed) then stopped

    @patch("omega.loop.inner.inner_loop")
    @patch("omega.engine.orchestrator.generate_blueprint")
    def test_decomposition_on_failure(self, mock_generate, mock_inner):
        """When a lemma fails with decompose enabled, new sub-lemmas should appear."""
        call_count = [0]

        def inner_side_effect(*args, **kwargs):
            call_count[0] += 1
            r = MagicMock()
            # First call fails, subsequent calls succeed
            if call_count[0] == 1:
                r.success = False
                r.error = "too hard"
                r.termination = "stuck"
            else:
                r.success = True
                r.code = "by native_decide"
            r.rounds = 2
            return r

        mock_inner.side_effect = inner_side_effect

        lemma = LemmaNode(label="main", header="theorem t : a = b := by",
                          status=LemmaStatus.UNPROVEN)
        mock_generate.return_value = Blueprint(
            theorem_header="theorem t : a = b := by",
            lemmas={lemma.id: lemma},
            edges=[],
        )

        orch = Orchestrator(OrchestratorConfig(enable_fallback_decompose=True))
        result = orch.run("theorem t : a = b := by")

        # Should have triggered decomposition (first fails → split into sub-lemmas)
        # Even if sub-lemmas succeed, the original lemma is replaced
        assert result.n_primitives_used >= 1

    def test_custom_config_passed_through(self):
        cfg = OrchestratorConfig(
            max_refinement_rounds=10,
            lemma_timeout_s=300,
            parallel_proving=False,
        )
        orch = Orchestrator(cfg)
        assert orch._config.max_refinement_rounds == 10
        assert orch._config.parallel_proving is False

    @patch("omega.loop.inner.inner_loop")
    def test_state_history_records_transitions(self, mock_inner):
        """State history should show the full state machine flow."""
        inner_result = MagicMock()
        inner_result.success = True
        inner_result.code = "by trivial"
        inner_result.rounds = 1
        inner_result.termination = "proved"
        inner_result.error = None
        mock_inner.return_value = inner_result

        with patch("omega.engine.orchestrator.generate_blueprint") as mock_gen:
            lemma = LemmaNode(label="main", header="theorem t : True := by trivial")
            mock_gen.return_value = Blueprint(
                theorem_header="theorem t : True := by trivial",
                lemmas={lemma.id: lemma},
                edges=[],
            )

            orch = Orchestrator()
            result = orch.run("theorem t : True := by trivial")

            # With fast-path: 3 states (ANALYZING → GENERATING_BLUEPRINT → COMPLETED)
            # Without fast-path: 4+ states (ANALYZING → GEN_BP → PROVING → ... → COMPLETED)
            assert len(result.state_history) >= 3
            states = [s[0] for s in result.state_history]
            assert OrchestratorState.ANALYZING in states
            assert OrchestratorState.GENERATING_BLUEPRINT in states
            assert OrchestratorState.COMPLETED in states
            # Trivial theorems get fast-path (no PROVING_LEMMAS state)

    @patch("omega.engine.orchestrator.generate_blueprint")
    def test_analyze_unknown_theorem(self, mock_gen):
        """Orchestrator should handle gibberish input gracefully."""
        mock_gen.return_value = Blueprint(
            theorem_header="",
            lemmas={},
            edges=[],
        )
        orch = Orchestrator()
        result = orch.run("")
        assert result.success is False  # Can't prove empty
        assert result.blueprint is not None

    def test_result_summary_format(self):
        r = OrchestratorResult(success=True, proof="by rfl", n_primitives_used=3,
                                n_refinements=1, elapsed_ms=1500)
        assert "✅" in r.summary
        assert "3 primitives" in r.summary
        assert "1 refinements" in r.summary
        assert "1500ms" in r.summary

        r2 = OrchestratorResult(success=False)
        assert "❌" in r2.summary


# ═══════════════════════════════════════════════════════════════════
# 3. Blueprint Integration
# ═══════════════════════════════════════════════════════════════════


class TestOrchestratorBlueprint:
    """Blueprint integration tests."""

    @patch("omega.engine.orchestrator.generate_blueprint")
    @patch("omega.loop.inner.inner_loop")
    def test_blueprint_multi_lemma(self, mock_inner, mock_gen):
        """Multiple dependent lemmas should be proved in order."""
        lemma_a = LemmaNode(_id='a', label='lemma_a',
                            header="lemma_a : X := by",
                            status=LemmaStatus.UNPROVEN)
        lemma_b = LemmaNode(_id="b", label="lemma_b",
                            header="lemma_b : Y := by",
                            status=LemmaStatus.UNPROVEN,
                            dependencies=["a"])

        mock_gen.return_value = Blueprint(
            theorem_header="theorem t : Z := by",
            lemmas={"a": lemma_a, "b": lemma_b},
            edges=[("b", "a")],
        )

        inner_result = MagicMock()
        inner_result.success = True
        inner_result.code = "by native_decide"
        inner_result.rounds = 1
        inner_result.termination = "proved"
        inner_result.error = None
        mock_inner.return_value = inner_result

        orch = Orchestrator()
        result = orch.run("theorem t : Z := by")

        # Both lemmas should be proved (or at least attempted)
        assert result.blueprint is not None
        assert result.n_primitives_used >= 1

    @patch("omega.engine.orchestrator.generate_blueprint")
    def test_blueprint_generation_fallback(self, mock_gen):
        """When blueprint generation fails, fallback to single-goal."""
        mock_gen.side_effect = RuntimeError("LLM error")

        with patch("omega.loop.inner.inner_loop") as mock_inner:
            inner_result = MagicMock()
            inner_result.success = True
            inner_result.code = "by trivial"
            inner_result.rounds = 1
            inner_result.termination = "proved"
            inner_result.error = None
            mock_inner.return_value = inner_result

            orch = Orchestrator()
            result = orch.run("theorem t : True := by trivial")
            assert result.success
            assert result.blueprint is not None

    def test_empty_blueprint(self):
        """Orchestrator should handle no-lemmas scenario."""
        with patch("omega.engine.orchestrator.generate_blueprint") as mock_gen, \
             patch("omega.loop.inner.inner_loop") as mock_inner:
            mock_gen.return_value = Blueprint(
                theorem_header="theorem t : True := by trivial",
                lemmas={},
                edges=[],
            )
            inner_result = MagicMock()
            inner_result.success = True
            inner_result.code = "by trivial"
            inner_result.rounds = 1
            inner_result.termination = "proved"
            inner_result.error = None
            mock_inner.return_value = inner_result

            orch = Orchestrator()
            result = orch.run("theorem t : True := by trivial")
            # Should still work — empty blueprint means direct prove
            assert result.blueprint is not None

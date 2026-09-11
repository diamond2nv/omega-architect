"""CompileGate dominant error class: a *failed* compile must never report NO_ERROR.

Regression for a real-Lean measurement (2026-09-11, WSL / Lean 4.30.0):

    theorem a2 : 1 + 1 = 3 := by decide
    -> success=False, errors=['Tactic `decide` proved that the proposition'],
       error_class=CompileErrorClass.NO_ERROR          # before the fix

Lean emits **one** error line plus **two** info-level context lines
(``1 + 1 = 3`` / ``is false``). ``classify_diagnostic_entry`` correctly labels the
error line ``OTHER`` (the message text matches no named class), but the aggregation
step emptied the named-class pool, fell through to ``max(cls_counts)`` over the whole
pool, and NO_ERROR (2) beat OTHER (1).

Consequence, not cosmetics: ``ERROR_PROXIMITY[NO_ERROR] = 1.0``, so
``CompileDistanceEvaluator`` valued that dead end like a finished proof - the very
defect fixed in `docs/experiments/mcts-lean-e2e-2026-09-10.md` §4.1, resurfacing
through the aggregation step instead of the per-entry step.

No Lean toolchain needed: the compile callback is injected.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omega.engine.lean_adapters import CompileDistanceEvaluator  # noqa: E402
from omega.engine.trajectory import ProofState  # noqa: E402
from omega.loop.compile_gate import CompileGate  # noqa: E402
from omega.loop.errors import CompileErrorClass  # noqa: E402

# The exact diagnostic shape real Lean produced for `decide` on a false goal.
DECIDE_FALSE_DIAGNOSTICS = [
    {"message": "Tactic `decide` proved that the proposition", "severity": "error",
     "line": 2, "column": 2},
    {"message": "1 + 1 = 3", "severity": "info", "line": 1, "column": 1},
    {"message": "is false", "severity": "info", "line": 1, "column": 1},
]


def _gate(payload: dict) -> CompileGate:
    """A CompileGate with an injected compile callback (no Lean binary touched)."""
    gate = CompileGate(cache_dir=tempfile.mkdtemp(prefix="omega-gate-test-"))
    gate._compile_fn = lambda _code: payload
    return gate


def _payload(diagnostics: list[dict], exit_code: int) -> dict:
    return {"success": exit_code == 0, "exit_code": exit_code,
            "diagnostics": diagnostics, "elapsed_ms": 1}


class TestDominantErrorClass:
    def test_failure_with_only_info_diagnostics_is_other(self):
        """失败 + 诊断全是 info 级 ⇒ 池空，必须显式 OTHER，不得回落 NO_ERROR。

        这是残留漏检的形态（真语料实测 3/257 次）：第一版修法用
        ``{...} or cls_counts`` 兜底，池被清空后又把 NO_ERROR 复活了。
        """
        diags = [{"message": "⊢ P", "severity": "info", "line": 1, "column": 1}]
        r = _gate(_payload(diags, 1)).compile("theorem a : P := by\n  bogus")
        assert r.success is False
        assert r.error_class is CompileErrorClass.OTHER

    def test_failure_with_no_diagnostics_is_other(self):
        r = _gate(_payload([], 1)).compile("theorem a : P := by\n  ")
        assert r.error_class is CompileErrorClass.OTHER
    def test_decide_on_false_goal_is_other_not_no_error(self):
        r = _gate(_payload(DECIDE_FALSE_DIAGNOSTICS, 1)).compile("theorem a : 1 + 1 = 3 := by\n  decide")
        assert r.success is False
        assert r.error_class is CompileErrorClass.OTHER
        assert r.error_class is not CompileErrorClass.NO_ERROR

    def test_named_class_still_wins_over_other(self):
        diags = [
            {"message": "unsolved goals\n⊢ P", "severity": "error", "line": 2, "column": 2},
            {"message": "⊢ P", "severity": "info", "line": 1, "column": 1},
            {"message": "⊢ Q", "severity": "info", "line": 1, "column": 1},
        ]
        r = _gate(_payload(diags, 1)).compile("theorem a : P := by\n  simp")
        assert r.error_class is CompileErrorClass.UNSOLVED_GOAL

    def test_success_with_only_info_diagnostics_stays_no_error(self):
        """The fix must not over-reach: a *passing* compile keeps NO_ERROR."""
        diags = [{"message": "⊢ P", "severity": "info", "line": 1, "column": 1}]
        r = _gate(_payload(diags, 0)).compile("theorem a : P := by\n  exact h")
        assert r.success is True
        assert r.error_class is CompileErrorClass.NO_ERROR

    def test_failure_without_diagnostics_is_not_no_error(self):
        r = _gate(_payload([], 1)).compile("theorem a : P := by\n  ")
        assert r.success is False
        assert r.error_class is not CompileErrorClass.NO_ERROR

    def test_cache_roundtrip_preserves_other(self):
        """Cached path re-hydrates the class from a string - it must survive."""
        gate = _gate(_payload(DECIDE_FALSE_DIAGNOSTICS, 1))
        code = "theorem a : 1 + 1 = 3 := by\n  decide"
        first = gate.compile(code)
        second = gate.compile(code)
        assert second.cached is True
        assert first.error_class is second.error_class is CompileErrorClass.OTHER

    def test_value_signal_no_longer_treats_the_dead_end_as_done(self):
        """The point of the fix: the MCTS value signal must separate dead end from proof."""
        r = _gate(_payload(DECIDE_FALSE_DIAGNOSTICS, 1)).compile(
            "theorem a : 1 + 1 = 3 := by\n  decide")
        expected = r.error_class
        assert expected is CompileErrorClass.OTHER
        dead = ProofState(theorem="theorem a : 1 + 1 = 3 := by",
                          code="theorem a : 1 + 1 = 3 := by\n  decide",
                          errors=list(r.errors), error_class=expected.value,
                          is_terminal=False)
        solved = ProofState(theorem="theorem a : 1 + 1 = 2 := by",
                            code="theorem a : 1 + 1 = 2 := by\n  decide",
                            is_terminal=True)
        evaluator = CompileDistanceEvaluator()
        assert evaluator(solved) == 1.0
        assert evaluator(dead) < evaluator(solved)
        assert evaluator(dead) < 0.999

"""Tests for the Lean adapters (Step 2) - no Lean, no LLM required.

Each adapter is exercised through its injected collaborators so the production
code path is the one under test; only the LLM and the compiler are faked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from omega.engine.lean_adapters import (
    ERROR_PROXIMITY,
    CompileDistanceEvaluator,
    CompileGateTransition,
    LeanActionGenerator,
    build_lean_mcts,
)
from omega.engine.trajectory import ProofAction, ProofState
from omega.loop.errors import CompileErrorClass


# ── fakes ────────────────────────────────────────────────────────────────
@dataclass
class FakeCompile:
    """Duck-typed CompileResult."""

    success: bool
    errors: list[str] = field(default_factory=list)
    error_class: object = None
    line: int = 0
    diagnostics: list[dict] = field(default_factory=list)
    elapsed_ms: int = 1
    cached: bool = False


def make_compiler(rules: list[tuple[str, FakeCompile]], default: FakeCompile):
    """Return a compile_fn keyed on a substring of the submitted code."""

    def _compile(code: str) -> FakeCompile:
        for needle, result in rules:
            if needle in code:
                return result
        return default

    return _compile


OK = FakeCompile(success=True)
MISMATCH = FakeCompile(
    success=False,
    errors=["type mismatch at 'h'"],
    error_class=CompileErrorClass.TYPE_MISMATCH,
)
UNKNOWN = FakeCompile(
    success=False,
    errors=["unknown identifier 'foo'"],
    error_class=CompileErrorClass.UNKNOWN_IDENT,
)


def _state(code: str = "theorem t : 1 = 1 := by", **kw) -> ProofState:
    return ProofState(theorem="t", code=code, **kw)


# ── T1: CompileGateTransition ────────────────────────────────────────────
def test_transition_success_marks_terminal() -> None:
    tr = CompileGateTransition(make_compiler([("omega_step_ok", OK)], MISMATCH))
    nxt = tr(_state(), ProofAction(type="tactic", content="omega_step_ok"))
    assert nxt.is_terminal is True
    assert nxt.errors == []
    assert nxt.error_class == ""
    assert nxt.value == 1.0
    assert nxt.depth == 1


def test_transition_failure_carries_class_and_errors() -> None:
    tr = CompileGateTransition(make_compiler([("bad_tactic", MISMATCH)], UNKNOWN))
    nxt = tr(_state(), ProofAction(type="tactic", content="bad_tactic"))
    assert nxt.is_terminal is False
    assert nxt.error_class == CompileErrorClass.TYPE_MISMATCH.value
    assert nxt.errors and "type mismatch" in nxt.errors[0]
    assert nxt.depth == 1


def test_transition_survives_compiler_exception() -> None:
    def boom(_code: str) -> FakeCompile:
        raise RuntimeError("lean not installed")

    nxt = CompileGateTransition(boom)(_state(), ProofAction(content="anything"))
    assert nxt.is_terminal is False
    assert nxt.error_class == CompileErrorClass.OTHER.value
    assert nxt.errors and "lean not installed" in nxt.errors[0]
    assert nxt.metadata.get("compile_exception") == "RuntimeError"


def test_append_tactic_indents_and_ignores_empty() -> None:
    tr = CompileGateTransition(make_compiler([], OK))
    assert tr.append_tactic("theorem t := by", "rfl") == "theorem t := by\n  rfl"
    assert tr.append_tactic("theorem t := by", "   ") == "theorem t := by"


# ── T2: CompileDistanceEvaluator ─────────────────────────────────────────
def test_evaluator_solved_state_is_one() -> None:
    ev = CompileDistanceEvaluator()
    assert ev(_state(is_terminal=True)) == 1.0


def test_evaluator_orders_error_proximity() -> None:
    ev = CompileDistanceEvaluator()
    near = ev(_state(error_class=CompileErrorClass.FAILED_SYNTHESIS.value))
    mid = ev(_state(error_class=CompileErrorClass.TYPE_MISMATCH.value))
    far = ev(_state(error_class=CompileErrorClass.UNKNOWN_IDENT.value))
    dead = ev(_state(error_class=CompileErrorClass.TIMEOUT.value))
    assert near > mid > far > dead
    assert near < 1.0


def test_evaluator_prefers_fewer_remaining_goals() -> None:
    ev = CompileDistanceEvaluator()
    many = ev(_state(error_class=CompileErrorClass.TYPE_MISMATCH.value, goals=["a", "b", "c"]))
    few = ev(_state(error_class=CompileErrorClass.TYPE_MISMATCH.value, goals=["a"]))
    assert few > many


def test_evaluator_unknown_class_gets_penalty() -> None:
    ev = CompileDistanceEvaluator()
    assert ev(_state(error_class="something_new")) == pytest.approx(ev.unknown_penalty)


# ── T3: LeanActionGenerator ──────────────────────────────────────────────
def test_generator_parses_candidates() -> None:
    reply = "```lean\n  exact rfl\n1. simp\n- ring\n\nsome long prose line that rambles on and on and should not become a tactic at all here\n```"
    cands = LeanActionGenerator.parse(reply)
    contents = [c.content for c in cands]
    assert contents[:3] == ["exact rfl", "simp", "ring"]
    assert all(c.type == "tactic" for c in cands)


def test_generator_survives_llm_exception() -> None:
    def boom(_prompt: str) -> str:
        raise TimeoutError("api slow")

    gen = LeanActionGenerator(boom, k=3)
    assert gen(_state()) == []
    assert gen.last_error and "TimeoutError" in gen.last_error


def test_generator_handles_empty_and_junk() -> None:
    gen = LeanActionGenerator(lambda _p: "", k=3)
    assert gen(_state()) == []
    gen2 = LeanActionGenerator(lambda _p: "```lean\n```\n:\n", k=3)
    assert gen2(_state()) == []


# ── T4: end-to-end on a fake Lean (two-step proof) ───────────────────────
def test_build_lean_mcts_end_to_end_with_fakes() -> None:
    calls = {"n": 0}

    def fake_llm(_prompt: str) -> str:
        calls["n"] += 1
        # first proposal is wrong, later ones are right
        return "wrong_tactic" if calls["n"] == 1 else "omega_close"

    compiler = make_compiler([("omega_close", OK)], MISMATCH)
    strat = build_lean_mcts(llm_call=fake_llm, compile_fn=compiler, max_iterations=8)
    traj, diag = strat.run_diagnosed("theorem t : 1 = 1 := by")
    assert traj.success is True
    assert traj.strategy == "MCTS (Lean)"
    assert diag.explored_nodes >= 2
    assert diag.iterations >= 1


def test_build_lean_mcts_keeps_going_when_compiler_explodes() -> None:
    def boom(_code: str) -> FakeCompile:
        raise OSError("no lean")

    strat = build_lean_mcts(llm_call=lambda _p: "rfl", compile_fn=boom, max_iterations=4)
    traj, diag = strat.run_diagnosed("theorem t : 1 = 1 := by")
    assert traj.success is False  # nothing can compile
    assert diag.error_heat, "compiler failures must show up as error heat"
    assert any("other" in k for k in diag.error_heat)


# ── T5: proximity table sanity ───────────────────────────────────────────
def test_proximity_table_covers_all_error_classes() -> None:
    for cls in CompileErrorClass:
        assert cls.value in ERROR_PROXIMITY, f"missing proximity for {cls}"
    assert ERROR_PROXIMITY[CompileErrorClass.NO_ERROR.value] == 1.0

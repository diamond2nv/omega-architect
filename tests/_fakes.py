"""Shared fakes for the engine tests (no Lean toolchain needed).

Why this module exists: the fake compiler, the chain generator and the
"append onto a theorem source" transition were each written 2-3 times across
``test_counterfactual*.py`` / ``test_mcts_attribution_wiring.py`` /
``scripts/counterfactual_labeled_eval.py``. Same semantics should live in one
place - and the *production* half of that duplication is now
``omega.engine.counterfactual.tactics_of_code`` +
``omega.engine.lean_adapters.transition_from_source``, which this module reuses
instead of re-implementing.

Import from a test file::

    sys.path.insert(0, os.path.dirname(__file__))
    from _fakes import ChainCompiler, run_wiring
"""

from __future__ import annotations

from dataclasses import dataclass

from omega.engine.counterfactual import CounterfactualAttributor, tactics_of_code
from omega.engine.lean_adapters import CompileGateTransition, transition_from_source
from omega.engine.mcts_diagnosis import DiagnosisView  # noqa: F401  (re-export for tests)
from omega.engine.strategy_mcts import MCTSStrategy
from omega.engine.trajectory import ProofAction, ProofState

SOURCE = "theorem t : P := by"


# ── compile-result shaped fakes ────────────────────────────────────────────


@dataclass
class Result:
    """``CompileResult``-shaped object (getattr-friendly, unlike a bare dict)."""

    success: bool
    errors: list
    error_class: str = ""
    exit_code: int = 0
    diagnostics: list | None = None
    line: int = 2

    def __post_init__(self) -> None:
        if self.diagnostics is None:
            self.diagnostics = []


def ok() -> Result:
    return Result(success=True, errors=[], error_class="", exit_code=0)


def fail(errors: int = 1, error_class: str = "unsolved_goal", message: str = "unsolved goals") -> Result:
    return Result(
        success=False,
        errors=[message] * errors,
        error_class=error_class,
        exit_code=1,
        diagnostics=[{"severity": "error", "message": message, "line": 2}],
    )


class ChainCompiler:
    """Fails while any ``culprit`` tactic is present; one error per culprit.

    Mirrors the shape of a serve-style failing proof: *every* prefix containing
    the culprit fails, so the search builds a chain instead of stopping at the
    first tactic.
    """

    def __init__(self, culprits=("alpha",)) -> None:
        self.culprits = set(culprits)
        self.calls: list[str] = []

    def __call__(self, code: str) -> Result:
        self.calls.append(code)
        bad = [t for t in tactics_of_code(code) if t in self.culprits]
        return ok() if not bad else fail(len(bad))


class RaisingCompiler:
    """A compiler that blows up (missing Lean binary, timeout, ...)."""

    def __call__(self, _code: str) -> Result:
        raise RuntimeError("lean binary vanished")


class ErrorShrinkFake:
    """Erasing *anything* shrinks the error count by one but never fixes it.

    The real-Lean shape that exposed the reverse-assertion bug: baseline 2
    errors, erase one tactic -> 1 error, still failing.
    """

    def __call__(self, code: str) -> Result:
        return fail(1 + len(tactics_of_code(code)), error_class="other", message="e")


class CulpritOmegafake:
    """Only ``omega`` is the real culprit: present -> fail, absent -> pass."""

    def __call__(self, code: str) -> Result:
        if "omega" in tactics_of_code(code):
            return fail()
        return ok()


class ExplodingAttributor(CounterfactualAttributor):
    """Attributor that misbehaves *after* render - must be contained by the caller."""

    def attribute(self, *_args, **_kwargs):  # type: ignore[override]
        raise ValueError("attributor blew up")


# ── search wiring helpers ─────────────────────────────────────────────────


def chain_generator(tactics: list[str]):
    """One candidate per call, by **position** (duplicates must not be dropped)."""

    def _gen(state: ProofState) -> list[ProofAction]:
        i = int(state.metadata.get("chain_index") or 0)
        if i >= len(tactics):
            return []
        state.metadata["chain_index"] = i + 1
        return [ProofAction(type="tactic", content=tactics[i], confidence=0.5)]

    return _gen


def transition_over(source: str, compile_fn, *, indent: str = "  ") -> CompileGateTransition:
    """Thin alias over the production adapter (kept for readable test call sites)."""
    return transition_from_source(source, compile_fn, indent=indent)


def run_wiring(
    compile_fn,
    tactics=("alpha", "beta", "gamma"),
    *,
    render=None,
    indent: str = "  ",
    source: str = SOURCE,
    **kw,
):
    """Drive the production flow: MCTS search + L3 attribution."""
    from omega.engine.counterfactual import render_by_append

    strategy = MCTSStrategy(
        action_generator=chain_generator(list(tactics)),
        state_transition=transition_over(source, compile_fn, indent=indent),
        max_iterations=20,
    )
    attributor = CounterfactualAttributor(compile_fn=compile_fn, render=render or render_by_append)
    return strategy.run_diagnosed(source, attributor=attributor, **kw)

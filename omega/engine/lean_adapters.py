"""Adapters wiring MCTS proof search onto the real Lean proof loop (Step 2).

Step 1 shipped the search core (`strategy_mcts`) with three injectable
collaborators. This module supplies the *production* implementations:

    LeanActionGenerator   LLM  -> tactic candidates            (action_generator)
    CompileGateTransition tactic -> compiled state             (state_transition)
    CompileDistanceEvaluator state -> cheap value              (evaluator)

Everything stays injectable: the LLM callable and the compile callable default
to the real backends but can be replaced, so the adapters are testable on a
machine without Lean (see the Step 2 plan). Nothing here imports Lean at module
level.

Value design (no rollouts): a compiled proof scores 1.0; otherwise the value is
a *proximity* table over Lean error classes - errors that usually mean "the
tactic was the right shape, the automation was not enough" (failed synthesis,
type mismatch) score higher than errors that mean "we are lost" (syntax,
unknown identifier), and resource errors (timeout/memory/io) score near zero
because they carry no information about proof progress.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from omega.engine.trajectory import ProofAction, ProofState
from omega.loop.errors import CompileErrorClass

# ── injected-collaborator types ───────────────────────────────────────────
LLMCall = Callable[[str], str]
CompileFn = Callable[[str], Any]  # returns a CompileResult-like object

DEFAULT_MAX_CANDIDATES = 3
DEFAULT_INDENT = "  "

# Proximity of each error class to "proof is nearly done" (higher = closer).
ERROR_PROXIMITY: dict[str, float] = {
    CompileErrorClass.NO_ERROR.value: 1.0,
    CompileErrorClass.FAILED_SYNTHESIS.value: 0.60,  # automation not enough
    CompileErrorClass.UNUSED_VARIABLE.value: 0.55,  # usually a warning
    CompileErrorClass.TYPE_MISMATCH.value: 0.50,  # right shape, wrong type
    CompileErrorClass.AMBIGUOUS.value: 0.45,
    CompileErrorClass.FUNCTION_EXPECTED.value: 0.40,
    CompileErrorClass.UNKNOWN_IDENT.value: 0.35,
    CompileErrorClass.SYNTAX_ERROR.value: 0.30,
    CompileErrorClass.UNIVERSE.value: 0.25,
    CompileErrorClass.CYCLIC_DEP.value: 0.20,
    CompileErrorClass.OTHER.value: 0.15,
    CompileErrorClass.TIMEOUT.value: 0.05,  # no information about progress
    CompileErrorClass.MEMORY.value: 0.05,
    CompileErrorClass.FILE_IO.value: 0.05,
}

# Tactic-ish line: optional bullet/numbering, then 2-200 chars of content.
_TACTIC_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*(.{2,200}?)\s*$")
_FENCE = re.compile(r"^\s*```")


def _prompt_for(state: ProofState, k: int) -> str:
    """Build the tactic-candidate prompt for one proof state."""
    goals = "\n".join(f"  - {g}" for g in (state.goals or [])) or "  (unknown)"
    errs = "\n".join(f"  - {e}" for e in (state.errors or [])[:5]) or "  (none)"
    return (
        "You are a Lean 4 tactic engine. Propose the next tactic(s).\n"
        f"{state.code}\n"
        f"Remaining goals:\n{goals}\n"
        f"Last compile errors:\n{errs}\n"
        f"Return {k} candidate tactics, one per line, no prose."
    )


class LeanActionGenerator:
    """Generate tactic candidates with an LLM.

    Parameters
    ----------
    llm_call : callable ``(prompt) -> text``. Defaults to a lazy import of the
        repository LLM helper at call time (so importing this module needs no
        API key).
    k : number of candidates requested / kept.
    prompt_builder : override the prompt template.

    Robustness: an LLM exception or unparseable output yields an **empty list**
    (never raises) - the search then records a blind spot, which is exactly the
    diagnosis the collector is meant to surface.
    """

    def __init__(
        self,
        llm_call: LLMCall | None = None,
        *,
        k: int = DEFAULT_MAX_CANDIDATES,
        prompt_builder: Callable[[ProofState, int], str] | None = None,
    ) -> None:
        self._llm = llm_call
        self.k = max(1, int(k))
        self._prompt_builder = prompt_builder or _prompt_for
        self.last_error: str | None = None

    def __call__(self, state: ProofState) -> list[ProofAction]:
        self.last_error = None
        prompt = self._prompt_builder(state, self.k)
        try:
            text = (self._llm or _default_llm_call)(prompt)
        except Exception as exc:  # noqa: BLE001 - never break the search
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        return self.parse(text, depth=state.depth)

    @staticmethod
    def parse(text: str, *, depth: int = 0) -> list[ProofAction]:
        """Parse candidate tactics from a model reply (tolerant)."""
        out: list[ProofAction] = []
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or _FENCE.match(line):
                continue
            m = _TACTIC_LINE.match(line)
            if not m:
                continue
            body = m.group(1).strip().strip("`").strip()
            if not body or body.endswith(":"):
                continue
            # drop obvious prose lines
            if len(body.split()) > 24:
                continue
            out.append(
                ProofAction(
                    type="tactic",
                    content=body,
                    description=f"candidate at depth {depth}",
                    metadata={"candidate_index": len(out)},
                )
            )
        return out


class CompileGateTransition:
    """Apply a tactic to the proof code and compile it (no LLM).

    Parameters
    ----------
    compile_fn : ``(code) -> CompileResult-like``. Defaults to a lazy
        ``CompileGate().compile``.
    indent : indentation used when appending a tactic line.

    Failure containment: a compile call that raises (missing Lean binary,
    timeout, permission error) is turned into a *low-value, non-terminal* state
    carrying an ``error_class`` of ``other`` and the exception text - the search
    keeps going and the diagnosis records why.
    """

    def __init__(
        self,
        compile_fn: CompileFn | None = None,
        *,
        indent: str = DEFAULT_INDENT,
        max_errors: int = 5,
    ) -> None:
        self._compile = compile_fn
        self.indent = indent
        self.max_errors = max(1, int(max_errors))

    def __call__(self, state: ProofState, action: ProofAction) -> ProofState:
        code = self.append_tactic(state.code, action.content)
        try:
            result = (self._compile or _default_compile_fn)(code)
        except Exception as exc:  # noqa: BLE001 - search must survive
            return replace(
                state,
                code=code,
                depth=state.depth + 1,
                errors=[f"compile_error: {type(exc).__name__}: {exc}"],
                error_class=CompileErrorClass.OTHER.value,
                is_terminal=False,
                metadata={**state.metadata, "compile_exception": type(exc).__name__},
            )
        return self.state_from_result(state, code, result)

    def append_tactic(self, code: str, tactic: str) -> str:
        """Append a tactic line under the current proof body."""
        tactic = (tactic or "").strip()
        if not tactic:
            return code
        return f"{code}\n{self.indent}{tactic}" if code.strip() else f"{self.indent}{tactic}"

    def state_from_result(self, state: ProofState, code: str, result: Any) -> ProofState:
        """Build the successor state from a CompileResult-like object."""
        ok = bool(getattr(result, "success", False))
        errors = [str(e) for e in (getattr(result, "errors", None) or [])][: self.max_errors]
        raw_class = getattr(result, "error_class", None)
        class_name = getattr(raw_class, "value", None) or (str(raw_class) if raw_class else "")
        if not ok and not class_name:
            class_name = CompileErrorClass.OTHER.value
        goals = list(getattr(result, "diagnostics", None) or []) and list(state.goals or []) or []
        metadata = dict(state.metadata)
        line = getattr(result, "line", 0)
        if isinstance(line, (int, float)) and int(line) > 0:
            metadata["compile_error_line"] = int(line)
        elapsed = getattr(result, "elapsed_ms", None)
        if isinstance(elapsed, (int, float)):
            metadata["compile_elapsed_ms"] = int(elapsed)
        cached = getattr(result, "cached", None)
        if cached is not None:
            metadata["compile_cached"] = bool(cached)
        return replace(
            state,
            code=code,
            depth=state.depth + 1,
            errors=[] if ok else errors,
            error_class="" if ok else class_name,
            goals=goals,
            is_terminal=ok,
            value=1.0 if ok else 0.0,
            metadata=metadata,
        )


class CompileDistanceEvaluator:
    """Cheap state value from the compile outcome (no rollout, no LLM).

    ``1.0`` when the state is a finished proof; otherwise a proximity score
    derived from the error class, nudged by how few goals remain and by how deep
    the failing line sits (later failures are closer to done).
    """

    #: failing line at or beyond this is treated as "deep into the proof"
    LINE_SATURATION = 40.0

    def __init__(
        self,
        *,
        proximity: dict[str, float] | None = None,
        goal_bonus: float = 0.05,
        line_bonus: float = 0.02,
        unknown_penalty: float = 0.15,
    ) -> None:
        self.proximity = dict(proximity or ERROR_PROXIMITY)
        self.goal_bonus = float(goal_bonus)
        self.line_bonus = float(line_bonus)
        self.unknown_penalty = float(unknown_penalty)

    def __call__(self, state: ProofState) -> float:
        if state.is_terminal:
            return 1.0
        base = self.proximity.get(state.error_class)
        if base is None:
            # Unrecognised class: no proximity knowledge, so no bonuses either -
            # a flat, conservative penalty keeps the ordering honest.
            return max(0.0, min(0.999, self.unknown_penalty))
        bonus = 0.0
        if state.goals:
            bonus += self.goal_bonus * (1.0 / (1.0 + len(state.goals)))
        line = state.metadata.get("compile_error_line")
        if isinstance(line, (int, float)) and line > 0:
            bonus += self.line_bonus * min(1.0, float(line) / self.LINE_SATURATION)
        # keep strict separation from a solved state
        return max(0.0, min(0.999, base + bonus))


def build_lean_mcts(
    *,
    llm_call: LLMCall | None = None,
    compile_fn: CompileFn | None = None,
    k: int = DEFAULT_MAX_CANDIDATES,
    max_iterations: int = 32,
    c: float = 1.4,
):
    """Assemble an :class:`MCTSStrategy` wired onto the Lean proof loop.

    All collaborators are injectable; with no arguments it uses the repository
    LLM helper and ``CompileGate`` (which requires a local Lean toolchain - see
    the Step 2 plan for the run requirements).
    """
    from omega.engine.strategy_mcts import MCTSStrategy

    return MCTSStrategy(
        action_generator=LeanActionGenerator(llm_call, k=k),
        state_transition=CompileGateTransition(compile_fn),
        evaluator=CompileDistanceEvaluator(),
        max_iterations=max_iterations,
        c=c,
        name="MCTS (Lean)",
    )


# ── default backends (lazy; never imported at module load) ──────────────
def _default_llm_call(prompt: str) -> str:
    """Default LLM backend: the repository helper, imported lazily."""
    import omega.llm as llm  # noqa: PLC0415 - lazy by design

    for name in ("complete", "generate", "call", "chat"):
        fn = getattr(llm, name, None)
        if callable(fn):
            return str(fn(prompt))
    raise RuntimeError("omega.llm exposes no known completion entry point")


def _default_compile_fn(code: str) -> Any:
    """Default compile backend: CompileGate (needs a local Lean toolchain)."""
    from omega.loop.compile_gate import CompileGate  # noqa: PLC0415 - lazy

    return CompileGate().compile(code)

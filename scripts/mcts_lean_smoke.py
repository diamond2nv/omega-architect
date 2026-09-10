#!/usr/bin/env python3
"""Smoke test: MCTS + diagnosis against a *real* Lean toolchain (Step 2 e2e).

Run this on a machine that has Lean installed (``lean`` / ``lake`` on PATH, or a
working ``LEAN_LSP_MCP``). It exercises the whole Step 1 + Step 2 stack:

    L  MCTS search core            (omega/engine/strategy_mcts.py)
    L  diagnosis export            (omega/engine/mcts_diagnosis.py)
    L  Lean adapters               (omega/engine/lean_adapters.py)
    L  CompileGate                 (omega/loop/compile_gate.py)  <- needs Lean

Two generator modes:

* ``--generator fixed`` (default) - cycles a built-in tactic list. No LLM, no
  API key: isolates the Lean + MCTS + diagnosis path.
* ``--generator llm`` - uses ``LeanActionGenerator`` (repository LLM helper).

Examples
--------
    python scripts/mcts_lean_smoke.py --help
    python scripts/mcts_lean_smoke.py --theorem "theorem t : 1 + 1 = 2 := by" \
        --iterations 6 --imports "import Mathlib"
    python scripts/mcts_lean_smoke.py --generator llm --iterations 12

Exit codes: 0 = MCTS solved the theorem; 1 = not solved (diagnosis printed);
2 = environment problem (no Lean).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Run from a checkout (`python scripts/...`) as well as from an install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omega.engine.lean_adapters import (  # noqa: E402
    CompileDistanceEvaluator,
    CompileGateTransition,
    LeanActionGenerator,
)
from omega.engine.mcts_diagnosis import DiagnosisView  # noqa: E402
from omega.engine.strategy_mcts import MCTSStrategy
from omega.engine.trajectory import ProofAction, ProofState, Trajectory

DEFAULT_THEOREM = "theorem smoke_add : 1 + 1 = 2 := by"
DEFAULT_IMPORTS = "import Mathlib"
BUILTIN_TACTICS = [
    "norm_num",
    "rfl",
    "simp",
    "omega",
    "ring",
    "linarith",
    "decide",
    "aesop",
]


def lean_available() -> bool:
    """True when a Lean toolchain looks reachable from this shell."""
    return any(shutil.which(binary) for binary in ("lean", "lake", "elan"))


def build_theorem_source(imports: str, theorem: str) -> str:
    return f"{imports}\n\n{theorem}" if imports.strip() else theorem


class FixedGenerator:
    """Deterministic candidate source: cycles tactics, minus those already tried."""

    def __init__(self, tactics: list[str] | None = None) -> None:
        self.tactics = list(tactics or BUILTIN_TACTICS)

    def __call__(self, state: ProofState) -> list[ProofAction]:
        tried = set(state.metadata.get("tried_tactics") or [])
        # one candidate per expansion keeps the search a clean tactic sweep
        for tactic in self.tactics:
            if tactic not in tried:
                return [ProofAction(type="tactic", content=tactic, confidence=0.5)]
        return []


def make_transition(compile_fn, theorem_source: str) -> CompileGateTransition:
    """Transition that appends tactics to the *theorem source*, not the header."""
    transition = CompileGateTransition(compile_fn)

    original_append = transition.append_tactic

    def append(code: str, tactic: str) -> str:  # type: ignore[no-untyped-def]
        if not code.strip():
            return original_append(theorem_source, tactic)
        return original_append(code, tactic)

    transition.append_tactic = append  # type: ignore[method-assign]
    return transition


def run(theorem: str, imports: str, iterations: int, c: float, generator: str) -> int:
    if not lean_available():
        print(
            "[env] no Lean toolchain found (looked for: lean, lake, elan).\n"
            "      This smoke test needs a machine with Lean installed.\n"
            "      Set LEAN_LSP_MCP / install via elan, then re-run."
        )
        return 2

    from omega.loop.compile_gate import CompileGate

    gate = CompileGate()
    source = build_theorem_source(imports, theorem)

    gen = LeanActionGenerator(k=3) if generator == "llm" else FixedGenerator()

    strategy = MCTSStrategy(
        action_generator=_tracking_generator(gen),
        state_transition=make_transition(gate.compile, source),
        evaluator=CompileDistanceEvaluator(),
        max_iterations=iterations,
        c=c,
        name=f"MCTS (Lean, {generator})",
    )

    print(f"[run] theorem: {theorem}")
    print(f"[run] generator={generator} iterations={iterations} c={c}")
    trajectory, diagnosis = strategy.run_diagnosed(source)
    _report(trajectory, diagnosis)

    if trajectory.success:
        print("\n[result] SOLVED")
        return 0
    print("\n[result] not solved - diagnosis above is the deliverable")
    return 1


def _tracking_generator(gen):  # type: ignore[no-untyped-def]
    """Wrap a generator so the tactics it returns are recorded in state metadata."""

    def _wrapped(state: ProofState) -> list[ProofAction]:
        actions = list(gen(state) or [])
        tried = list(state.metadata.get("tried_tactics") or [])
        for action in actions:
            if action.content and action.content not in tried:
                tried.append(action.content)
        state.metadata["tried_tactics"] = tried
        return actions

    return _wrapped


def _report(trajectory: Trajectory, diagnosis: DiagnosisView) -> None:
    print("\n[trajectory]")
    print(f"  success={trajectory.success} steps={trajectory.depth} "
          f"elapsed_ms={trajectory.elapsed_ms} strategy={trajectory.strategy!r}")
    if trajectory.proof:
        print("  proof:")
        for line in trajectory.proof.splitlines():
            print(f"    {line}")
    print("\n[diagnosis]")
    print(f"  {diagnosis.summary()}")
    for node in diagnosis.stuck_nodes[:5]:
        print(f"  - {node.line()}")
    for spot in diagnosis.blind_spots[:5]:
        print(f"  - {spot.line()}")
    if diagnosis.failure_chain:
        print(f"  failure chain: {' -> '.join(diagnosis.failure_chain)}")
    for key, count in diagnosis.top_error_buckets():
        print(f"  heat: {key} ×{count}")
    print(f"\n  note: {diagnosis.falsifiability_note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--theorem", default=DEFAULT_THEOREM, help="Lean theorem header")
    parser.add_argument("--imports", default=DEFAULT_IMPORTS, help="import preamble ('' to disable)")
    parser.add_argument("--iterations", type=int, default=6, help="MCTS iteration budget")
    parser.add_argument("--c", type=float, default=1.4, help="UCB1 exploration constant")
    parser.add_argument("--generator", choices=("fixed", "llm"), default="fixed")
    args = parser.parse_args(argv)
    return run(args.theorem, args.imports, args.iterations, args.c, args.generator)


if __name__ == "__main__":
    sys.exit(main())

"""Experiment: Hybrid (Mode C) vs pure Dialogue (Mode A) comparison.

Tests 4 MiniF2F theorems across difficulty levels and compares:
- Pass rate
- Attempts
- Time
- Cost (token count proxy)
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field, asdict

# Add project root
_HERE = pathlib.Path(__file__).resolve().parent
_PROJECT = _HERE.parent  # omega-architect root (parent of experiments/)

# Run from project root to avoid module resolution issues
os.chdir(str(_PROJECT))
sys.path.insert(0, str(_PROJECT))

# Break circular import chain: pre-import budget before loop/plan chain
import omega.resource.budget  # noqa: F401, E402

from omega.engine.hybrid import run_hybrid, HybridConfig, HybridResult
from omega.loop.inner import inner_loop, InnerLoopConfig, InnerLoopResult


# ── Load theorems from MiniF2F dataset ─────────────────────────

DATASET_PATH = pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / \
    "formal-provers" / "goedel-prover-v2" / "dataset" / "minif2f.jsonl"


def load_theorems(max_n: int = 10) -> list[dict]:
    """Load theorems from MiniF2F JSONL."""
    theorems = []
    with open(DATASET_PATH) as f:
        for line in f:
            data = json.loads(line)
            # Use formal_statement as the theorem header
            statement = data.get("formal_statement", "")
            if not statement:
                continue
            theorems.append({
                "name": data.get("name", "unknown"),
                "statement": statement,
                "split": data.get("split", "unknown"),
            })
            if len(theorems) >= max_n:
                break
    return theorems


# ── Pick diverse theorem subset ─────────────────────────────────

# These 4 theorems span easy/medium/hard domains from earlier experiments
THEOREM_NAMES = [
    "mathd_algebra_478",      # algebra, usually easy
    "amc12_2001_p5",          # combinatorics, medium
    "aime_1983_p1",           # number theory, medium
    "imo_1959_p1",            # olympiad, hard
]


def find_theorems(names: list[str]) -> list[dict]:
    """Find specific theorems by name from the dataset."""
    found: dict[str, dict] = {}
    with open(DATASET_PATH) as f:
        for line in f:
            data = json.loads(line)
            name = data.get("name", "")
            if name in names and name not in found:
                found[name] = {
                    "name": name,
                    "statement": data.get("formal_statement", ""),
                    "split": data.get("split", "unknown"),
                }
    return [found[n] for n in names if n in found]


# ── Experiment config ───────────────────────────────────────────


@dataclass
class ExperimentResult:
    theorem_name: str
    mode: str  # "dialogue" or "hybrid"
    success: bool
    proof_length: int = 0
    n_attempts: int = 0
    elapsed_s: float = 0.0
    error: str = ""


# ── Run a single theorem ────────────────────────────────────────


def run_dialogue(statement: str, max_rounds: int = 50) -> ExperimentResult:
    """Run pure Dialogue (Mode A) on a theorem."""
    t0 = time.perf_counter()
    config = InnerLoopConfig(max_rounds=max_rounds)
    try:
        result: InnerLoopResult = inner_loop(
            theorem_header=statement,
            config=config,
        )
        elapsed = time.perf_counter() - t0
        return ExperimentResult(
            theorem_name=statement.split(":")[0].replace("theorem ", "").strip()[:40],
            mode="dialogue",
            success=result.success,
            proof_length=len(result.code or ""),
            n_attempts=result.rounds,
            elapsed_s=round(elapsed, 1),
            error=(result.error or "")[:200],
        )
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return ExperimentResult(
            theorem_name="error",
            mode="dialogue",
            success=False,
            elapsed_s=round(elapsed, 1),
            error=f"Exception: {e}",
        )


def run_hybrid_mode(statement: str) -> ExperimentResult:
    """Run Hybrid (Mode C) on a theorem."""
    t0 = time.perf_counter()
    config = HybridConfig(
        num_samples=6,
        max_correction_rounds=2,
        max_dialogue_rounds=50,
    )
    try:
        result: HybridResult = run_hybrid(
            theorem_header=statement,
            config=config,
        )
        elapsed = time.perf_counter() - t0
        name_parts = statement.split(":")[0].replace("theorem ", "").strip()
        return ExperimentResult(
            theorem_name=name_parts[:40],
            mode="hybrid",
            success=result.success,
            proof_length=len(result.proof or ""),
            n_attempts=result.n_attempts,
            elapsed_s=round(elapsed, 1),
            error=f"source={result.source}" if not result.success else "",
        )
    except Exception as e:
        elapsed = time.perf_counter() - t0
        import traceback
        return ExperimentResult(
            theorem_name="error",
            mode="hybrid",
            success=False,
            elapsed_s=round(elapsed, 1),
            error=f"Exception: {traceback.format_exc()[:300]}",
        )


# ── Main ────────────────────────────────────────────────────────


def main():
    print("=" * 70)
    print("Experiment: Hybrid (Mode C) vs pure Dialogue (Mode A)")
    print("=" * 70)

    theorems = find_theorems(THEOREM_NAMES)
    print(f"\nLoaded {len(theorems)}/{len(THEOREM_NAMES)} theorems:")
    for t in theorems:
        name = t["name"]
        stmt = t["statement"][:80].replace("\n", " ")
        print(f"  {name}: {stmt}...")
    print()

    results: list[ExperimentResult] = []

    for theorem in theorems:
        name = theorem["name"]
        statement = theorem["statement"]
        print(f"\n{'─' * 60}")
        print(f" Theorem: {name}")
        print(f"{'─' * 60}")

        # Mode A: Dialogue
        print(f"\n  ▶ Mode A (Dialogue)...")
        r1 = run_dialogue(statement)
        print(f"    {'✅' if r1.success else '❌'} success={r1.success} "
              f"attempts={r1.n_attempts} time={r1.elapsed_s}s")
        if r1.error:
            print(f"    error: {r1.error[:120]}")
        results.append(r1)

        # Mode C: Hybrid
        print(f"\n  ▶ Mode C (Hybrid)...")
        r2 = run_hybrid_mode(statement)
        print(f"    {'✅' if r2.success else '❌'} success={r2.success} "
              f"attempts={r2.n_attempts} time={r2.elapsed_s}s "
              f"error={r2.error[:120]}")
        results.append(r2)

    # ── Summary ─────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    header = f"{'Theorem':<30} {'Dialogue':<20} {'Hybrid':<20}"
    print(f"\n{header}")
    print("-" * 70)

    d_ok = h_ok = 0
    for i in range(0, len(results), 2):
        d = results[i]
        h = results[i + 1] if i + 1 < len(results) else None
        theorem_name = THEOREM_NAMES[i // 2] if i // 2 < len(THEOREM_NAMES) else d.theorem_name
        d_str = f"{'✅' if d.success else '❌'} {d.n_attempts}a {d.elapsed_s}s"
        h_str = f"{'✅' if h and h.success else '❌'} {(h.n_attempts if h else 0)}a {(h.elapsed_s if h else 0)}s"
        print(f"{theorem_name:<30} {d_str:<20} {h_str:<20}")
        if d.success:
            d_ok += 1
        if h and h.success:
            h_ok += 1

    print("-" * 70)
    total = len(THEOREM_NAMES)
    print(f"{'Total':<30} {f'{d_ok}/{total} ({100*d_ok//total}%)':<20} {f'{h_ok}/{total} ({100*h_ok//total}%)':<20}")
    print()

    # Save results
    out_path = _HERE / "results" / f"hybrid_vs_dialogue_{int(time.time())}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_data = [asdict(r) for r in results]
    out_path.write_text(json.dumps(out_data, indent=2, ensure_ascii=False))
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()

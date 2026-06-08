#!/usr/bin/env python3
"""Label generation for ModelRouter LightGBM training — Stage 2.

Generates 3-tier labels (0=simple, 1=medium, 2=hard) for all cached
training examples, using Omega's existing heuristic routing system.

Strategy per source:
  RL_dataset_V2        → heuristic (_estimate_complexity)
  Lean-workbook-proofs → heuristic (_estimate_complexity)
  Herald_statements    → heuristic (_estimate_complexity)
  Omega_Benchmarks     → gold labels (hand-curated)

No LLM API calls — all labeling is local and deterministic.

Usage::

    # Generate labels for all cached datasets
    python -m omega.research.training.label_generation generate

    # Show stats only
    python -m omega.research.training.label_generation stats
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ── Paths ──────────────────────────────────────────────────────

_CACHE_DIR = Path.home() / ".omega" / "training"
_LABELED_DIR = _CACHE_DIR / "labeled"
_OMEGA_BENCH_PATH = _CACHE_DIR / "omega_bench" / "data.jsonl"

# ── Regex for theorem header extraction ─────────────────────────

_RE_THEOREM_HEADER = re.compile(
    r"(theorem\s+\w[\w']*\s+[^:=]*?(?::=\s*))",
    re.DOTALL,
)
_RE_THEOREM_HEADER_SIMPLE = re.compile(
    r"(theorem\s+\w[\w']*.*?)\s*:=",
    re.DOTALL,
)

# ── RL_dataset_V2 subset → difficulty proxy ────────────────────

_SUBSET_TIER_MAP: dict[str, int] = {
    "lean4_revision": 2,      # Needed revision → hard
    "lean4_completion": 1,    # Straight completion → medium
}


# ═══════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════


@dataclass
class LabeledExample:
    """A single labeled training example."""
    name: str
    header: str
    tier: int                    # 0=simple, 1=medium, 2=hard
    source: str                  # Dataset source name
    label_method: str            # "gold" | "heuristic" | "subset_proxy"
    confidence: float = 0.0      # Label confidence (0-1)
    header_cleaned: str = ""     # Clean header for display
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "header": self.header,
            "header_cleaned": self.header_cleaned or self.header[:120],
            "tier": self.tier,
            "source": self.source,
            "label_method": self.label_method,
            "confidence": round(self.confidence, 3),
            "metadata": self.metadata,
        }


# ═══════════════════════════════════════════════════════════════
# Theorem header extraction
# ═══════════════════════════════════════════════════════════════


def extract_theorem_header(text: str) -> tuple[str, float]:
    """Extract the Lean 4 theorem header from text.

    Returns
    -------
    tuple[str, float]
        (extracted_header, confidence)
        confidence ∈ [0, 1] — 1.0 if direct match, lower if extracted
        from chat context.
    """
    # Try direct theorem header
    m = _RE_THEOREM_HEADER.search(text)
    if m:
        return m.group(1).strip(), 0.9
    # Try simpler pattern
    m = _RE_THEOREM_HEADER_SIMPLE.search(text)
    if m:
        return m.group(1).strip() + " := by", 0.8
    return "", 0.0


def extract_name_from_header(header: str) -> str:
    """Extract a short name from a theorem header."""
    m = re.search(r"theorem\s+(\w[\w']*)", header)
    return m.group(1) if m else "unnamed"


# ═══════════════════════════════════════════════════════════════
# Source-specific readers
# ═══════════════════════════════════════════════════════════════


def _read_rl_v2(path: Path) -> list[dict]:
    """Read RL_dataset_V2 — extract theorem header from chat messages."""
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            subset = obj.get("subset", "unknown")
            messages = obj.get("messages", [])

            # Extract theorem header from first user message
            header_text = ""
            for msg in messages:
                if msg.get("role") == "user":
                    header_text = msg.get("content", "")
                    break

            header, conf = extract_theorem_header(header_text)
            if not header:
                continue

            name = extract_name_from_header(header)
            results.append({
                "name": name,
                "header": header,
                "header_clean": header[:120],
                "source": "RL_dataset_V2",
                "subset": subset,
                "extract_confidence": conf,
            })
    return results


def _read_lean_workbook(path: Path) -> list[dict]:
    """Read Lean-workbook-proofs — extract from full_proof field."""
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            full_proof = obj.get("full_proof", "")
            problem_id = obj.get("problem_id", "")

            # The first line of the proof usually has: theorem ... :=
            first_line = full_proof.split("\n")[0] if full_proof else ""
            header, conf = extract_theorem_header(first_line)
            if not header:
                # Fall back to extracting from full proof text
                header, conf = extract_theorem_header(full_proof)
            if not header:
                continue

            results.append({
                "name": problem_id or extract_name_from_header(header),
                "header": header,
                "header_clean": header[:120],
                "source": "Lean-workbook-proofs",
                "problem_id": problem_id,
                "extract_confidence": conf,
            })
    return results


def _read_herald(path: Path) -> list[dict]:
    """Read Herald_statements — extract from formal_statement field."""
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            formal = obj.get("formal_statement", "")
            informal = obj.get("informal_statement", "")
            example_id = obj.get("id", "")

            header, conf = extract_theorem_header(formal)
            if not header:
                continue

            results.append({
                "name": f"herald_{example_id}" if example_id != "" else extract_name_from_header(header),
                "header": header,
                "header_clean": header[:120],
                "source": "Herald_statements",
                "example_id": example_id,
                "extract_confidence": conf,
            })
    return results


def _read_omega_bench(path: Path) -> list[dict]:
    """Read Omega Benchmarks — already has tier labels."""
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            results.append({
                "name": obj.get("name", ""),
                "header": obj.get("header", ""),
                "header_clean": obj.get("header", "")[:120],
                "source": "Omega_Benchmarks",
                "gold_tier": obj.get("tier", -1),
                "extract_confidence": 1.0,
            })
    return results


# ═══════════════════════════════════════════════════════════════
# Label generation
# ═══════════════════════════════════════════════════════════════


def _heuristic_tier(header: str) -> tuple[int, float]:
    """Use Omega's existing routing system to label a theorem header.

    Returns (tier, confidence) where tier ∈ {0, 1, 2}.
    """
    # Import Omega's routing machinery
    from omega.resource.model_router import _estimate_complexity  # type: ignore[import-untyped]
    from omega.resource.routing_flags import TIER_NAMES

    tier_str, flags, base_tier = _estimate_complexity(header)
    tier = {"simple": 0, "medium": 1, "hard": 2}.get(tier_str, 1)

    # Confidence = pattern analysis confidence
    from omega.search.proposer import analyze_theorem_pattern
    pattern = analyze_theorem_pattern(header)
    conf = pattern.get("confidence", 0.5)

    return tier, conf


def _rl_subset_tier(subset: str) -> int:
    """Proxy tier based on RL_dataset_V2 subset type."""
    return _SUBSET_TIER_MAP.get(subset, 1)


def _is_complex_header(header: str) -> bool:
    """Quick heuristic check: is this header likely complex (tier 2)?"""
    indicators = [
        "induction" in header.lower(),
        "∀" in header,
        "∃" in header,
        header.count("∧") > 2,
        len(header) > 300,
    ]
    return sum(indicators) >= 2


def generate_labels(
    rl_path: Path | None = None,
    workbook_path: Path | None = None,
    herald_path: Path | None = None,
    omega_path: Path | None = None,
    output_dir: Path | None = None,
) -> list[LabeledExample]:
    """Generate labels for all cached datasets.

    Parameters
    ----------
    rl_path : Path, optional
        Path to RL_dataset_V2 JSONL.
    workbook_path : Path, optional
        Path to Lean-workbook-proofs JSONL.
    herald_path : Path, optional
        Path to Herald_statements JSONL.
    omega_path : Path, optional
        Path to Omega_Benchmarks JSONL.
    output_dir : Path, optional
        Output directory for labeled data.

    Returns
    -------
    list[LabeledExample]
        All labeled examples.
    """
    if output_dir is None:
        output_dir = _LABELED_DIR

    output_dir.mkdir(parents=True, exist_ok=True)

    all_examples: list[LabeledExample] = []

    # ── Source 1: RL_dataset_V2 ────────────────────────────
    if rl_path and rl_path.exists():
        print(f"[RL_dataset_V2] Reading {rl_path} ...")
        raw = _read_rl_v2(rl_path)
        print(f"[RL_dataset_V2]  {len(raw)} theorem headers extracted")
        for r in raw:
            tier, conf = _heuristic_tier(r["header"])
            all_examples.append(LabeledExample(
                name=r["name"],
                header=r["header"],
                header_cleaned=r["header_clean"],
                tier=tier,
                source="RL_dataset_V2",
                label_method="heuristic",
                confidence=conf,
                metadata={"subset": r.get("subset", ""), "extract_conf": r["extract_confidence"]},
            ))
        # Also add subset-proxy labels for comparison
        for r in raw:
            tier_proxy = _rl_subset_tier(r.get("subset", "lean4_completion"))
            all_examples.append(LabeledExample(
                name=r["name"] + "_proxy",
                header=r["header"],
                header_cleaned=r["header_clean"],
                tier=tier_proxy,
                source="RL_dataset_V2",
                label_method="subset_proxy",
                confidence=0.6,
                metadata={"subset": r.get("subset", ""), "parent_name": r["name"]},
            ))

    # ── Source 2: Lean-workbook-proofs ─────────────────────
    if workbook_path and workbook_path.exists():
        print(f"[Lean-workbook] Reading {workbook_path} ...")
        raw = _read_lean_workbook(workbook_path)
        print(f"[Lean-workbook]  {len(raw)} theorem headers extracted")
        for r in raw:
            tier, conf = _heuristic_tier(r["header"])
            all_examples.append(LabeledExample(
                name=r["name"],
                header=r["header"],
                header_cleaned=r["header_clean"],
                tier=tier,
                source="Lean-workbook-proofs",
                label_method="heuristic",
                confidence=conf,
                metadata={"problem_id": r.get("problem_id", ""), "extract_conf": r["extract_confidence"]},
            ))

    # ── Source 3: Herald_statements ────────────────────────
    if herald_path and herald_path.exists():
        print(f"[Herald] Reading {herald_path} ...")
        raw = _read_herald(herald_path)
        print(f"[Herald]  {len(raw)} theorem headers extracted")
        for r in raw:
            tier, conf = _heuristic_tier(r["header"])
            all_examples.append(LabeledExample(
                name=r["name"],
                header=r["header"],
                header_cleaned=r["header_clean"],
                tier=tier,
                source="Herald_statements",
                label_method="heuristic",
                confidence=conf,
                metadata={"example_id": r.get("example_id", ""), "extract_conf": r["extract_confidence"]},
            ))

    # ── Source 4: Omega Benchmarks (gold) ──────────────────
    if omega_path and omega_path.exists():
        print(f"[Omega_Bench] Reading {omega_path} ...")
        raw = _read_omega_bench(omega_path)
        print(f"[Omega_Bench]  {len(raw)} gold-standard theorems")
        for r in raw:
            gold_tier = r.get("gold_tier", -1)
            if gold_tier < 0:
                continue
            all_examples.append(LabeledExample(
                name=r["name"],
                header=r["header"],
                header_cleaned=r["header_clean"],
                tier=gold_tier,
                source="Omega_Benchmarks",
                label_method="gold",
                confidence=1.0,
                metadata={"gold_tier": gold_tier},
            ))

    # ── Write output ───────────────────────────────────────
    out_path = output_dir / "data.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for ex in all_examples:
            f.write(json.dumps(ex.to_dict(), ensure_ascii=False) + "\n")
    print(f"\n[Output] {len(all_examples)} examples → {out_path}")
    print(f"[Output]   heuristic: {sum(1 for e in all_examples if e.label_method == 'heuristic')}")
    print(f"[Output]   subset_proxy: {sum(1 for e in all_examples if e.label_method == 'subset_proxy')}")
    print(f"[Output]   gold: {sum(1 for e in all_examples if e.label_method == 'gold')}")

    return all_examples


# ═══════════════════════════════════════════════════════════════
# Stats
# ═══════════════════════════════════════════════════════════════


def stats(examples: list[LabeledExample] | None = None) -> dict:
    """Compute label distribution statistics."""
    if examples is None:
        path = _LABELED_DIR / "data.jsonl"
        if not path.exists():
            return {"error": "No labeled data found. Run 'generate' first."}
        examples = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    examples.append(LabeledExample(**json.loads(line)))

    tiers = {0: 0, 1: 0, 2: 0}
    sources: dict[str, dict[int, int]] = {}
    methods: dict[str, int] = {}

    for ex in examples:
        tiers[ex.tier] = tiers.get(ex.tier, 0) + 1
        methods[ex.label_method] = methods.get(ex.label_method, 0) + 1
        if ex.source not in sources:
            sources[ex.source] = {0: 0, 1: 0, 2: 0}
        sources[ex.source][ex.tier] = sources[ex.source].get(ex.tier, 0) + 1

    return {
        "total": len(examples),
        "tier_distribution": tiers,
        "per_source": sources,
        "per_method": methods,
    }


def print_stats(s: dict) -> None:
    """Print a formatted stats table."""
    print("=" * 60)
    print("  Label Generation — Stats")
    print("=" * 60)
    print(f"  Total examples: {s['total']}")
    print(f"  Label methods:  {s['per_method']}")
    print(f"  Distribution:   Tier 0 (simple): {s['tier_distribution'].get(0, 0)}")
    print(f"                   Tier 1 (medium): {s['tier_distribution'].get(1, 0)}")
    print(f"                   Tier 2 (hard):   {s['tier_distribution'].get(2, 0)}")
    print()
    print(f"  {'Source':<26} {'T0':>5} {'T1':>5} {'T2':>5} {'Total':>8}")
    print("  " + "-" * 49)
    for source, tiers in sorted(s.get("per_source", {}).items()):
        t = (tiers.get(0, 0), tiers.get(1, 0), tiers.get(2, 0))
        print(f"  {source:<26} {t[0]:>5} {t[1]:>5} {t[2]:>5} {sum(t):>8}")
    print()


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"

    if cmd == "generate":
        rl_path = _CACHE_DIR / "rl_v2" / "data.jsonl"
        workbook_path = _CACHE_DIR / "lean_workbook" / "data.jsonl"
        herald_path = _CACHE_DIR / "herald" / "data.jsonl"
        omega_path = _CACHE_DIR / "omega_bench" / "data.jsonl"

        t0 = time.time()
        examples = generate_labels(
            rl_path=rl_path,
            workbook_path=workbook_path,
            herald_path=herald_path,
            omega_path=omega_path,
        )
        elapsed = time.time() - t0
        print(f"\n  Generated in {elapsed:.1f}s ({len(examples)/max(elapsed,0.1):.0f} examples/s)")
        print()
        s = stats(examples)
        print_stats(s)

    elif cmd == "stats":
        s = stats()
        if "error" in s:
            print(s["error"])
            return 1
        print_stats(s)

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python -m omega.research.training.label_generation [generate|stats]")
        return 1
    return 0


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Analyze MiniF2F benchmark results for Phase C Step 5 — dynamic target setting.

Usage
-----
    # Latest benchmark
    python benchmarks/minif2f/analyze_results.py

    # Specific file
    python benchmarks/minif2f/analyze_results.py --file path/to/results.json

Output
------
    - Error taxonomy table with counts per error type
    - Pass rate per domain (algebra, number theory, combinatorics, analysis)
    - Dynamic target recommendation based on Phase 1 roadmap rules
    - Model switching decision if pass rate is low
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_RESULTS_DIR = _HERE / "results"


# ── Domain classification ──────────────────────────────────────


_DOMAIN_PATTERNS: dict[str, list[str]] = {
    "algebra": [
        "mathd_algebra", "algebra", "polynomial", "equation", "quadratic",
        "factor", "gcd", "lcm", "mod",
    ],
    "number_theory": [
        "numbertheory", "number_theory", "prime", "divisible", "modular",
        "congruence", "nt",
    ],
    "combinatorics": [
        "combinator", "binomial", "permutation", "combination", "pigeon",
        "catalan", "sum", "inequality",
    ],
    "analysis": [
        "analysis", "calculus", "limit", "continu", "derivative", "integral",
        "sequence", "converge", "bound", "sup", "inf", "topolog",
    ],
}


def classify_domain(name: str) -> str:
    """Classify a theorem name into a domain category."""
    name_lower = name.lower()
    for domain, patterns in _DOMAIN_PATTERNS.items():
        for pat in patterns:
            if pat in name_lower:
                return domain
    return "other"


# ── Error taxonomy ────────────────────────────────────────────


_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (r"unsolved goal|unsolved goals", "unsolved_goal",
     "Proof started but not completed"),
    (r"syntax|expected '{'|expected '}'|expected term|expected command",
     "syntax", "Malformed Lean syntax"),
    (r"type mismatch|type error|type mismatch", "type_mismatch",
     "Type annotation mismatch"),
    (r"unknown identifier|unknown constant", "unknown_identifier",
     "Undefined symbol or missing import"),
    (r"unknown tactic", "unknown_tactic", "Tactic not in scope"),
    (r"unknown module|unknown package", "unknown_module", "Missing import"),
    (r"failed to synthesize|instance.*not found", "instance_synth",
     "Typeclass synthesis failure"),
    (r"don't know how to synthesize|don't know how to", "synth_failure",
     "Don't know how to synthesize placeholder"),
    (r"maximum recursion depth|recursion limit", "recursion_depth",
     "Recursion limit exceeded"),
    (r"redundant|cannot be used|already have|duplicate", "redundant",
     "Redundant or duplicate clause"),
    (r"Prover: no proof generated|no proof generated", "no_proof",
     "Prover did not find any proof candidate"),
    (r"timeout|timed out|exceeded max", "timeout", "Time budget exceeded"),
]


def classify_error(error_msg: str) -> str:
    """Classify an error message into a taxonomy category."""
    for pattern, category, _desc in _ERROR_PATTERNS:
        if re.search(pattern, error_msg, re.IGNORECASE):
            return category
    return "other"


# ── Analysis ───────────────────────────────────────────────────


def load_latest_results() -> dict[str, Any] | None:
    """Find the most recent full-mode benchmark result file."""
    full_results = sorted(
        _RESULTS_DIR.glob("benchmark_full_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not full_results:
        print(f"No benchmark_full_*.json files found in {_RESULTS_DIR}")
        return None
    latest = full_results[-1]
    print(f"Loading: {latest}")
    with open(latest) as f:
        return json.load(f)


def analyze(report: dict[str, Any]) -> None:
    """Full analysis of a benchmark report."""
    results = report.get("results", [])
    total = len(results)
    t1_pass = sum(1 for r in results if r.get("t1_verified"))
    t2_pass = sum(1 for r in results if r.get("t2_verified"))

    # ── Overview ──
    print("\n" + "=" * 65)
    print("  MiniF2F Benchmark Analysis — Phase C Step 5")
    print("=" * 65)
    print(f"  Total problems:    {total}")
    print(f"  T1 pass:           {t1_pass}/{total} ({t1_pass/total*100:.1f}%)" if total else "  T1 pass: 0/0")
    print(f"  T2 pass:           {t2_pass}/{total} ({t2_pass/total*100:.1f}%)" if total else "  T2 pass: 0/0")
    if t1_pass > 0:
        print(f"  T1→T2 conversion:  {t2_pass/t1_pass*100:.1f}%")
    print(f"  Total time:        {report.get('total_elapsed_ms', 0)/1000:.1f}s")

    # ── Domain breakdown ──
    print("\n── Domain Breakdown ──")
    print(f"  {'Domain':<20} {'Total':>6} {'T2 Pass':>8} {'Rate':>6}")
    print("  " + "-" * 42)
    domains: dict[str, dict[str, int]] = {}
    for r in results:
        dom = classify_domain(r["name"])
        if dom not in domains:
            domains[dom] = {"total": 0, "t2_pass": 0}
        domains[dom]["total"] += 1
        if r.get("t2_verified"):
            domains[dom]["t2_pass"] += 1
    for dom, counts in sorted(domains.items()):
        rate = counts["t2_pass"] / counts["total"] * 100 if counts["total"] else 0
        print(f"  {dom:<20} {counts['total']:>6} {counts['t2_pass']:>8} {rate:>5.1f}%")

    # ── Error taxonomy ──
    print("\n── Error Taxonomy ──")
    error_counts: dict[str, int] = {}
    error_details: dict[str, list[str]] = {}
    for r in results:
        if r.get("t2_verified"):
            continue
        # Collect prover errors
        prover_errors = r.get("t2_errors", r.get("t1_issues", []))
        if not prover_errors:
            prover_errors = [r.get("t2_errors", "no_proof")]
        if isinstance(prover_errors, str):
            prover_errors = [prover_errors]
        for err in prover_errors:
            category = classify_error(str(err))
            error_counts[category] = error_counts.get(category, 0) + 1
            if category not in error_details:
                error_details[category] = []
            if len(error_details[category]) < 5:  # keep 5 examples per type
                error_details[category].append(str(err)[:120])

    sorted_errors = sorted(error_counts.items(), key=lambda x: -x[1])
    print(f"  {'Error Type':<25} {'Count':>6} {'% of Fails':>12}")
    print("  " + "-" * 45)
    fail_total = total - t2_pass
    for cat, count in sorted_errors:
        pct = count / fail_total * 100 if fail_total else 0
        print(f"  {cat:<25} {count:>6} {pct:>11.1f}%")
        examples = error_details.get(cat, [])
        if examples:
            for ex in examples[:2]:
                print(f"    └ {ex[:90]}")

    # ── Per-problem details ──
    print("\n── Per-Problem T2 Failures (first 10) ──")
    failed = [r for r in results if not r.get("t2_verified")]
    for r in failed[:10]:
        errors = r.get("t2_errors", r.get("t1_issues", []))
        if isinstance(errors, str):
            errors = [errors]
        if not errors:
            errors = ["(no errors)"]
        categories = ", ".join(set(classify_error(str(e)) for e in errors))
        print(f"  ❌ {r['name'][:50]:50s} [{categories}]")

    # ── Dynamic target recommendation ──
    pass_rate = t2_pass / total * 100 if total else 0

    print("\n" + "=" * 65)
    print("  Dynamic Target Recommendation (Phase 1 Roadmap)")
    print("=" * 65)

    if pass_rate < 5:
        print(f"\n  ⚠️  PASS RATE {pass_rate:.1f}% < 5% — Model too weak")
        print("  ┌─────────────────────────────────────────────────────┐")
        print("  │ This suggests qwen3-coder:30b cannot handle        │")
        print("  │ MiniF2F proof generation at the required level.    │")
        print("  │ Recommended actions:                               │")
        print("  │   a) deepseek-r1:8b  — full benchmark (1 day)      │")
        print("  │   b) qwen3.6:latest  — 36B dense (2 days)         │")
        print("  │   c) API: deepseek-v4-pro — $0.28/M tok (0.5 day) │")
        print("  └─────────────────────────────────────────────────────┘")
        print(f"  Phase C target: TBD (depends on best model)")
        print(f"  Phase C restart: YES — run model comparison first")
    elif pass_rate >= 30:
        print(f"\n  ✅ PASS RATE {pass_rate:.1f}% >= 30% — Model sufficient")
        print("  ┌─────────────────────────────────────────────────────┐")
        print("  │ qwen3-coder:30b is adequate.                       │")
        print("  │ Strategy optimization can reach 60%.               │")
        print("  │ Recommended: increase num_samples (6→12) +         │")
        print("  │   correction_rounds (2→3)                          │")
        print("  └─────────────────────────────────────────────────────┘")
        print(f"  Phase C target: 60% (realistic)")
        print(f"  Phase C restart: NO — continue strategy optimization")
    else:
        print(f"\n  ⚠️  PASS RATE {pass_rate:.1f}% — Critical range (5-30%)")
        print("  ┌─────────────────────────────────────────────────────┐")
        print("  │ Model is borderline. Hybrid approach needed:       │")
        print("  │   a) Increase num_samples (6→12), corrections (2→3)│")
        print("  │   b) Run deepseek-r1:8b in parallel for comparison │")
        print("  │   c) Target = max(current×2, optimistic upper)     │")
        print("  └─────────────────────────────────────────────────────┘")
        print(f"  Phase C target: {max(pass_rate*2, 30):.0f}% (optimistic)")
        print(f"  Phase C restart: CONDITIONAL — depends on model comp")

    # ── Action summary ──
    print("\n── Next Steps ──")
    if pass_rate < 5:
        print("  1. Run model comparison: deepseek-r1:8b, qwen3.6, API")
        print("  2. Select best → re-run Phase C")
    elif pass_rate >= 30:
        print("  1. Increase num_samples to 12, correction_rounds to 3")
        print("  2. Rerun 50-theorem benchmark with improved settings")
        print("  3. If pass_rate > 50% → target 60% is achievable")
    else:
        print("  1. Increase sampling + corrections in parallel")
        print("  2. Run deepseek-r1:8b comparison (same 50 theorems)")
        print("  3. Evaluate combined pass rate")
    print()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Analyze MiniF2F benchmark results")
    parser.add_argument("--file", "-f", type=str, default=None,
                        help="Path to benchmark result JSON")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"File not found: {path}")
            sys.exit(1)
        with open(path) as f:
            report = json.load(f)
    else:
        report = load_latest_results()
        if report is None:
            sys.exit(1)

    analyze(report)


if __name__ == "__main__":
    main()

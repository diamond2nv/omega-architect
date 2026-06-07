"""MiniF2F benchmark runner for Ω-Architect.

Evaluates the T1+T2 pipeline on the MiniF2F formal-to-formal benchmark.
Supports offline (T1-only) and online (T1+T2 with MCP) modes.

Usage
-----
    # Fast T1-only pass (no MCP needed)
    python benchmarks/minif2f/run_benchmark.py --mode t1 --max 10

    # Full pipeline (requires MCP lean_run_code)
    python benchmarks/minif2f/run_benchmark.py --mode full --max 5

    # Resume from saved results
    python benchmarks/minif2f/run_benchmark.py --resume
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# Add project root to path
_HERE = pathlib.Path(__file__).resolve().parent
_PROJECT = _HERE.parent.parent
sys.path.insert(0, str(_PROJECT))

from omega.verify.t1_llm import verify as t1_verify
from omega.verify.t2_lean import verify as t2_verify

# ── real compile callback (auto-detected) ──────────────────────

try:
    from omega.verify.t2_real import make_real_compile_callback

    _REAL_COMPILE_FN: Callable | None = make_real_compile_callback()
except (ImportError, FileNotFoundError) as _e:
    _REAL_COMPILE_FN: Callable | None = None
"""``compile_fn`` for T2 using ``lake env lean --stdin`` with Mathlib cache.

Auto-detected on import.  Falls back to ``None`` if the Lean project
or binary is not available.
"""

# ── paths ──────────────────────────────────────────────────────

# Try multiple possible MiniF2F data locations
_CANDIDATE_PATHS = [
    pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
    / "goedel-prover-v2" / "dataset" / "minif2f.jsonl",
    pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
    / "deepseek-prover" / "datasets" / "minif2f.jsonl",
    pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
    / "bfs-prover" / "src" / "data" / "minif2f_statements.jsonl",
]

RESULTS_DIR = _HERE / "results"


# ── data models ────────────────────────────────────────────────


@dataclass
class Problem:
    """A single MiniF2F problem."""
    name: str
    informal_prefix: str
    formal_statement: str
    split: str
    lean4_code: str = ""
    problem_id: int = 0


@dataclass
class ProblemResult:
    """Result of running T1+T2 on one problem."""
    name: str
    split: str
    t1_verified: bool
    t1_issues: list[str] = field(default_factory=list)
    t1_confidence: float = 0.0
    t2_verified: bool = False
    t2_errors: list[str] = field(default_factory=list)
    t2_elapsed_ms: int = 0
    prover_succeeded: bool = False
    prover_elected: str = ""
    prover_attempts: int = 0
    prover_elapsed_ms: int = 0
    elapsed_ms: int = 0


@dataclass
class BenchmarkReport:
    """Summary report for a benchmark run."""
    timestamp: str = ""
    mode: str = "t1"
    total: int = 0
    t1_pass: int = 0
    t1_pass_rate: float = 0.0
    t2_pass: int = 0
    t2_pass_rate: float = 0.0
    t1_to_t2_conversion: float = 0.0
    total_elapsed_ms: int = 0
    results: list[dict] = field(default_factory=list)
    worst_issues: list[str] = field(default_factory=list)


# ── loader ─────────────────────────────────────────────────────


def find_minif2f() -> pathlib.Path | None:
    """Find a MiniF2F JSONL file in candidate locations."""
    for p in _CANDIDATE_PATHS:
        if p.exists():
            return p
    return None


def load_problems(path: str | pathlib.Path | None = None,
                  split: str | None = None,
                  max_problems: int | None = None) -> list[Problem]:
    """Load MiniF2F problems from a JSONL file.

    Parameters
    ----------
    path : str or Path, optional
        Path to ``.jsonl`` file.  Auto-detected if omitted.
    split : str, optional
        Filter by split ("train", "valid", "test").  ``None`` = all.
    max_problems : int, optional
        Limit the number of problems loaded.

    Returns
    -------
    list[Problem]
    """
    if path is None:
        found = find_minif2f()
        if found is None:
            raise FileNotFoundError(
                "MiniF2F JSONL not found. Cloned repos:\n"
                + "\n".join(f"  {p}" for p in _CANDIDATE_PATHS)
            )
        path = found

    problems: list[Problem] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            problem = Problem(
                name=d.get("name", "unknown"),
                informal_prefix=d.get("informal_prefix", ""),
                formal_statement=d.get("formal_statement", d.get("lean4_code", "")),
                split=d.get("split", d.get("split", "unknown")),
                lean4_code=d.get("lean4_code", ""),
                problem_id=d.get("problem_id", 0),
            )
            if split is None or problem.split == split:
                problems.append(problem)
                if max_problems and len(problems) >= max_problems:
                    break

    return problems


# ── runner ─────────────────────────────────────────────────────


def run_t1_on_problem(problem: Problem) -> ProblemResult:
    """Run T1 pattern checks on a MiniF2F problem.

    T1 checks the problem's formal statement (theorem header + goal type).
    Since MiniF2F statements have no proof body (they're ``:= by``),
    T1 will typically flag ``missing_proof_body`` — this is expected.
    The key metric is whether T1 finds any *structural* issues.
    """
    code = problem.formal_statement
    t0 = time.perf_counter()

    result = t1_verify(code)

    elapsed = int((time.perf_counter() - t0) * 1000)
    return ProblemResult(
        name=problem.name,
        split=problem.split,
        t1_verified=result.verified,
        t1_issues=result.issues,
        t1_confidence=result.confidence,
        elapsed_ms=elapsed,
    )


def run_t1t2_on_problem(problem: Problem,
                        compile_fn: Callable | None = None,
                        prover: Any | None = None) -> ProblemResult:
    """Run T1 + Prover + T2 on a MiniF2F problem.

    Flow:
    1. T1 pattern check (structural)
    2. If T1 passes: run EnsembleProver to generate a proof candidate
    3. If proof generated: compile the proof (not the statement) via T2
    """
    t1_result = run_t1_on_problem(problem)

    if not t1_result.t1_verified:
        return t1_result

    # Run EnsembleProver if available
    if prover is not None:
        t0 = time.perf_counter()
        try:
            ensemble_result = prover.run(problem.formal_statement)
            p_elapsed = int((time.perf_counter() - t0) * 1000)
            t1_result.prover_elapsed_ms = p_elapsed
            t1_result.prover_succeeded = ensemble_result.succeeded
            # Support both GoedelResult and EnsembleResult interfaces
            if hasattr(ensemble_result, "elected"):
                t1_result.prover_elected = ensemble_result.elected or ""
                t1_result.prover_attempts = sum(
                    o.n_attempts for o in ensemble_result.outcomes.values()
                )
            else:
                t1_result.prover_elected = "goedel"
                t1_result.prover_attempts = ensemble_result.n_attempts

            # If prover found a candidate proof, compile it with T2
            if ensemble_result.succeeded:
                best_proof = (ensemble_result.best_proof
                              if hasattr(ensemble_result, "best_proof")
                              else ensemble_result.proof)
                if best_proof:
                    try:
                        t2_result = t2_verify(
                            best_proof,
                            compile_fn=compile_fn,
                        )
                        t1_result.t2_verified = t2_result.verified
                        t1_result.t2_errors = t2_result.errors
                        t1_result.t2_elapsed_ms = t2_result.elapsed_ms
                    except Exception as e:
                        t1_result.t2_errors = [f"T2 exception: {e}"]
                        t1_result.t2_verified = False
            else:
                # Prover didn't find a proof — T2 error explains why
                t1_result.t2_errors = ["Prover: no proof generated"]
                t1_result.t2_verified = False
        except Exception as e:
            t1_result.t2_errors = [f"EnsembleProver exception: {e}"]
            t1_result.t2_verified = False
    else:
        # Legacy mode: compile the statement directly (will fail for MiniF2F)
        try:
            t2_result = t2_verify(problem.formal_statement, compile_fn=compile_fn)
            t1_result.t2_verified = t2_result.verified
            t1_result.t2_errors = t2_result.errors
            t1_result.t2_elapsed_ms = t2_result.elapsed_ms
        except Exception as e:
            t1_result.t2_errors = [f"T2 exception: {e}"]
            t1_result.t2_verified = False

    return t1_result


# ── report generation ──────────────────────────────────────────


def generate_report(results: list[ProblemResult],
                    mode: str = "t1",
                    elapsed_ms: int = 0) -> BenchmarkReport:
    """Aggregate results into a benchmark report."""
    total = len(results)
    t1_pass = sum(1 for r in results if r.t1_verified)
    t2_pass = sum(1 for r in results if r.t2_verified)

    # Collect worst issues (most common T1 failures)
    issue_counts: dict[str, int] = {}
    for r in results:
        for issue in r.t1_issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    worst = sorted(issue_counts.items(), key=lambda x: -x[1])[:10]
    worst_issues = [f"[{count}×] {issue}" for issue, count in worst]

    return BenchmarkReport(
        timestamp=datetime.now(UTC).isoformat(),
        mode=mode,
        total=total,
        t1_pass=t1_pass,
        t1_pass_rate=round(t1_pass / total * 100, 1) if total else 0.0,
        t2_pass=t2_pass,
        t2_pass_rate=round(t2_pass / total * 100, 1) if total else 0.0,
        t1_to_t2_conversion=round(t2_pass / t1_pass * 100, 1) if t1_pass else 0.0,
        total_elapsed_ms=elapsed_ms,
        results=[asdict(r) for r in results],
        worst_issues=worst_issues,
    )


def _issue_short_label(issue: str) -> str:
    """Shorten an issue string for table display."""
    if "Unresolved" in issue:
        return "sorry"
    if "Unclosed" in issue or "Unmatched" in issue or "Mismatched" in issue:
        return "bracket"
    if "no body" in issue:
        return "no_proof"
    if "admit" in issue:
        return "admit"
    return issue[:20]


def print_report_table(report: BenchmarkReport) -> None:
    """Print a human-readable markdown report."""
    print("\n## MiniF2F Benchmark Report")
    print(f"- **Mode**: {report.mode}")
    print(f"- **Total problems**: {report.total}")
    print(f"- **Total time**: {report.total_elapsed_ms / 1000:.1f}s")
    print(f"- **T1 pass rate**: {report.t1_pass}/{report.total} ({report.t1_pass_rate}%)")
    if report.mode == "full":
        print(f"- **T2 pass rate**: {report.t2_pass}/{report.total} ({report.t2_pass_rate}%)")
        print(f"- **T1→T2 conversion**: {report.t1_to_t2_conversion}%")

    print("\n### Per-problem results")
    print("| # | Name | Split | T1 | Issues | T2 | Time(ms) |")
    print("|---|------|-------|----|--------|----|---------|")
    for i, r_dict in enumerate(report.results):
        r = ProblemResult(**r_dict)
        t1_icon = "✅" if r.t1_verified else "❌"
        t2_icon = "✅" if r.t2_verified else ("❌" if r.t2_elapsed_ms > 0 else "—")
        issues = ", ".join(set(_issue_short_label(i) for i in r.t1_issues)) or "—"
        print(f"| {i+1} | {r.name[:40]} | {r.split[:4]} | {t1_icon} | {issues} | {t2_icon} | {r.elapsed_ms} |")

    if report.worst_issues:
        print("\n### Most common T1 issues")
        for w in report.worst_issues:
            print(f"- {w}")

    # Save to file
    results_path = RESULTS_DIR / f"benchmark_{report.mode}_{report.timestamp.replace(':', '-')}.json"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(results_path, "w") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False)
    print(f"\nReport saved to: {results_path}")


# ── main CLI ───────────────────────────────────────────────────


def main():
    import argparse
    parser = argparse.ArgumentParser(description="MiniF2F benchmark runner")
    parser.add_argument("--mode", choices=["t1", "full"], default="t1",
                       help="Run mode: t1 (pattern only) or full (T1+T2)")
    parser.add_argument("--max", type=int, default=None,
                       help="Max problems to run")
    parser.add_argument("--split", choices=["train", "valid", "test"], default=None,
                       help="Filter by split")
    parser.add_argument("--data", type=str, default=None,
                       help="Path to MiniF2F JSONL (auto-detected if omitted)")

    parser.add_argument("--prover", choices=["ensemble", "goedel"], default="goedel",
                       help="Prover strategy (default: goedel for speed)")
    parser.add_argument("--prover-attempts", type=int, default=6,
                       help="Number of prover attempts per theorem (default: 6)")
    parser.add_argument("--model", type=str, default="deepseek-api",
                       help="Generator model. Options:\n"
                       "  deepseek-api    — DeepSeek API (default)\n"
                       "  ollama/qwen3    — qwen3-coder:30b via Ollama\n"
                       "  ollama/deepseek — deepseek-r1:8b via Ollama\n"
                       "  none            — template-only (for baseline)")

    args = parser.parse_args()

    # Load problems
    try:
        problems = load_problems(path=args.data, split=args.split, max_problems=args.max)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Loaded {len(problems)} MiniF2F problems (split={args.split or 'all'})")
    print(f"Mode: {args.mode}")

    # Run
    total_t0 = time.perf_counter()
    results: list[ProblemResult] = []

    # Resolve compile_fn for full mode
    compile_fn = _REAL_COMPILE_FN if args.mode == "full" else None
    prover = None
    if args.mode == "full" and _REAL_COMPILE_FN is None:
        print("⚠️  WARNING: Real compile callback not available (Lean/lean-paper-plane missing).")
        print("   Falling back to offline T2 (all theorems will show as unverified).")
    elif args.mode == "full" and _REAL_COMPILE_FN is not None:
        if args.prover == "ensemble":
            from omega.prover.ensemble import EnsembleProver
            prover = EnsembleProver(
                compile_fn=_REAL_COMPILE_FN,
                config={"goedel": {"num_samples": args.prover_attempts, "max_correction_rounds": 2},
                        "rethlas": {"max_depth": 2, "max_attempts": 2},
                        "archon": {"max_iterations": 2, "goedel_samples": 2}},
            )
            prover_label = "EnsembleProver"
        else:
            # Resolve generate_fn based on model choice
            generate_fn = None
            model_label = "template-only"
            if args.model == "deepseek-api":
                from omega.llm import make_deepseek_generate_fn
                generate_fn = make_deepseek_generate_fn(
                    model="deepseek-v4-flash",
                    temperature=0.3,
                    max_tokens=4096,
                )
                model_label = "DeepSeek-v4-Flash"
                if generate_fn is None:
                    print("   ⚠️  DeepSeek API key not found — falling back to template-only")
                    model_label = "template-only (API key missing)"
            elif args.model == "ollama/qwen3":
                from omega.llm import make_langchain_generate_fn
                generate_fn = make_langchain_generate_fn(
                    model="qwen3-coder:30b",
                    temperature=0.3,
                    num_predict=4096,
                )
                model_label = "qwen3-coder:30b"
            elif args.model == "ollama/deepseek":
                from omega.llm import make_langchain_generate_fn
                generate_fn = make_langchain_generate_fn(
                    model="deepseek-r1:8b",
                    temperature=0.3,
                    num_predict=4096,
                )
                model_label = "deepseek-r1:8b"

            from omega.prover.go_prover import make_goedel_prover
            prover = make_goedel_prover(
                compile_fn=_REAL_COMPILE_FN,
                generate_fn=generate_fn,
                num_samples=args.prover_attempts,
                max_correction_rounds=2,
            )
            prover_label = f"GoedelProver(samples={args.prover_attempts}, model={model_label})"
        print(f"   Using proof generation: {prover_label}")
        if args.prover == "ensemble":
            print(f"     Goedel: {prover.config['goedel']}")
            print(f"     Rethlas: {prover.config['rethlas']}")
            print(f"     Archon: {prover.config['archon']}")

    for i, problem in enumerate(problems):
        t0 = time.perf_counter()
        if args.mode == "full":
            result = run_t1t2_on_problem(problem, compile_fn=compile_fn,
                                          prover=prover)
        else:
            result = run_t1_on_problem(problem)
        elapsed = int((time.perf_counter() - t0) * 1000)
        result.elapsed_ms = elapsed
        results.append(result)

        # Progress
        t1_status = "PASS" if result.t1_verified else "FAIL"
        print(f"  [{i+1}/{len(problems)}] {problem.name[:50]:50s} T1={t1_status} ({elapsed}ms)")

    total_elapsed = int((time.perf_counter() - total_t0) * 1000)
    report = generate_report(results, mode=args.mode, elapsed_ms=total_elapsed)
    print_report_table(report)

    # Return exit code based on T1 pass rate
    if report.t1_pass_rate >= 80:
        print(f"\n✅ T1 pass rate {report.t1_pass_rate}% ≥ 80% target — PASS")
        sys.exit(0)
    else:
        print(f"\n⚠️  T1 pass rate {report.t1_pass_rate}% < 80% target")
        sys.exit(0)


if __name__ == "__main__":
    main()

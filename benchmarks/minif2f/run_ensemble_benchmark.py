#!/usr/bin/env python3
"""MiniF2F benchmark using Ω-Architect P0-P5 pipeline.

Uses OmegaPassKManager (P0) + Curriculum pass@k (P4) + Channels (P5)
for end-to-end proof generation + T2 compilation.

Usage:
    # Quick test: 5 theorems
    python benchmarks/minif2f/run_ensemble_benchmark.py --max 5

    # Full MiniF2F test (244 theorems, ~$0.50 API cost)
    python benchmarks/minif2f/run_ensemble_benchmark.py --full --mode curriculum

    # PutnamBench (672 theorems)
    python benchmarks/minif2f/run_ensemble_benchmark.py --putnam --max 50
"""

from __future__ import annotations

import json
import logging
import pathlib
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

_HERE = pathlib.Path(__file__).resolve().parent
_PROJECT = _HERE.parent.parent
sys.path.insert(0, str(_PROJECT))

# Centralised logging — initialise once at module import
from omega.logger import init_logging, get_benchmark_logger  # noqa: E402

_log_dir = init_logging()
logger = logging.getLogger("benchmark.ensemble")

# ── Paths ───────────────────────────────────────────────────────

RESULTS_DIR = _HERE / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

_CANDIDATE_PATHS = [
    pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
    / "goedel-prover-v2" / "dataset" / "minif2f.jsonl",
    pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
    / "deepseek-prover" / "datasets" / "minif2f.jsonl",
    pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
    / "bfs-prover" / "src" / "data" / "minif2f_statements.jsonl",
]

# PutnamBench paths
_PUTNAM_CANDIDATE_PATHS = [
    pathlib.Path.home() / "Gitlab" / "Agentic4Sci" / "AI4Math" / "formal-provers"
    / "goedel-prover-v2" / "dataset" / "putnam.jsonl",
]


# ── Data models ─────────────────────────────────────────────────


@dataclass
class Problem:
    name: str
    informal_prefix: str = ""
    formal_statement: str = ""
    split: str = "test"
    lean4_code: str = ""
    problem_id: int = 0


@dataclass
class TheoremResult:
    name: str
    difficulty_S: float = 0.0
    k_allocated: int = 0
    n_passed: int = 0
    pass_rate: float = 0.0
    best_proof: str | None = None
    backend: str = ""
    total_elapsed_s: float = 0.0
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class BenchmarkReport:
    timestamp: str = ""
    mode: str = "curriculum"
    total: int = 0
    proved: int = 0
    prove_rate: float = 0.0
    total_proofs_found: int = 0  # sum of n_passed across theorems
    total_elapsed_s: float = 0.0
    total_cost_usd: float = 0.0
    results: list[dict] = field(default_factory=list)
    avg_difficulty: float = 0.0
    avg_k: float = 0.0


# ── Loader ──────────────────────────────────────────────────────


def find_dataset(dataset: str = "minif2f") -> pathlib.Path | None:
    paths = _CANDIDATE_PATHS if dataset == "minif2f" else _PUTNAM_CANDIDATE_PATHS
    for p in paths:
        if p.exists():
            return p
    return None


def load_problems(path: str | pathlib.Path | None = None,
                  max_problems: int | None = None) -> list[Problem]:
    if path is None:
        found = find_dataset()
        if found is None:
            raise FileNotFoundError("Dataset not found")
        path = found

    problems: list[Problem] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            problems.append(Problem(
                name=d.get("name", "unknown"),
                informal_prefix=d.get("informal_prefix", ""),
                formal_statement=d.get("formal_statement", "") or d.get("lean4_code", ""),
                split=d.get("split", "test"),
                lean4_code=d.get("lean4_code", ""),
                problem_id=d.get("problem_id", 0),
            ))
    if max_problems:
        problems = problems[:max_problems]
    logger.info("Loaded %d problems from %s", len(problems), path)
    return problems


def extract_theorem_header(full_statement: str) -> str:
    """Extract just the theorem header (first line) from a Lean statement.

    MiniF2F statements look like:
    ``theorem mathd_algebra_478 (x : ℝ) : x * (-2) + 8 = x := by
       ...``
    We need just the header for the LLM.
    """
    lines = full_statement.split("\n")
    for line in lines:
        line = line.strip()
        if line.startswith("theorem ") or line.startswith("lemma "):
            # Remove trailing := if present
            if ":=" in line:
                line = line.split(":=")[0].strip() + " :="
            return line
    # Fallback: use first line
    first = lines[0].strip() if lines else ""
    if ":=" in first:
        first = first.split(":=")[0].strip() + " :="
    return first


# ── Runner ──────────────────────────────────────────────────────


def run_benchmark(
    problems: list[Problem],
    mode: str = "curriculum",
    base_budget: int = 64,
    channels: list[int] | None = None,
) -> BenchmarkReport:
    """Run ensemble benchmark on a list of problems.

    Parameters
    ----------
    problems : list[Problem]
    mode : str
        ``"curriculum"`` (P4: dynamic k based on difficulty) or
        ``"fixed"`` (fixed k for all).
    base_budget : int
        Total sample budget for curriculum mode (default 64).
    channels : list[int] or None
        Channels to use (None = all 5, or [1] for standard only).
    """
    from omega.search.passk import OmegaPassKManager
    from omega.search.curriculum import compute_difficulty_with_strategy, allocate_k
    from omega.search.channels import EnsembleChannels

    import os
    if not os.environ.get("DEEPSEEK_API_KEY"):
        logger.error("DEEPSEEK_API_KEY not set")
        return BenchmarkReport()

    # Decide which engine to use
    if channels and len(channels) < 5:
        # Use OmegaPassKManager for single-channel runs
        mgr = OmegaPassKManager(backend="deepseek")
        use_channels = False
    else:
        # Use EnsembleChannels for multi-channel
        compile_fn = None  # Will use default from OmegaPassKManager
        ens = EnsembleChannels()
        use_channels = True

    report = BenchmarkReport(
        timestamp=datetime.now(UTC).isoformat(),
        mode=mode,
        total=len(problems),
    )
    t_start = time.perf_counter()

    # Machine-readable JSONL progress log (tail -f friendly)
    blog = get_benchmark_logger(f"ensemble_{mode}")
    blog.log_progress(index=0, total=len(problems), name="START",
                      elapsed_s=0.0)

    for i, prob in enumerate(problems):
        header = extract_theorem_header(prob.formal_statement)
        logger.info("[%d/%d] %s", i + 1, len(problems), prob.name)

        t0 = time.perf_counter()
        try:
            if mode == "curriculum":
                diff = compute_difficulty_with_strategy(header)
                k = allocate_k(diff["S"], base_budget=base_budget)
            else:
                diff = {"S": 3.5}
                k = 8

            if use_channels:
                # Multi-channel
                result = ens.run(header, k=k, channels=channels)
                tres = TheoremResult(
                    name=prob.name,
                    difficulty_S=diff["S"],
                    k_allocated=k,
                    n_passed=result.n_passed,
                    pass_rate=result.pass_rate,
                    best_proof=result.best_proof[:200] if result.best_proof else None,
                    backend=f"channels-{channels or 'all'}",
                    total_elapsed_s=result.total_elapsed_s,
                    cost_usd=result.cost_usd,
                )
            else:
                # Single-channel (standard)
                _report = mgr.run(header, k=k)
                tres = TheoremResult(
                    name=prob.name,
                    difficulty_S=diff["S"],
                    k_allocated=k,
                    n_passed=_report.n_passed,
                    pass_rate=_report.pass_rate,
                    best_proof=_report.best_proof[:200] if _report.best_proof else None,
                    backend=_report.backend,
                    total_elapsed_s=_report.total_elapsed_s,
                    cost_usd=_report.cost_usd,
                )

            if tres.n_passed > 0:
                report.proved += 1

            report.total_proofs_found += tres.n_passed
            report.total_cost_usd += tres.cost_usd
            report.avg_difficulty += tres.difficulty_S
            report.avg_k += tres.k_allocated

        except Exception as e:
            logger.error("Failed on %s: %s", prob.name, e)
            tres = TheoremResult(name=prob.name, error=str(e))

        report.results.append(asdict(tres))
        logger.info("  %s | S=%.1f k=%d | %d/%d passed (%.1f%%) in %.1fs",
                    "✅" if tres.n_passed > 0 else "❌",
                    tres.difficulty_S, tres.k_allocated,
                    tres.n_passed, tres.k_allocated,
                    tres.pass_rate * 100, tres.total_elapsed_s)

        # Machine-readable progress
        blog.log_progress(
            index=i + 1, total=len(problems), name=prob.name,
            difficulty_S=tres.difficulty_S, k=tres.k_allocated,
            n_passed=tres.n_passed, elapsed_s=tres.total_elapsed_s,
            cost_usd=tres.cost_usd,
            error=tres.error,
        )

    report.total_elapsed_s = time.perf_counter() - t_start
    report.prove_rate = report.proved / max(len(problems), 1)
    report.avg_difficulty /= max(len(problems), 1)
    report.avg_k /= max(len(problems), 1)

    # Save
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    path = RESULTS_DIR / f"ensemble_benchmark_{ts}.json"
    with open(path, "w") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False)
    logger.info("Report saved to %s", path)

    return report


# ── CLI ─────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Ω-Architect Ensemble Benchmark on MiniF2F/PutnamBench"
    )
    parser.add_argument("--max", type=int, default=10,
                        help="Max problems to test (default 10)")
    parser.add_argument("--full", action="store_true",
                        help="Run full dataset (all problems)")
    parser.add_argument("--mode", choices=["curriculum", "fixed"], default="curriculum",
                        help="Pass@k allocation mode (default: curriculum)")
    parser.add_argument("--budget", type=int, default=64,
                        help="Base sample budget (default 64)")
    parser.add_argument("--channels", type=str, default=None,
                        help="Comma-separated channel numbers (e.g. '1,3,5')")
    parser.add_argument("--putnam", action="store_true",
                        help="Use PutnamBench dataset instead of MiniF2F")
    args = parser.parse_args()

    dataset = "putnam" if args.putnam else "minif2f"
    path = find_dataset(dataset)
    if not path:
        print(f"❌ {dataset} dataset not found")
        sys.exit(1)

    n = None if args.full else args.max
    problems = load_problems(path, max_problems=n)

    channels = None
    if args.channels:
        channels = [int(c.strip()) for c in args.channels.split(",")]

    report = run_benchmark(
        problems,
        mode=args.mode,
        base_budget=args.budget,
        channels=channels,
    )

    print()
    print("=" * 60)
    print(f"  {dataset.upper()} BENCHMARK REPORT")
    print("=" * 60)
    print(f"  Mode:         {report.mode}")
    print(f"  Total:        {report.total} theorems")
    print(f"  Proved:       {report.proved} ({report.prove_rate:.1%})")
    print(f"  Total proofs: {report.total_proofs_found}")
    print(f"  Avg diff:     {report.avg_difficulty:.2f}/7.0")
    print(f"  Avg k:        {report.avg_k:.0f}")
    print(f"  Time:         {report.total_elapsed_s:.1f}s")
    print(f"  Cost:         ${report.total_cost_usd:.4f}")
    print("=" * 60)

    # Check against Goedel-Architect
    target = 0.992  # Goedel-Architect MiniF2F benchmark
    if report.prove_rate >= target:
        print(f"  🏆 EXCEEDED Goedel-Architect ({target:.1%})")
    else:
        print(f"  📊 Below target {target:.1%} — need {(target - report.prove_rate)*100:.1f}% more")


if __name__ == "__main__":
    main()

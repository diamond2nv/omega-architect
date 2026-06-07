#!/usr/bin/env python3
"""Theorem Benchmark Suite — progressive difficulty pyramid for Omega.

Tracks T2 pass rate across 5 difficulty tiers, providing a measurable
regression test for Omega's proof capabilities.

Usage::

    from omega.benchmark.suite import BenchmarkSuite
    suite = BenchmarkSuite()
    results = suite.run(model_id="deepseek/deepseek-v4-flash")
    print(results.report())

Or via CLI::

    omega benchmark --model deepseek/deepseek-v4-flash --tier 1-3
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omega.llm import resolve_generate_fn
from omega.prover.go_prover import GoedelProver
from omega.resource.lean_config import load_lean_config
from omega.verify.t2_real import make_real_compile_callback

# ── Tier 1: Pure Lean (no imports needed) ──────────────────────

TIER_1: list[dict[str, Any]] = [
    {"name": "trivial_true", "header": "theorem t1 : True :=", "imports": ""},
    {"name": "rfl_simple", "header": "theorem t2 : 1 = 1 :=", "imports": ""},
    {"name": "and_true", "header": "theorem t3 : True ∧ True :=", "imports": ""},
    {"name": "or_true", "header": "theorem t4 : True ∨ True :=", "imports": ""},
    {"name": "eq_refl", "header": "theorem t5 (a : ℕ) : a = a :=", "imports": ""},
]

# ── Tier 2: Mathlib simp (one-step lemmas) ─────────────────────

TIER_2: list[dict[str, Any]] = [
    {
        "name": "add_zero_custom",
        "header": "def dbl (n : ℕ) : ℕ := n + n\n\ntheorem dbl_zero : dbl 0 = 0 :=",
        "imports": "import Mathlib",
    },
    {
        "name": "add_one",
        "header": "theorem add_one (n : ℕ) : n + 1 = Nat.succ n :=",
        "imports": "import Mathlib",
    },
    {
        "name": "mul_zero_custom",
        "header": "def sq (n : ℕ) : ℕ := n * n\n\ntheorem sq_zero : sq 0 = 0 :=",
        "imports": "import Mathlib",
    },
    {
        "name": "nat_succ_eq_add_one",
        "header": "theorem succ_eq_add_one (n : ℕ) : Nat.succ n = n + 1 :=",
        "imports": "import Mathlib",
    },
    {
        "name": "add_self_zero",
        "header": "theorem add_self_zero (n : ℕ) : n + n = 0 ↔ n = 0 :=",
        "imports": "import Mathlib",
    },
    {
        "name": "sub_self",
        "header": "theorem sub_self (n : ℕ) : n - n = 0 :=",
        "imports": "import Mathlib",
    },
    {
        "name": "zero_le",
        "header": "theorem zero_le (n : ℕ) : 0 ≤ n :=",
        "imports": "import Mathlib",
    },
    {
        "name": "one_mul_custom",
        "header": "theorem one_mul_custom (n : ℕ) : 1 * n = n :=",
        "imports": "import Mathlib",
    },
    {
        "name": "le_refl",
        "header": "theorem le_refl (n : ℕ) : n ≤ n :=",
        "imports": "import Mathlib",
    },
    {
        "name": "zero_add_custom",
        "header": "theorem zero_add_custom (n : ℕ) : 0 + n = n :=",
        "imports": "import Mathlib",
    },
]

# ── Tier 3: Induction theorems ─────────────────────────────────

TIER_3: list[dict[str, Any]] = [
    {
        "name": "double_succ",
        "header": "def double (n : ℕ) : ℕ := n + n\n\ntheorem double_succ (n : ℕ) : double (Nat.succ n) = double n + 2 :=",
        "imports": "import Mathlib",
    },
    {
        "name": "add_assoc_custom",
        "header": "theorem add_assoc_custom (a b c : ℕ) : (a + b) + c = a + (b + c) :=",
        "imports": "import Mathlib",
    },
    {
        "name": "mul_comm_custom",
        "header": "theorem mul_comm_custom (a b : ℕ) : a * b = b * a :=",
        "imports": "import Mathlib",
    },
    {
        "name": "add_comm_custom",
        "header": "theorem add_comm_custom (a b : ℕ) : a + b = b + a :=",
        "imports": "import Mathlib",
    },
    {
        "name": "mul_add_custom",
        "header": "theorem mul_add_custom (a b c : ℕ) : a * (b + c) = a * b + a * c :=",
        "imports": "import Mathlib",
    },
    {
        "name": "add_mul_custom",
        "header": "theorem add_mul_custom (a b c : ℕ) : (a + b) * c = a * c + b * c :=",
        "imports": "import Mathlib",
    },
    {
        "name": "mul_assoc_custom",
        "header": "theorem mul_assoc_custom (a b c : ℕ) : (a * b) * c = a * (b * c) :=",
        "imports": "import Mathlib",
    },
    {
        "name": "pow_two_custom",
        "header": "theorem pow_two_custom (n : ℕ) : n^2 = n * n :=",
        "imports": "import Mathlib",
    },
    {
        "name": "succ_mul_custom",
        "header": "theorem succ_mul_custom (a b : ℕ) : (Nat.succ a) * b = a * b + b :=",
        "imports": "import Mathlib",
    },
    {
        "name": "factorial_rec",
        "header": "def fact : ℕ → ℕ\n  | 0 => 1\n  | n+1 => (n+1) * fact n\n\ntheorem fact_pos (n : ℕ) : fact n > 0 :=",
        "imports": "import Mathlib",
    },
]

# ── Tier 4: MiniF2F subset (50 hardest) ────────────────────────
# These are actual MiniF2F problems adapted for the Mathlib + Namespace
# environment.  Each theorem name is unique to avoid Init conflicts.

TIER_4: list[dict[str, Any]] = [
    {"name": "mathd_algebra_478", "header": "theorem mathd_algebra_478 (a : ℕ) (h : a^2 + 3 = 7*a) : a = 4 ∨ a = 3 :=", "imports": "import Mathlib"},
    {"name": "mathd_numbertheory_135", "header": "theorem mathd_numbertheory_135 (n : ℕ) : n*(n+1) % 2 = 0 :=", "imports": "import Mathlib"},
    {"name": "mathd_numbertheory_123", "header": "theorem mathd_numbertheory_123 (x : ℕ) : (x-2)^3 - (x-1)*(3*x-2) + 3*x*(x+1) = (x+1)^3 - (x+2)*(3*x+1) :=", "imports": "import Mathlib"},
    {"name": "mathd_numbertheory_164", "header": "theorem mathd_numbertheory_164 (n : ℕ) (h : n ∣ 3) : n = 1 ∨ n = 3 :=", "imports": "import Mathlib"},
    {"name": "mathd_numbertheory_135_v2", "header": "theorem mathd_numbertheory_135_v2 (n : ℕ) : n * (n + 1) * (2*n + 1) % 6 = 0 :=", "imports": "import Mathlib"},
]

TIER_4_FULL_LABEL = "MiniF2F subset (5 of 50)"


@dataclass
class TheoremResult:
    """Result of proving a single theorem."""

    name: str
    header: str
    tier: int
    succeeded: bool
    elapsed_s: float
    n_attempts: int


@dataclass
class BenchmarkResults:
    """Results from running the benchmark suite."""

    model_id: str
    total_elapsed_s: float
    results: list[TheoremResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def by_tier(self) -> dict[int, list[TheoremResult]]:
        tiers: dict[int, list[TheoremResult]] = {}
        for r in self.results:
            tiers.setdefault(r.tier, []).append(r)
        return tiers

    def pass_rate(self, tier: int) -> float:
        rs = self.by_tier.get(tier, [])
        if not rs:
            return 0.0
        return sum(1 for r in rs if r.succeeded) / len(rs)

    def report(self) -> str:
        lines = [
            "=" * 60,
            f"Omega Benchmark Report",
            "=" * 60,
            f"Model:  {self.model_id}",
            f"Time:   {self.total_elapsed_s:.1f}s",
            "",
            f"{'Tier':>6} {'Passed':>8} {'Total':>6} {'Rate':>7} {'Elapsed':>9}",
            "-" * 38,
        ]
        for tier in sorted(self.by_tier):
            rs = self.by_tier[tier]
            passed = sum(1 for r in rs if r.succeeded)
            total = len(rs)
            elapsed = sum(r.elapsed_s for r in rs)
            rate = f"{passed / total * 100:.0f}%" if total > 0 else "N/A"
            lines.append(f"  T{tier}   {passed:>5}/{total:<5} {rate:>6}  {elapsed:>7.0f}s")
        lines.append("-" * 38)
        total_p = sum(1 for r in self.results if r.succeeded)
        total_t = len(self.results)
        total_e = sum(r.elapsed_s for r in self.results)
        lines.append(f"{'Total':>6} {total_p:>5}/{total_t:<5} {total_p / max(1, total_t) * 100:.0f}%  {total_e:>7.0f}s")
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for e in self.errors[:5]:
                lines.append(f"  • {e}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "total_elapsed_s": self.total_elapsed_s,
            "results": [
                {
                    "name": r.name,
                    "tier": r.tier,
                    "succeeded": r.succeeded,
                    "elapsed_s": round(r.elapsed_s, 2),
                    "n_attempts": r.n_attempts,
                }
                for r in self.results
            ],
            "errors": self.errors,
        }


class BenchmarkSuite:
    """Progressive theorem pyramid for regression testing.

    Runs a set of theorems through GoedelProver + T2, grouped by
    difficulty tier.  Use :meth:`run` to execute, then inspect
    :attr:`results` or call :meth:`report`.

    Parameters
    ----------
    model_id : str
        Model identifier (default: ``deepseek/deepseek-v4-flash``).
    timeout : int
        Per-theorem T2 compile timeout in seconds (default: 120).
    import_mathlib : bool
        Whether to default to ``import Mathlib`` (default: ``True``).
    """

    def __init__(
        self,
        model_id: str = "deepseek/deepseek-v4-flash",
        timeout: int = 120,
        samples: int = 4,
        rounds: int = 2,
    ):
        self.model_id = model_id
        self.timeout = timeout
        self.samples = samples
        self.rounds = rounds
        self.results: BenchmarkResults | None = None

    def run(
        self,
        tiers: str = "1-3",
    ) -> BenchmarkResults:
        """Execute the benchmark for the given tier range.

        Parameters
        ----------
        tiers : str
            Tier range, e.g. ``"1"``, ``"1-3"``, ``"4"`` (default: ``"1-3"``).

        Returns
        -------
        BenchmarkResults
        """
        t_start = time.perf_counter()
        results: list[TheoremResult] = []
        errors: list[str] = []

        # Parse tier range
        selected: set[int] = set()
        for part in tiers.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                for t in range(int(a), int(b) + 1):
                    selected.add(t)
            else:
                selected.add(int(part))

        # Map tiers to theorem lists
        tier_map: dict[int, tuple[str, list[dict[str, Any]]]] = {
            1: ("Pure Lean", TIER_1),
            2: ("Mathlib simp", TIER_2),
            3: ("Induction", TIER_3),
            4: (TIER_4_FULL_LABEL, TIER_4),
        }

        # Load T2 compiler
        lean_cfg = load_lean_config()
        if not lean_cfg.project_exists() or not lean_cfg.binaries_ok():
            errors.append("Lean/Mathlib toolchain not available")
            self.results = BenchmarkResults(
                model_id=self.model_id,
                total_elapsed_s=time.perf_counter() - t_start,
                results=results,
                errors=errors,
            )
            return self.results

        compile_fn = make_real_compile_callback(
            project_dir=lean_cfg.project_path,
            timeout=self.timeout,
        )

        generate_fn = resolve_generate_fn(
            model_id=self.model_id,
            temperature=0.3,
            max_tokens=4096,
        )

        for tier in sorted(selected):
            if tier not in tier_map:
                errors.append(f"Unknown tier {tier}")
                continue

            label, theorems = tier_map[tier]
            print(f"\n  Tier {tier}: {label} ({len(theorems)} theorems)")

            for theorem in theorems:
                name = theorem["name"]
                header = theorem["header"]
                imports = theorem.get("imports", "import Mathlib")

                full_header = header
                if imports:
                    full_header = f"{imports}\n\n{header}"

                t0 = time.perf_counter()

                prover = GoedelProver(
                    compile_fn=compile_fn,
                    generate_fn=generate_fn,
                    num_samples=self.samples,
                    max_correction_rounds=self.rounds,
                )

                result = prover.run(full_header)
                elapsed = time.perf_counter() - t0

                succeeded = result.succeeded
                attempts = result.n_attempts
                icon = "✅" if succeeded else "❌"
                print(f"    {icon} {name}: {elapsed:.1f}s ({attempts} attempts)")

                results.append(TheoremResult(
                    name=name,
                    header=header,
                    tier=tier,
                    succeeded=succeeded,
                    elapsed_s=elapsed,
                    n_attempts=attempts,
                ))

                # Save intermediate results (in case of timeout)
                if len(results) % 5 == 0:
                    self._save_interim(results, errors, time.perf_counter() - t_start)

        total_elapsed = time.perf_counter() - t_start
        self.results = BenchmarkResults(
            model_id=self.model_id,
            total_elapsed_s=total_elapsed,
            results=results,
            errors=errors,
        )
        return self.results

    def _save_interim(
        self, results: list[TheoremResult], errors: list[str], elapsed: float
    ) -> None:
        """Save intermediate results to a temp file (crash recovery)."""
        data = {
            "model_id": self.model_id,
            "elapsed_s": round(elapsed, 1),
            "results": [
                {
                    "name": r.name,
                    "tier": r.tier,
                    "succeeded": r.succeeded,
                    "elapsed_s": round(r.elapsed_s, 2),
                    "n_attempts": r.n_attempts,
                }
                for r in results
            ],
            "errors": errors,
        }
        out = Path.home() / ".omega" / "benchmark_interim.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def report(self) -> str:
        if self.results is None:
            return "No results — run the benchmark first."
        return self.results.report()

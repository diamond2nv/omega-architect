#!/usr/bin/env python3
"""LEAP Mode End-to-End Benchmark — 10 theorems, CPU-only, DeepSeek API.

Runs the Orchestrator (LEAP mode) on a 10-theorem subset of the
benchmark suite. Tracks:
  - proof success rate
  - cost (USD + RMB at ~7.3 CNY/USD)
  - time
  - DecompositionReviewer stats (reviews, accepts, rejects)

Usage:
    python3 scripts/run_leap_benchmark.py

Budget: ¥20 CNY (~$2.75 USD), 4 hours max.
"""

from __future__ import annotations

import logging
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("leap_benchmark")

# ── Budget ──────────────────────────────────────────────────────
BUDGET_USD = 2.75       # ~¥20 CNY
BUDGET_TIME_S = 14400   # 4 hours
CNY_PER_USD = 7.3

# ── 10 Test Theorems (Tier 1-3 mix) ─────────────────────────────
THEOREMS: list[dict] = [
    # Tier 1: trivial (should all pass)
    {"name": "trivial_true",    "theorem": "theorem t1 : True :=",          "tier": 1, "expected": True},
    {"name": "rfl_simple",      "theorem": "theorem t2 : 1 = 1 :=",        "tier": 1, "expected": True},
    {"name": "and_true",        "theorem": "theorem t3 : True ∧ True :=",  "tier": 1, "expected": True},

    # Tier 2: simp lemmas (should mostly pass)
    {"name": "add_one",         "theorem": "theorem add_one (n : ℕ) : n + 1 = Nat.succ n :=",  "tier": 2, "expected": True},
    {"name": "zero_add_custom", "theorem": "theorem zero_add_custom (n : ℕ) : 0 + n = n :=",   "tier": 2, "expected": True},
    {"name": "one_mul_custom",  "theorem": "theorem one_mul_custom (n : ℕ) : 1 * n = n :=",     "tier": 2, "expected": True},
    {"name": "le_refl",         "theorem": "theorem le_refl (n : ℕ) : n ≤ n :=",                 "tier": 2, "expected": True},

    # Tier 3: induction (harder, some may fail)
    {"name": "add_comm_custom",  "theorem": "theorem add_comm_custom (a b : ℕ) : a + b = b + a :=",    "tier": 3, "expected": True},
    {"name": "mul_comm_custom",  "theorem": "theorem mul_comm_custom (a b : ℕ) : a * b = b * a :=",    "tier": 3, "expected": True},
    {"name": "mul_add_custom",   "theorem": "theorem mul_add_custom (a b c : ℕ) : a * (b + c) = a * b + a * c :=", "tier": 3, "expected": True},

    # Multi-goal tests: need decomposition → exercise DecompositionReviewer
    {"name": "sum_n_induction",  "theorem": "theorem sum_n (n : ℕ) : (∑_{i=0}^{n} i) = n * (n + 1) / 2 :=",  "tier": 3, "expected": True},
    {"name": "even_square",      "theorem": "theorem even_sq (n : ℕ) (h : Even n) : Even (n^2) :=",       "tier": 3, "expected": True},
]


def main():
    from omega.engine.orchestrator import Orchestrator, OrchestratorConfig

    logger.info("=" * 60)
    logger.info("LEAP Mode End-to-End Benchmark")
    logger.info(f"Budget: ¥{BUDGET_USD * CNY_PER_USD:.0f} CNY (${BUDGET_USD:.2f})")
    logger.info(f"Max time: {BUDGET_TIME_S / 3600:.1f}h")
    logger.info(f"Theorems: {len(THEOREMS)} (Tier 1-3)")
    logger.info("=" * 60)
    logger.info("")

    config = OrchestratorConfig(
        max_refinement_rounds=2,     # Limited refinements to control cost
        lemma_timeout_s=120.0,        # 2 min per lemma
        parallel_proving=False,       # Sequential to track per-lemma cost
        max_concurrent_lemmas=1,
        enable_fallback_decompose=True,
        enable_llm_sketch=True,
        reviewer_mode="api",          # LLM-based review (semantic, not lexical)
        reviewer_required=True,        # Enforce review
        mock_compile=False,            # MUST be False: requires real Lean compiler
        disable_verifier=False,        # MUST be False: requires LLM verifier
    )
    orch = Orchestrator(config=config)

    t0 = time.time()
    total_usd = 0.0
    results = []

    for i, tdef in enumerate(THEOREMS):
        name = tdef["name"]
        theorem = tdef["theorem"]
        tier = tdef["tier"]
        expected = tdef["expected"]

        # Budget check
        elapsed = time.time() - t0
        if elapsed > BUDGET_TIME_S:
            logger.warning("Time budget exceeded after %d theorems — stopping", i)
            break
        if total_usd > BUDGET_USD:
            logger.warning("Cost budget exceeded after %d theorems — stopping", i)
            break

        logger.info(f"[{i + 1}/{len(THEOREMS)}] T{tier} {name}...")
        logger.info(f"  Theorem: {theorem[:80]}")

        try:
            t1 = time.time()
            result = orch.run(theorem)
            t2 = time.time()

            # Estimate cost from actual API token tracking
            n_subgoals = len(result.lemma_results) if result.lemma_results else 1
            total_cost = sum(
                lm.metadata.get("cost_usd", 0.0)
                for lm in (result.lemma_results or {}).values()
            )
            total_tokens = sum(
                lm.metadata.get("tokens", 0)
                for lm in (result.lemma_results or {}).values()
            )
            estimated_usd = total_cost if total_cost > 0 else n_subgoals * 0.005
            total_usd += estimated_usd

            status = "✅" if result.success else "❌"
            match = "✓" if result.success == expected else "✗"

            logger.info(f"  {status} {match} success={result.success}, "
                        f"subgoals={n_subgoals}, "
                        f"refinements={result.n_refinements}, "
                        f"time={t2 - t1:.1f}s, "
                        f"cost≈${estimated_usd:.4f}")

            results.append({
                "name": name,
                "tier": tier,
                "success": result.success,
                "expected": expected,
                "elapsed_s": t2 - t1,
                "n_subgoals": n_subgoals,
                "refinements": result.n_refinements,
                "cost_usd": estimated_usd,
                "tokens": total_tokens,
                "error": result.error,
            })

        except Exception as e:
            logger.error(f"  ❌ CRASH: {e}")
            results.append({
                "name": name, "tier": tier, "success": False,
                "expected": expected, "elapsed_s": time.time() - t1 if 't1' in dir() else 0,
                "n_subgoals": 0, "refinements": 0, "cost_usd": 0.0, "error": str(e),
            })

        logger.info("")

    # ── Report ──
    total_t = time.time() - t0
    n_pass = sum(1 for r in results if r["success"])
    n_total = len(results)
    passed_by_tier: dict[int, list[bool]] = {}
    for r in results:
        passed_by_tier.setdefault(r["tier"], []).append(r["success"])

    logger.info("=" * 60)
    logger.info("LEAP Benchmark Results")
    logger.info("=" * 60)
    logger.info(f"Passed: {n_pass}/{n_total} ({n_pass / max(n_total, 1) * 100:.0f}%)")
    logger.info(f"Time:   {total_t:.0f}s ({total_t / 60:.1f}min)")
    logger.info(f"Cost:   ${total_usd:.4f} USD (~¥{total_usd * CNY_PER_USD:.2f} CNY)")
    logger.info("")
    logger.info(f"{'Tier':>6} {'Passed':>8} {'Total':>6} {'Rate':>7}")
    logger.info("-" * 30)
    for tier in sorted(passed_by_tier):
        pb = passed_by_tier[tier]
        passed = sum(1 for p in pb if p)
        total = len(pb)
        rate = f"{passed / total * 100:.0f}%" if total else "N/A"
        logger.info(f"  T{tier}   {passed:>5}/{total:<5} {rate:>6}")
    logger.info("")
    logger.info(f"Reviewer stats: {orch._reviewer.stats}")
    logger.info("")

    # Per-theorem detail
    logger.info("Per-theorem detail:")
    for r in results:
        icon = "✅" if r["success"] == r["expected"] else "⚠️"
        logger.info(f"  {icon} {r['name']:25s} {'PASS' if r['success'] else 'FAIL':>5}  "
                    f"({r['elapsed_s']:.0f}s, ${r['cost_usd']:.4f})"
                    + (f"  {r['error'][:100]}" if r.get('error') else ""))

    logger.info("")
    logger.info("Done.")


if __name__ == "__main__":
    main()

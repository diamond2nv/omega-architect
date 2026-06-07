#!/usr/bin/env python3
"""Phase 0 Experiment: Run representative MiniF2F theorems through GoedelProver.

Collects T2 error patterns to inform the Phase 1 strategy.
Output: JSONL (one :class:`ProofRecord` + N :class:`AttemptRecord` per theorem).
"""

import sys
import time
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path.home() / "Gitlab" / "Agentic4Sci" / "omega-architect"))

from omega.data import RecordWriter, proof_records_from_goedel_result
from omega.llm import make_langchain_generate_fn
from omega.prover.go_prover import GoedelProver
from omega.verify.t2_real import make_real_compile_callback

# -- Test theorems ---------------------------------------------

# MiniF2F theorems with full Mathlib imports, covering different
# difficulty levels and domains. Using only 2 theorems for Phase 0
# to keep runtime tractable (<5 min per theorem).

TEST_THEOREMS = [
    {
        "name": "mathd_algebra_141",
        "difficulty": "easy",
        "domain": "algebra",
        "header": (
            "import Mathlib\n"
            "open Real\n\n"
            "theorem mathd_algebra_141 (a b : \u211d)"
            " (h\u2081 : a * b = 180)"
            " (h\u2082 : 2 * (a + b) = 54) : a ^ 2 + b ^ 2 = 369 :=\n"
        ),
    },
    {
        "name": "mathd_numbertheory_3",
        "difficulty": "easy",
        "domain": "number_theory",
        "header": (
            "import Mathlib\n"
            "open Real\n\n"
            "theorem mathd_numbertheory_3 (n : \u2115) :"
            " n * (n + 1) * (2 * n + 1) % 6 = 0 :=\n"
        ),
    },
]


def extract_error_pattern(errors: list[str]) -> dict:
    """Classify error messages into pattern categories."""
    patterns = {
        "syntax": 0,
        "type_mismatch": 0,
        "unknown_identifier": 0,
        "unknown_tactic": 0,
        "unsolved_goal": 0,
        "timeout": 0,
        "recursion_limit": 0,
        "other": 0,
    }
    for err in errors:
        e = err.lower()
        if "unexpected" in e or "expected" in e or "syntax" in e:
            patterns["syntax"] += 1
        elif "type mismatch" in e or ("type" in e and "mismatch" in e):
            patterns["type_mismatch"] += 1
        elif "unknown identifier" in e or "unknown constant" in e:
            patterns["unknown_identifier"] += 1
        elif "unknown tactic" in e:
            patterns["unknown_tactic"] += 1
        elif "unsolved" in e and ("goal" in e or "case" in e):
            patterns["unsolved_goal"] += 1
        elif "timeout" in e or "heartbeat" in e:
            patterns["timeout"] += 1
        elif "recursion" in e or "depth" in e:
            patterns["recursion_limit"] += 1
        else:
            patterns["other"] += 1
    return patterns


def run_experiment() -> None:
    compile_fn = make_real_compile_callback()
    if compile_fn is None:
        print("FATAL: No real compile callback available. Cannot run experiment.")
        sys.exit(1)

    generate_fn = make_langchain_generate_fn(
        model="qwen3-coder:30b",
        temperature=0.3,
        num_predict=4096,
        enable_tracing=False,
    )

    # Compact config: 2 samples, 1 correction round
    gp = GoedelProver(
        compile_fn=compile_fn,
        generate_fn=generate_fn,
        num_samples=2,
        max_correction_rounds=1,
    )

    results = []

    for t in TEST_THEOREMS:
        print(f"\n{'=' * 70}", flush=True)
        print(f"THEOREM: {t['name']} [{t['difficulty']}, {t['domain']}]", flush=True)
        print(f"{'=' * 70}", flush=True)

        t0 = time.time()
        result = gp.run(t["header"])
        elapsed = time.time() - t0

        print(f"  Elapsed: {elapsed:.1f}s", flush=True)
        print(f"  Succeeded: {result.succeeded}", flush=True)
        print(f"  Attempts: {result.n_attempts}", flush=True)

        # Print every attempt's errors in detail
        for i, att in enumerate(result.attempts):
            status = "PASS" if att["verified"] else "FAIL"
            round_idx = att["round"]
            desc = (att["description"] or "")[:60]
            print(f"  Attempt {i + 1} [round={round_idx}, {status}]: {desc}")
            for err in att["errors"][:2]:
                err_lines = err.split("\n")
                for line in err_lines[:3]:
                    print(f"    | {line}")
                if len(err_lines) > 3:
                    print(f"    | ... ({len(err_lines)} lines total)")

        # Aggregate error patterns
        all_errors = []
        for att in result.attempts:
            all_errors.extend(att["errors"])
        patterns = extract_error_pattern(all_errors)

        # Collect unique error first lines
        unique_errors = {}
        for att in result.attempts:
            for err in att["errors"]:
                first_line = err.split("\n")[0][:120]
                if first_line not in unique_errors:
                    unique_errors[first_line] = {
                        "count": 0,
                        "rounds_seen": set(),
                    }
                unique_errors[first_line]["count"] += 1
                unique_errors[first_line]["rounds_seen"].add(att["round"])

        outcome = {
            "name": t["name"],
            "succeeded": result.succeeded,
            "n_attempts": result.n_attempts,
            "elapsed_s": round(elapsed, 1),
            "corrections_used": result.corrections_used,
            "proof_preview": result.proof[:300] if result.proof else None,
            "n_total_errors": len(all_errors),
            "error_patterns": {k: v for k, v in sorted(patterns.items(), key=lambda x: -x[1])},
            "unique_errors": list(unique_errors.keys())[:8],
        }
        results.append(outcome)

        print("\n  === SUMMARY ===")
        print(f"  Succeeded:     {result.succeeded}")
        print(f"  Elapsed:       {elapsed:.1f}s")
        print(f"  Total errors:  {len(all_errors)}")
        print("  Error patterns:")
        for pat, cnt in sorted(patterns.items(), key=lambda x: -x[1]):
            bar = "█" * min(cnt * 2, 50)
            print(f"    {pat:20s}: {cnt:4d} {bar}")
        print("  Top unique errors:")
        for i, ue in enumerate(list(unique_errors.keys())[:5]):
            print(f"    [{i + 1}] {ue}")

    # Save to JSONL (one ProofRecord + N AttemptRecords per theorem)
    out_dir = Path.home() / ".omega" / "experiments"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "phase0_results.jsonl"
    config = {
        "model": "qwen3-coder:30b",
        "num_samples": 2,
        "max_correction_rounds": 1,
        "source": "phase0_experiment",
    }
    with RecordWriter(out_path) as writer:
        for t_header, result in zip(TEST_THEOREMS, results):
            proof_rec, attempt_recs = proof_records_from_goedel_result(
                theorem_id=t_header["name"],
                result=result,
                config=config,
            )
            writer.write(proof_rec)
            for ar in attempt_recs:
                writer.write(ar)

    print(
        f"\n  JSONL saved: {out_path} ({len(results)} proofs, "
        f"{sum(len(r.attempts) for r in results)} attempts)"
    )


if __name__ == "__main__":
    run_experiment()

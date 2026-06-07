"""End-to-end test for GoedelProver with real T2 compile.

Tests the complete pipeline:
  theorem_header → Proposer (template) → Lean code → T2 compile → result
"""

from __future__ import annotations

import sys
import os

# Ensure omega-core is importable
sys.path.insert(0, os.path.expanduser(
    "~/Gitlab/Agentic4Sci/omega-architect/omega-core"
))

# Load bridge module (handles hyphen in dir name)
import importlib.util

spec = importlib.util.spec_from_file_location(
    "bridge",
    os.path.expanduser("~/Gitlab/Agentic4Sci/omega-architect/omega-plugin/_bridge.py"),
)
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

from omega.prover.go_prover import GoedelProver


def test(name: str, thm: str) -> bool:
    """Run GoedelProver on a theorem and print results."""
    cf = br.make_hermes_compile_fn()
    prover = GoedelProver(compile_fn=cf, num_samples=6, max_correction_rounds=1)
    result = prover.run(thm)

    print(f"  {name}:")
    print(f"    succeeded={result.succeeded}")
    print(f"    attempts={result.n_attempts}")
    print(f"    time={result.timings.get('total_s', 0):.2f}s")
    print(f"    corrections={result.corrections_used}")

    if result.succeeded:
        proof_snippet = (result.proof or "")[:120]
        print(f"    ✅ PROOF: {proof_snippet}")
    else:
        # Collect unique error messages
        seen = set()
        for a in result.attempts:
            for e in a.get("errors") or []:
                msg = str(e)[:100]
                if msg not in seen:
                    seen.add(msg)
                    print(f"    ❌ {msg}")
    print()
    return result.succeeded


def main() -> None:
    print("=" * 60)
    print("E2E: GoedelProver (template mode — no LLM)")
    print("=" * 60)
    print()

    results = []

    for name, thm in [
        ("add_zero", "theorem add_zero (n : Nat) : n + 0 = n :="),
        ("zero_add", "theorem zero_add (n : Nat) : 0 + n = n :="),
        ("mul_comm", "theorem mul_comm (a b : Nat) : a * b = b * a :="),
    ]:
        ok = test(name, thm)
        results.append((name, ok))

    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"RESULT: {passed}/{total} theorems proved via GoedelProver")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    if passed == total:
        print()
        print("🎉 END-TO-END PIPELINE VERIFIED")
    else:
        print(f"\n❌ {total - passed} theorem(s) failed T2")


if __name__ == "__main__":
    main()

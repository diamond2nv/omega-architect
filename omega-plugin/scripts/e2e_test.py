"""End-to-end test for GoedelProver with real T2 compile.

Tests the complete pipeline:
  theorem_header → Proposer (template) → Lean code → T2 compile → result

Tests both:
- Prelude-only theorems (no Mathlib needed)
- Mathlib-reliant theorems (via lake env)
"""

from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.expanduser(
    "~/Gitlab/Agentic4Sci/omega-architect/omega-core"
))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "bridge",
    os.path.expanduser("~/Gitlab/Agentic4Sci/omega-architect/omega-plugin/_bridge.py"),
)
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

from omega.prover.go_prover import GoedelProver  # noqa: E402


def test(name: str, thm: str, needs_mathlib: bool = False) -> bool:
    """Run GoedelProver on a theorem."""
    cf = br.make_hermes_compile_fn()
    prover = GoedelProver(compile_fn=cf, num_samples=6, max_correction_rounds=1)
    t0 = time.perf_counter()
    result = prover.run(thm)
    elapsed = time.perf_counter() - t0

    tag = "📖" if needs_mathlib else "  "
    print(f"  {tag} {name}:")
    print(f"      succeeded={result.succeeded}")
    print(f"      attempts={result.n_attempts}")
    print(f"      time={elapsed:.2f}s")

    if result.succeeded:
        proof = (result.proof or "")[:120]
        print(f"      ✅ PROOF: {proof}")
    else:
        seen = set()
        for a in result.attempts:
            for e in a.get("errors") or []:
                msg = str(e)[:100]
                if msg not in seen:
                    seen.add(msg)
                    print(f"      ❌ {msg}")
    print()
    return result.succeeded


def main() -> None:
    print("=" * 60)
    print("Ω-Architect E2E Test — GoedelProver (template mode)")
    print("=" * 60)
    print()

    results = []

    for name, thm, needs_ml in [
        ("add_zero",       "theorem add_zero (n : Nat) : n + 0 = n :=", False),
        ("zero_add",       "theorem zero_add (n : Nat) : 0 + n = n :=", False),
        ("mul_comm",       "theorem mul_comm (a b : Nat) : a * b = b * a :=", False),
        ("hello_world",    "theorem hello : True := by trivial", False),
        ("ident",          "theorem id_eq (x : Nat) : x = x := by rfl", False),
        ("add_comm_simple","theorem ac (a b : Nat) : a + b = b + a := by\n  apply Nat.add_comm", True),
    ]:
        ok = test(name, thm, needs_ml)
        results.append((name, ok))

    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"RESULT: {passed}/{total} theorems proved")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    if passed == total:
        print()
        print("🎉 ALL PASSED — E2E pipeline fully verified")
    else:
        print(f"\n{total - passed} theorem(s) unresolved (expected for template-only mode)")
    print()

    # Verify Mathlib compilation path separately
    print("-" * 60)
    print("Mathlib compile path check:")
    cf = br.make_hermes_compile_fn()
    code = """import Mathlib
theorem test_mathlib : 1 + 1 = 2 := by
  native_decide
"""
    r = cf(code)
    errs = [d for d in r.get("diagnostics", []) if d.get("severity") == "error"]
    if errs:
        print(f"  ❌ Mathlib compile FAILED ({len(errs)} errors)")
        for e in errs[:3]:
            print(f"     {e.get('message','?')[:100]}")
    else:
        print("  ✅ Mathlib compile OK (lake env + native_decide)")


if __name__ == "__main__":
    main()

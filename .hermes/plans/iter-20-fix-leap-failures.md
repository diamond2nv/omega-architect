# iter-20: Fix LEAP Benchmark 3 Failures

## Goals
Fix 3/12 LEAP benchmark failures → target 12/12 (100%)

## Priority

### P0: le_refl — ModeRouter simple theorem detection
- **Root cause**: ModeRouter routes T1-T2 trivial theorems to LEAP mode when DFS would solve them in 1-2 rounds
- **Fix**: `omega/engine/router.py` — add `_score_simple()` heuristic: theorems ≤20 chars with `:=` and no `→`/`∧`/`∀` get boosted DFS score
- **Expected**: le_refl → DFS → solved in 1 round with `rfl`

### P1: mul_add_custom — lemma injection for distributivity
- **Root cause**: LEAP blueprint generation doesn't suggest `Nat.add_mul`/`Nat.mul_add`/`Nat.succ_mul` for distributivity theorems
- **Fix**: `omega/search/blueprint.py` — add distributivity lemma suggestions to `get_lemma_hints()`
- **Expected**: mul_add_custom resolved via `simp`

### P2: sum_n_induction — division handling in Nat
- **Root cause**: `(∑_{i=0}^{n} i) = n * (n + 1) / 2` uses Nat division which rounds down → needs `Nat.succ_eq_add_one` and `Nat.mul_comm` chain
- **Fix**: `omega/engine/orchestrator.py` — add `Nat.succ_eq_add_one` to default `simp` set + improve blueprint for Nat arithmetic
- **Expected**: sum_n_induction resolved via induction + `simp`

### Verify
1. Run `python3 -m pytest tests/ -q --tb=no` — 747+ pass, 0 regression
2. Re-run `python3 scripts/run_leap_benchmark.py` on new budget
3. Push to NAS → git tag iter-20

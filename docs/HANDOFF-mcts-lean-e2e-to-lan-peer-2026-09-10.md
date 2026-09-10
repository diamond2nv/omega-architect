# HANDOFF → LAN peer with Lean: MCTS × Lean end-to-end validation (2026-09-10)

> **Audience**: the operator of the machine that has a Lean toolchain installed
> (this repo's own checkout on that machine).
> **Status of the work**: Step 1 + Step 2 are implemented and tested here with
> *injected* collaborators; the real-Lean end-to-end run is the remaining
> unverified link, because the machine that produced these commits has no Lean
> (`lean` / `lake` / `elan` all absent).
> **Nothing in this document is machine-specific** - substitute your own paths.

## 1. What is already done (and verified)

| Step | Deliverable | Verification |
|:--|:--|:--|
| 1 | `omega/engine/strategy_mcts.py` (MCTS core, injectable trio, standard UCB1, anytime) | 13 tests (`tests/test_mcts_strategy.py`) |
| 1 | `omega/engine/mcts_diagnosis.py` (stuck nodes / blind spots / error heat / visit entropy / failure chain) | same |
| 2 | `omega/engine/lean_adapters.py` (LLM candidates, CompileGate transition, compile-distance value, `build_lean_mcts()`) | 14 tests (`tests/test_lean_adapters.py`), all with a **faked** compiler |
| 2 | `scripts/mcts_lean_smoke.py` (end-to-end smoke) | exits 2 with a clear message when no Lean is present - **never yet run against real Lean** |

Full suite at the time of writing: **759 passed / 0 failed / 53 skipped**.

## 2. What you need to do

Run the real end-to-end path and report back.

### 2.1 Prepare

```bash
cd <your omega-architect checkout>
git remote -v                        # ⚠️ remote NAMES differ per machine:
                                     #   the NAS Forgejo remote may be `origin` or `local`,
                                     #   and `origin` may point at the public mirror instead!
git pull <nas-remote> main           # pull from whichever remote points at NAS
grep '^version' pyproject.toml | head -1

# Lean toolchain sanity
which lean lake elan || echo "NO LEAN"
echo "LEAN_LSP_MCP=${LEAN_LSP_MCP:-<unset>}"
# Mathlib cache (first compile is otherwise very slow):
#   lake exe cache get
```

### 2.2 Confirm the injected-collaborator tests still pass on your machine

```bash
python3 -m pytest tests/test_lean_adapters.py tests/test_mcts_strategy.py -q
# expect: >= 27 passed (the count grows as review regressions land; 37 as of 9103b71)
```

### 2.3 Run the smoke test against real Lean

```bash
python3 scripts/mcts_lean_smoke.py --help

# (a) no LLM, no API key - isolates Lean + MCTS + diagnosis:
python3 scripts/mcts_lean_smoke.py \
    --theorem "theorem smoke_add : 1 + 1 = 2 := by" \
    --iterations 6

# (b) with the repository LLM helper generating tactic candidates:
python3 scripts/mcts_lean_smoke.py --generator llm --iterations 12
```

Exit codes: `0` solved · `1` not solved (diagnosis is still the deliverable) ·
`2` environment problem (no Lean).

## 3. Acceptance criteria (please report each explicitly)

- [ ] `tests/test_lean_adapters.py` + `tests/test_mcts_strategy.py` → 27 passed
- [ ] smoke (a) on `1 + 1 = 2` → **SOLVED** (exit 0)
- [ ] the diagnosis block prints a non-empty `summary()` and at least one of
      `stuck` / `blind` / `heat` (proves the diagnosis link is live, not just
      the search)
- [ ] smoke (b) runs end-to-end (record whether the LLM generator produced
      usable candidates; empty output → blind spots is a valid, reportable result)
- [ ] one *harder* theorem (e.g. `theorem t (n : ℕ) : n + 0 = n := by`) run to
      completion **even if unsolved** - attach the diagnosis output
- [ ] record `compile_elapsed_ms` (visible on nodes via the metadata) vs the
      iteration budget: how many MCTS iterations are affordable per minute?

## 4. Traps (learned the hard way)

1. ⚠️ **`ruff check` has side effects in this repo** - `[tool.ruff] fix = true`,
   so a bare run **silently rewrites files** (it once reverted a fresh fix and
   touched 24 unrelated files). Always use **`ruff check --no-fix`**, and
   re-check `git status` afterwards.
2. ⚠️ Remote names are machine-specific (verified 2026-09-10: on one peer the NAS
   remote is `local` and `origin` points at the public mirror - the reverse of
   the other). Always `git remote -v` first; never push to the public remote
   without an explicit release decision (see `AGENTS.md`).
3. `scripts/*.py` is exempted from the `T20` (print) lint rule in
   `pyproject.toml`; `tests/*.py` likewise.
4. If the first compile takes minutes, it is Mathlib warming up - `lake exe
   cache get` first.

## 5. Reporting back

Write results to `docs/experiments/` (this repo) and/or reply to the handoff
e-mail with:

* the exact command(s) run and their exit codes,
* the printed diagnosis block (stuck / blind / heat / failure chain),
* `compile_elapsed_ms` observations and how many iterations were affordable,
* any adapter that needed adjusting for the real backend (especially the code
  assembly in `CompileGateTransition.append_tactic`, which currently appends a
  tactic to the end of the proof body and is the most likely thing to need
  refinement against real Lean syntax).

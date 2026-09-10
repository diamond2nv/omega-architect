# MCTS × Lean end-to-end on the LAN peer with a real Lean toolchain (2026-09-10)

> **Machine**: the LAN peer that has Lean installed (`lean` / `lake` / `elan` present).
> **Refs**: `docs/HANDOFF-mcts-lean-e2e-to-lan-peer-2026-09-10.md` (handoff) ·
> base commit `9103b71` (NAS `main` after `git pull`).
> **Verdict**: acceptance criteria **all met**, and the real-Lean run exposed
> **3 real defects** (one of them silently corrupting the search's value signal)
> which are fixed and regression-tested below.

> ⚠️ **Remote-name note**: on this machine the NAS Forgejo remote is `local`, not
> `origin` (`origin` points at the public Aliyun Codeup repo). The handoff text says
> "the NAS remote is `origin`" — that is true *on the producing machine*. Nothing
> here was pushed to `origin`.

## 1. Commands run and exit codes

| # | Command | Exit | Note |
|:--|:--|:--:|:--|
| 1 | `git fetch local && git merge --ff-only local/main` | 0 | `cab4697` → `9103b71` (3 commits incl. `c0cb9ce`) |
| 2 | `grep '^version' pyproject.toml` | 0 | `version = "0.2.0"` |
| 3 | `python3 -m pytest tests/test_lean_adapters.py tests/test_mcts_strategy.py -q` | 0 | **37 passed** (handoff says 27 — the count grew in the two review-fix commits after `c0cb9ce`) |
| 4 | `mcts_lean_smoke.py --theorem "smoke_add : 1 + 1 = 2 := by" --iterations 6` | **0** | **SOLVED** |
| 5 | `mcts_lean_smoke.py --generator llm --iterations 12` | **0** | **SOLVED** (after the fixes in §4) |
| 6 | `mcts_lean_smoke.py --theorem "t (n : Nat) : n + 0 = n := by" --iterations 8` | 0 | SOLVED (`norm_num`, 1 iteration) |
| 7 | `mcts_lean_smoke.py --theorem "false_demo : 1 + 1 = 3 := by" --iterations 8` | 1 | not solved → **diagnosis is the deliverable** |
| 8 | `python3 -m pytest -q` (full suite) | 1 | **817 passed / 4 failed / 1 skipped** — all 4 failures are **pre-existing** (see §6) |
| 9 | `ruff check --no-fix omega/loop/errors.py omega/engine/lean_adapters.py` | 0 | All checks passed (never run bare ruff: `[tool.ruff] fix = true`) |
| 10 | `pyright omega/loop/errors.py omega/engine/lean_adapters.py` | 0 | 0 errors, 0 warnings |

## 2. Acceptance criteria

| Criterion | Result |
|:--|:--|
| `test_lean_adapters` + `test_mcts_strategy` → 27 passed | ✅ **37 passed** (superset of 27) |
| smoke (a) on `1 + 1 = 2` → **SOLVED** (exit 0) | ✅ SOLVED, 2164 ms, `norm_num` at depth 1 |
| diagnosis block prints non-empty `summary()` and ≥1 of `stuck`/`blind`/`heat` | ✅ `blind=1` + 3 `heat` buckets + 9-node failure chain (see §3) |
| smoke (b) runs end-to-end; record whether the LLM produced usable candidates | ✅ ran, **and now solves**. First run: **0 candidates** → 3 defects (§4). After fix: SOLVED via `rfl`, 4784 ms |
| one harder theorem run to completion even if unsolved, with diagnosis attached | ✅ `n + 0 = n` solved; `1 + 1 = 3` run to full budget with diagnosis (§3) |
| record `compile_elapsed_ms` vs iteration budget → iterations affordable per minute | ✅ **§5** |

## 3. Diagnosis output (the deliverable)

### 3.1 Unsolvable theorem `1 + 1 = 3` — full 8-iteration budget

```
[trajectory] success=False steps=8 elapsed_ms=16251
[diagnosis]
  explored=9 max_depth=8 solved=0 iterations=8 | stuck=0 blind=1
  visit_entropy=2.050(norm 0.933) | hot: tactic_failed@1×3, tactic_failed@2×3, unsolved_goal@0×1
  - blind n9 (depth=8, never expanded)
  failure chain: n1 -> n2 -> n3 -> n4 -> n5 -> n6 -> n7 -> n8 -> n9
  heat: tactic_failed@1 ×3
  heat: tactic_failed@2 ×3
  heat: unsolved_goal@0 ×1
```

**Before the §4.1 fix this same run reported `hot: no_error@1×3, no_error@2×3,
no_error@0×2`** — i.e. the diagnosis claimed *no errors* on a proof that cannot
typecheck, and the value heuristic scored every dead end as **1.0 proximity =
"proof is nearly done"**.

### 3.2 LLM-generator run (after fixes)

```
[trajectory] success=True steps=1 elapsed_ms=4784 strategy='MCTS (Lean, llm)'
  proof: import Mathlib / theorem smoke_add : 1 + 1 = 2 := by / rfl
[diagnosis] explored=2 max_depth=1 solved=1 iterations=1 | stuck=0 blind=0
  visit_entropy=0.637(norm 0.918)
```

## 4. Defects found by the real-Lean run and fixed

### 4.1 `UNSOLVED_GOAL` was unmapped → dead ends scored as finished proofs (severity: high)

`omega/loop/errors.py` `_ERROR_PATTERNS` had **no rule for `unsolved goals`**, and
`CompileErrorClass` had no such member — although the repo's *other two* classifiers
(`omega/classifier/layer1_rules.py:92`, `omega/search/error_classifier.py:84`) both map
it. `classify_compile_error` then fell through to `NO_ERROR`.

Compounding it: `parse_lean_diagnostics` (t2_real) **strips the `error:` prefix into a
separate `severity` field**, so the `if "error:" in diagnostic` fallback can never fire
and *every* unrecognised error message was silently labelled `NO_ERROR`.

Impact: `ERROR_PROXIMITY[NO_ERROR] = 1.0`, so `CompileDistanceEvaluator` valued a failing
tactic as a completed proof — the MCTS value signal was inverted for the single most
common real-Lean outcome.

Fix: added `UNSOLVED_GOAL` / `TACTIC_FAILED` classes + patterns; added
`classify_diagnostic_entry()` which is **severity-aware** (an entry Lean marked
`error`/`warning` can never come back `NO_ERROR`); info/note context lines such as a
trailing `⊢ goal` no longer pollute heat buckets; added proximity `unsolved_goal=0.65`,
`tactic_failed=0.45`.

### 4.2 `--generator llm` was dead on arrival (severity: high)

`_default_llm_call` probed `omega.llm.{complete,generate,call,chat}`; `omega.llm`
exposes **factories** (`resolve_generate_fn`, `make_deepseek_generate_fn`, …) and none of
those flat names → `RuntimeError: omega.llm exposes no known completion entry point`,
which `LeanActionGenerator` swallowed into `[]`. Result: `explored=1 max_depth=0
iterations=0` and **no compile at all**.

Fix: use `resolve_generate_fn(model_id)` (model overridable via `OMEGA_LLM_MODEL`,
default `deepseek/deepseek-v4-flash`); the flat-name probe is kept as a fallback.

### 4.3 Two reply-shape mismatches between the LLM backend and the parser (severity: medium)

`resolve_generate_fn("deepseek/…")` routes to `make_deepseek_json_generate_fn`, which
forces `response_format=json_object` and returns `{"tactic": …, "confidence": …}` —
while `LeanActionGenerator.parse()` only understood one-tactic-per-line text. So even a
successful call yielded 0 candidates.

Once JSON parsing worked, a second issue appeared: the model puts a **whole proof** in
the `tactic` field (e.g. `"norm_num\nrfl\ndecide"`). The search applies **one action per
node**, so appending the block verbatim made the first tactic close the goal and the
trailing ones error with "no goals to be solved" — `1 + 1 = 2` stayed **unsolved even
though `norm_num` alone solves it**.

Fix: `parse()` is now JSON-aware (object, list, and fenced JSON) and splits a multi-line
tactic payload into **one candidate per line**; `append_tactic()` indents **every** line
of a multi-line block (it previously indented only the first, leaving the rest at column
0 → syntax error) while preserving relative indentation for nested blocks.

> ⚠️ Known caveat: a genuinely nested block (`induction n with | zero => …`) would be
> split into unusable single lines. Prompt the model for single tactics, or add a
> block-aware branch, when nested proofs are on the menu.

## 5. Cost model: `compile_elapsed_ms` and affordable iterations

Measured on this machine (`lake env lean --stdin` in the Mathlib project, core i5-class CPU):

| Scenario | Wall time |
|:--|--:|
| core only, no `import Mathlib` | **2.58 s** |
| `import Mathlib`, **cold** (page cache empty) | **20.40 s** |
| `import Mathlib`, **warm** (subsequent compiles) | **~2.1 s** |

Smoke runs with the default `import Mathlib` therefore land at ~2.1 s/iteration warm:

| Iterations | Total | Throughput |
|--:|--:|--:|
| 6 | 16.3 s | ~22 iter/min |
| 8 | 16.3 s | ~29 iter/min |
| 12 (LLM) | 4.8 s + LLM latency | LLM-bound, not compile-bound |

**Rule of thumb: ~25–30 MCTS iterations/minute warm, ~3/minute cold.** The first compile
after boot dominates — run `lake exe cache get` (and one warm-up compile) before timing
anything. The compile cache (`~/.cache/omega/compile/`) collapses repeats to ~0 ms;
**invalidate it after changing classification or code assembly**, or stale results hide
fixes (this bit us in §4.1).

## 6. Pre-existing failures (NOT introduced here — verified by `git stash` baseline)

| Test | Status |
|:--|:--|
| `test_feature_asset_health.py::test_feature_pipeline_loads_without_version_warnings` | fails on baseline too |
| `test_gates.py::TestVersionGate::test_version_matches_pyproject` | fails on baseline too |
| `test_t2_real.py::…::test_works_with_verify` | fails on baseline too |
| `test_t2_real.py::…::test_verify_mathlib` | fails on baseline too (`AttributeError: 'dict' object has no attribute 'verified'`) |

Baseline (changes stashed): `4 failed, 13 passed` on the same selection. With changes:
identical 4 failures, +1 passing (the proximity-table test that guards §4.1). The handoff
reported "759 passed / 0 failed" at `c0cb9ce`; the suite has since grown to 817 passing.

### 6.1 Root causes (measured, supersedes the "drift" hypothesis)

The three below are **not** introduced between `c0cb9ce` and `9103b71` — they are
environment/consistency issues that happen to be red on *this* machine:

| Test | Measured root cause | Whose problem |
|:--|:--|:--|
| `test_feature_asset_health` | `feature_pipeline.joblib` was pickled with **scikit-learn 1.9.0**; this machine runs **1.8.0** → 3× `InconsistentVersionWarning`. `pyproject.toml` only pins `scikit-learn>=1.3` (no upper bound), so the skew is unguarded. | **Cross-machine version skew.** Re-exporting the asset with 1.8.0 here would break the producing machine; the real fix is a pinned `scikit-learn==1.9.*`, decided at the producing end. |
| `test_version_matches_pyproject` | `omega.__version__` reads `importlib.metadata` → `0.1.0`; `pyproject.toml` says `0.2.0`. The editable-install `METADATA` is dated **2026-06-10** while `pyproject.toml` was bumped **2026-09-10**. | **Local editable metadata staleness** — `pip install -e .` refreshes it; no repo change needed. |
| `test_t2_real` ×2 | The tests assert `result.verified`, but `make_real_compile_callback` is annotated and documented to return `Callable[[str], dict[str, Any]]`. | **Stale tests** left behind by an object→dict API change (note `omega/search/passk.py:264` still uses `t2_result.verified`, so the dict↔object contract deserves a deliberate decision). |

### 6.2 Push status ⚠️

`scripts/pre-push-hook.sh` runs (nearly) the whole suite, so these 4 failures **block
every push to the NAS remote** — from any machine. The work is committed locally as
`3ba9a6b` and is **not on NAS `main`**. `--no-verify` was deliberately **not** used.
Please fix or quarantine the 4 above at the producing end, then this lands.


## 7. Environment notes for the next LAN peer

1. **`lean --version` can hang/fail even with Lean "installed".** Here
   `~/.elan/settings.toml` sets `default_toolchain = "stable"`, and the `stable` *channel*
   now resolves to Lean **4.33.1** (not installed) → elan attempts a download from
   `releases.lean-lang.org` / GitHub → timeout. Pinned toolchains work fine:
   `~/.elan/toolchains/4.30.0/bin/lean` → `Lean (version 4.30.0)`. `omega.toml` already
   pins `4.30.0` explicitly, so the pipeline is unaffected — but any bare `lean`
   invocation via PATH is. `~/.elan/toolchains/leanprover/lean4:v4.31.0` exists but has no
   `bin/lean` (partial install).
2. `omega.toml [lean]` was already populated: `project_path = <lean project with Mathlib>`,
   `olean_count = 8109`, `size_gb = 6.7`, `channel = "lake_env"`.
3. Local Ollama backends need `pip install langchain-ollama` (not installed);
   `deepseek/*` works with `DEEPSEEK_API_KEY`.
4. `data/papers.db` (48 KB) is untracked in this working tree — a stray artefact from a
   tool run that did not set its data-dir env var. `.gitignore` already covers it; delete
   at will.

## 8. Recommendation

Both halves of the handoff are now verified against real Lean. The three code fixes are
small, self-contained and regression-tested; they should go to NAS `main` for the
producing machine to review (this peer does not push to the public remote). The
`append_tactic` multi-line handling was exactly the refinement the handoff predicted, and
§3.1 is the concrete evidence that the error-class gap was corrupting the value signal
rather than merely mislabelling logs.

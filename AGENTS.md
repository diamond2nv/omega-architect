# Ω-Architect AGENTS.md — Deterministic State Machine

## States

Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.

```
[ENTRY] → analyze_query
               │
               ▼
         select_skill ──────────────────────────┐
               │                                │
               ▼                                │
         decompose_task                         │
               │                                │
          ┌────┴────┐                           │
          ▼         ▼                           │
     skill_exec  [no exec needed] ──────────────┼── T1 fast fail?
          │         │                           │
          └────┬────┘                           │
               ▼                                │
         t1_verify (LLM, ~5s)                   │
          OK/FAIL                               │
               │                                │
          ┌────┴────┐                           │
          ▼         ▼                           │
     t2_verify  retry / fallback ───────────────┘
     (Lean, ~30s)
          │
          ▼
       [EXIT] → synthesize_result
```

## State Definitions

### analyze_query
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Parse user query into formal target + expected type signature
- **output**: `{"formal_target": "theorem statement in Lean", "target_type": "signature", "difficulty": "easy|medium|hard", "domain": "physics|combinatorics|..."

### select_skill
- **role**: leaf
- **toolsets**: ["terminal", "file", "web"]
- **goal**: Choose from 8 primitives (see below). If none match, fallback to decompose.
- **output**: `{"primitive": "name", "reason": "..."}`

### decompose_task
- **role**: orchestrator
- **toolsets**: ["terminal", "file"]
- **goal**: Split into sub-goals. Each sub-goal = a single `delegate_task` call.
- **constraints**: max_iterations=5, each sub-goal ≤50 lines Lean
- **output**: `[{"goal": "...", "expected_type": "..."}, ...]`

### skill_exec
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Execute the selected primitive. Returns proof attempt or failure state.
- **output**: `{"proof_attempt": "...", "success": true|false, "error": "..."}`

### t1_verify
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Fast LLM-based verification of the proof attempt. Check: type consistency, variable usage, missing imports, structural completeness.
- **constraints**: target ~5s per check
- **output**: `{"verified": true|false, "issues": ["..."], "confidence": 0.0-1.0}`

### t2_verify
- **role**: leaf
- **toolsets**: ["terminal"]
- **goal**: Full Lean compiler verification via `lake build` or `lean` CLI.
- **constraints**: target ~30s per check, timeout 60s
- **output**: `{"verified": true|false, "errors": ["..."], "elapsed_ms": 123}`

### synthesize_result
- **role**: leaf
- **toolsets**: ["terminal", "file"]
- **goal**: Combine all sub-goal results into final theorem statement + proof.
- **output**: `{"theorem": "...", "proof": "...", "verified_by": "t2", "elapsed_ms": 123}`

## 8 Skill Primitives

| # | Primitive | Trigger | Description |
|---|-----------|---------|-------------|
| 1 | `apply_lemma` | `apply` keywords | Apply existing lemma from Mathlib |
| 2 | `rewrite_goal` | `rw` / `simp` | Rewrite target using known identities |
| 3 | `induction` | `∀ n:ℕ` or recursive structure | Structural induction |
| 4 | `case_split` | `h : A ∨ B` or `if ...` | Case analysis |
| 5 | `calc_chain` | equality chain | `calc a = b := ...` |
| 6 | `search_lemma` | unknown identity | leansearch / loogle query |
| 7 | `extract_proof` | previous similar problem | Adapt known proof structure |
| 8 | `fallback_decompose` | complex goal | Delegate to decompose_task |

## Retry Policy

- T1 failure (confidence < 0.5): retry up to 2x with different decomposition
- T2 failure (Lean compile error): log error, retry once with error-aware rewrite
- Total max_iterations: 5
- After 5 failures: return best attempt + error diagnosis

## Context Limits

- MessageLog: last 50 messages per sub-goal
- Proof file: max 200 lines per sub-goal
- T2 timeout: 60s (hard fail)

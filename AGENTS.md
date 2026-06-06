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

## T2 Real Compile

新模块 `omega/verify/t2_real.py` 提供真实 Lean 编译器后端：

- **调用**：`lake env lean --stdin`（使用 `lean-paper-plane` 项目，含 Mathlib 7.1GB 缓存）
- **集成**：`from omega.verify.t2_real import make_real_compile_callback`
- **返回**：MCP 兼容格式 `{"diagnostics": [...], "exit_code": N}`
- **耗时**：~2.5s/定理（含 Mathlib）
- **测试**：17 tests，含真实编译测试（需 Mathlib 项目存在）
- **已知问题**：纯 Lean 定理 Init 预声明显冲突，建议始终加 `import Mathlib`

### MiniF2F Benchmark 状态

```
T1 pass: 241/244 (98.8%)
T2 pass: 0/244 (0.0%) — 预期，MiniF2F 为 statement-only
含 Mathlib 编译时间: ~2.5s/定理 × 244 = ~10min
```

运行: `python benchmarks/minif2f/run_benchmark.py --mode full --max 50`

## 证明生成系统（新）

三路证明生成器 + 联合集成，全部原生实现（零第三方外部调用）。

### omega/search/ — 证明搜索核心

```
omega/search/
├── __init__.py        # 统一导出
├── tree.py            # ProofTree, SearchNode, GoalState, NodeStatus
└── proposer.py        # Proposer, TacticSuggestion, LLM/template 生成
```

### omega/prover/ — 三路证明生成

| 模块 | 策略 | 来源灵感 | 行数 |
|------|------|---------|------|
| `go_prover.py` | 并行抽样 + 自修正 (2 rounds) | Goedel-Prover-V2 | 435 |
| `re_prover.py` | 蓝图分解 + 递归子目标 + LemmaCache | Rethlas | 647 |
| `ar_prover.py` | 多策略集成 + ProgressCritic (CONVERGING/CHURNING/STUCK) | Archon | 680 |
| `ensemble.py` | 联合运行三个 → 对比选举最优 | 自研 | 312 |

### 核心 API

```python
from omega.prover import EnsembleProver

# 需要 T2 编译回调
compile_fn = make_real_compile_callback()
prover = EnsembleProver(compile_fn=compile_fn)
result = prover.run(theorem_header)
print(result.summary())
print(result.comparison_table)
```

### 选举策略

优先级: T2 验证通过 → 证明最短 → 耗时最少

### 测试

```
tests/test_prover.py: 29 tests (含 2 个真实编译集成测试)
pytest 150/150 passed
```

### 许可证合规

`omega/license/README.md` 记录所有三方来源许可证:
- Goedel-Prover-V2 (Apache 2.0) — 并行抽样算法
- Rethlas (Apache 2.0) — 蓝图分解模式
- Archon (Apache 2.0) — 进度评判机制
- Mathlib (Apache 2.0) — T2 编译环境
- aesop (MIT) — 依赖项

所有代码自研重写，零直接复制。

## Context Limits

- MessageLog: last 50 messages per sub-goal
- Proof file: max 200 lines per sub-goal
- T2 timeout: 60s (hard fail)

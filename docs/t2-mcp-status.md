# T2 端到端编译管线 — 真实 Lean 编译器集成

## 架构

```
MiniF2F JSONL (244 题)
       │
       ▼
run_benchmark.py --mode full
       │
       ├─ T1: pattern check (5 rules + LLM, ~5s/244)
       │     98.8% pass rate
       │
       └─ T2: real compile (via t2_real.py)
              │
              ▼
        lake env lean --stdin
              │
              ├─ LEAN_PATH = lean-paper-plane/.lake/...
              ├─ Mathlib 8108 oleans (7.1 GB cached)
              └─ result: diagnostics + exit code
```

## T2 编译后端

新模块 `omega/verify/t2_real.py`：

- **调用**：`lake env lean --stdin`（项目内 `lean-paper-plane`，含完整 Mathlib 缓存）
- **返回**：MCP 兼容的 `{"diagnostics": [...], "exit_code": N}` 格式
- **连接**：`make_real_compile_callback()` → 可传入 `t2_lean.verify(code, compile_fn)`
- **自动检测**：`run_benchmark.py` 在 `--mode full` 时自动加载

## 验证结果

### ✅ Mathlib 定理编译通过

| 定理 | 依赖 | 耗时 | 结果 |
|------|------|------|------|
| `sin_sq_add_cos_sq` | ℝ, Real | ~2.5s | ✅ |
| `sqrt_sq_eq_abs` | ℝ, Real | ~2.5s | ✅ |
| `add_one_greater` | ℝ, nlinarith | ~2.5s | ✅ |
| `square_sum` | ℝ, ring | ~2.5s | ✅ |

### ✅ 纯 Lean 定理（通过 lake env 编译）

| 定理 | 之前 (lean_run_code) | 现在 (lake env) | 说明 |
|------|---------------------|-----------------|------|
| `true_trivial` | ✅ | ✅ | 一致 |
| `simple_identity` | ❌ (simp_attr 缺失) | ✅ | Lake 环境提供完整 Init |
| `add_zero/zero_add` | ✅ | ❌ | 已在内核中声明 → 绕过后被 lake Init 加载 |
| `and_comm/or_comm` | ✅ | ❌ | 同上，Init 已有声明 |

> 纯 Lean 定理的 `already declared` 问题是因为 `lake env` 加载的 Init 库比 `lean --stdin` 裸调用更完整。这不是缺陷——含 Mathlib 的定理才是真正的目标。

### ❌ MiniF2F 理论上：T2 pass = 0%（预期行为）

MiniF2F 题目是**定理声明后无证明体**（statement-only）。T2 编译失败原因：
- ~90%: `unexpected end of input; expected '{'`（无证明体）
- ~5%: `unexpected token 'in'; expected ','`（语法细微差异）
- ~5%: `unsolved goals`

⚠️ **无基础设施错误**：所有 `Mathlib` import、`set_option maxHeartbeats 0` 等均可正确通过，证明 Mathlib 缓存和编译环境完全就绪。

### 📊 Benchmark 数据（50 题采样）

```
T1 pass: 50/50 (100.0%)
T2 pass: 0/50 (0.0%)     ← 预期，无证明体
T1→T2 conversion: 0.0%
Total time: 128.8s (~2.5s/定理)
```

含 Mathlib 编译时间 ~2.5s/定理 × 244 = **~10.2 min 全量**。

## 下一步

1. **加入证明生成**：用 Goedel-Prover-V2 或 Rethlas/Archon 为 MiniF2F 生成证明 → T2 编译验证
2. **T2 目标**：生成证明的 T2 pass rate ≥80%
3. **并行化**：244 题按 `test`/`valid` 分片，多进程编译可压缩到 2-3 min

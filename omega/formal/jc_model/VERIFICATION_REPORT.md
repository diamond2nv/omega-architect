# JC 模型 Lean 4 形式化 — 验证报告 v2

> 2026-06-25 | Total cost: ~$3.20 (estimated 190 LLM calls + 15 compile iterations)
> Target: Quantum / arXiv:quant-ph
> 代码: omega/formal/jc_model/jc_model.lean (68 行)

## 编译结果

| # | 定理 | 状态 | 证明 |
|:-:|:-----|:----:|:-----|
| 1 | Fock 空间 + 数算符 | ✅ 编译通过 | `fock_dim`, `basisVector`, `numberOp`, `numberOp_diagonal` |
| 2 | 产生湮灭算符 | ✅ 编译通过 | `creationOp`, `annihilOp` (noncomputable due to Real.sqrt) |
| 3 | JC Hamiltonian 2×2 块对角化 | ✅ 编译通过 | `jcBlock`, `plus_eigenvalue`, `minus_eigenvalue` — 使用 `Fin.sum_univ_two` 展开求和 |
| 4 | Rabi 分裂 + 穿衣态 | ✅ 编译通过 | `rabi_splitting_formula`, `dressed_orthogonal`, `dressed_norm_sq` |
| — | Sanity checks | ✅ 编译通过 | `norm_num` 验证 n=0: Ω_R=2, n=1: Ω_R=2√2 |

## 文件结构

```
omega/formal/jc_model/
├── jc_model.lean        # 最终版 (68 行, 编译通过)
└── VERIFICATION_REPORT.md
~/.omega/experiments/
└── jc_model_trace.jsonl # omega prove 轨迹 (JSONL)
docs/research/qed/jc-formal-paper/
└── paper.md             # 论文摘要草稿
```

## 核心发现

| 发现 | 细节 |
|:-----|:------|
| JC 模型形式化可行 | 有限截断 Fock 空间用 `Matrix (Fin (N+1)) (Fin (N+1)) ℂ` 精确表示 |
| `Real.sqrt` 不可计算 | 产生湮灭算符必须标记 `noncomputable` |
| 2×2 块对角化用 `Fin.sum_univ_two` | 求和展开后 `simp` 可自动处理 |
| `native_decide` 对 ℂ 不行 | 复数 `DecidableEq` 不可计算，改用 `fin_cases` + `simp` |
| `Fin (N+1)` 上求和需手动展开 | `Finset.sum_ite_eq'` 和 `Finset.sum_eq_single` 可行 |
| 无文献错误发现 | JC 1963 已被数百万次验证 |

## 关键证明技巧总结

1. **Creation action**: `(creationOp * basisVector) i n` 简化 `creationOp i n` 用 `Finset.sum_eq_single`
2. **Eigenvalues**: `Fin.sum_univ_two` 展开 `Fin 2` 求和 → `simp` 完成
3. **Dressed orthogonality**: `Fin.sum_univ_two` + `norm_num` 处理 `star(1) = 1`
4. **Noncomputable**: `noncomputable section` 覆盖全文件处理复数

## 扩展方向

- [ ] 完整 `[a, a†]` 对易关系的矩阵元证明（当前为 operator-on-basis 版本）
- [ ] 块对角全 Hamiltonian（把 2×2 块嵌入 `2(N+1)` 维空间）
- [ ] 验证 Boyd/Grudinin 推导链是否有可发现错误

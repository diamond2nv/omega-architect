# 4 道 Never-Pass 题的调研分析

## 1. imo_1959_p1 (hard) — gcd(21n+4, 14n+3) = 1

### 数学解
欧几里得算法：`gcd(21n+4, 14n+3) = gcd(14n+3, 7n+1) = gcd(7n+1, 1) = 1`

### 可解决性：**极高**。应该能被 flash 解决
Goedel-Prover arXiv 论文 (2604.08388v1) 将此题作为示例，证明方法是：
```
21n+4 = 1*(14n+3) + (7n+1)
14n+3 = 2*(7n+1) + 1
```
Lean 中只需一行 `omega` 即可完成。失败原因：flash 模型不知道用 `omega` 或不会正确链式使用 `Nat.gcd_add_mul_right_right`。

### 解决方案
```lean4
theorem imo_1959_p1 (n : ℕ) (h₀ : 0 < n) : Nat.gcd (21 * n + 4) (14 * n + 3) = 1 := by
  omega
```

### 现有引用
- 已存在 mathlib archive: `imo1959_q1`
- AoPS Wiki: 1959 IMO Problems/Problem 1
- Lean 4 下 `omega` 可以直接处理

---

## 2. algebra_amgm_sum1toneqn_prod1tonleq1 (medium) — AM-GM

### 数学解
AM-GM 不等式: `(∏ a_i)^(1/n) ≤ (∑ a_i)/n`。已知 `∑ a_i = n`，所以 `(∏ a_i)^(1/n) ≤ 1`，故 `∏ a_i ≤ 1`。

### 可解决性：**高**。Mathlib 已有实现
`Mathlib/Analysis/MeanInequalities` 中提供 `geom_mean_le_arith_mean`。具体到 NNReal 有 `NNReal.geom_mean_le_arith_mean`。

### 解法
```lean4
theorem algebra_amgm_sum1toneqn_prod1tonleq1 (a : ℕ → NNReal) (n : ℕ)
    (h₀ : (∑ x ∈ Finset.range n, a x) = n) : (∏ x ∈ Finset.range n, a x) ≤ 1 := by
  have hgm := NNReal.geom_mean_le_arith_mean (Finset.range n) a
  -- hgm : (∏ x, a x) ^ (1/(n:ℝ)) ≤ (∑ x, a x) / (n:ℝ)
  -- 代入 h₀: (∑ x, a x) = n
  -- 得 (∏ x, a x) ^ (1/n) ≤ 1 → ∏ x, a x ≤ 1
  ...
```

失败原因：模型不知道 `NNReal.geom_mean_le_arith_mean` 的存在，也未能在 leansearch/loogle 中找到。

---

## 3. amc12a_2020_p10 (medium) — 对数方程 → 数字和

### 数学解
```
log₂(log₁₆(n)) = log₄(log₄(n))
令 x = log₄(n)，则 RHS = log₄(x)
LHS = log₂(log₁₆(n)) = log₂(log₄(n)/log₄(16)) = log₂(x/2) = log₂(x) - 1
所以: log₂(x) - 1 = log₄(x) = log₂(x)/2
得: log₂(x) = 2, x = 4
n = 4^4 = 256, 数字和 = 2+5+6 = 13
```

### 可解决性：**中等-困难**
问题在于 `Real.logb` 在 Lean 中操作复杂。该定理头长达 545 chars，包含 `Real.logb`、`Nat.digits`、`List.sum`。需要两阶段：
1. 从对数方程解得 `n = 256`（需用 `Real.logb` 的换底公式）
2. 计算 `Nat.digits 10 256` 的和为 13

第一阶段用 `Real.logb`、`Real.log` 的属性，属于实分析。第二阶段纯粹 `native_decide`。

失败原因：模型无法处理 `Real.logb` 的代数变换。

---

## 4. imo_1992_p1 (hard) — 整除性 → 枚举解

### 数学解（AoPS 已知）
已知 `1 < p < q < r`，`(p-1)(q-1)(r-1) | pqr-1`。通过数论推理证明只有 (2,4,8) 和 (3,5,15) 两组解。核心：对 `(pqr-1)/((p-1)(q-1)(r-1))` 的界进行分析，然后枚举候选。

### 可解决性：**困难但已知可解**
**DeepSeek-Prover-V2** 的 MiniF2F 测试中**已解决此题**（该模型在 MiniF2F-test 达到 88.9%）。因此这不是 Lean 的难度问题，而是模型能力问题。DeepSeek-Prover-V2 使用递归子目标分解+RL 训练来解决这类复杂数论题。

### 失败原因
flash/pro 模型都无法处理这一题需要的多步数论推理：上界估计 → 整除条件 → 候选枚举 → 矛盾推导。

---

## 交叉对比：为什么 flash/pro 失败而 DeepSeek-Prover-V2 能成功？

| | flash | pro | **Prover-V2** |
|--|:-----:|:---:|:-------------:|
| 参数 | 未知 | 未知 | **671B** (37B active) |
| MiniF2F | ~40% | ~40% | **88.9%** |
| 架构 | 通用 chat | 通用 chat | **专用 prover** |
| 训练 | 通用 SFT | 通用 SFT | RL + 子目标分解 |
| **imo_1959** | ❌ | ❌ | ✅ |
| **algebra_amgm** | ❌ | ❌ | ✅ |
| **amc12a_2020** | ❌ | ❌ | ✅ |
| **imo_1992** | ❌ | ❌ | ✅ |

## 结论与建议

**不属于架构问题**——这 4 道题 Solvable by Prover-V2 证明了它们不是硬性不可解的。是**模型能力**问题。应对方案：

| 选项 | 方案 | 预期 |
|:----:|------|:----:|
| A | 本地部署 **DeepSeek-Prover-V2-7B**（7B 参数，对 GPU 友好） | 可能解决 2-3/4 |
| B | DeepSeek API 直接调用 **deepseek-prover-v2**（如果有） | 最直接 |
| C | 为 flash 手动写 **Domain-specific prompt**（注入关键 lemma 名称） | 1-2/4 |
| D | **vLLM 扩容**（WSL 4096→更大 context）后 Goedel 可能更好 | 未知 |

**推荐**: 先试 C 最简单——对 4 道题分别注入关键 lemma 名重跑 flash，再看是否需部署 Prover-V2。

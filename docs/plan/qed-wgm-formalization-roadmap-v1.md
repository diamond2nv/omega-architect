---
plan_id: qed-wgm-formalization-roadmap-v1
title: "QED × WGM 形式化路线图 — 设计哲学驱动的代数推理规划"
version: 1.0
date: 2026-06-24
author: hermes-agent
status: draft
budget: "$12 (≈700 LLM calls, across all phases)"
timeline: "8 周 (Phase 0-4)"
depends_on:
  - plan/jc-model-formalization-v1.md
  - qed-wgm/ch02-wgm-formalization.qmd
  - plans/jc-model-formalization-v1.md
implements: "AGENTS.md Layer 2 (Formal Engine) — QED×WGM domain expansion"
sources:
  - "arXiv:2605.27734 (Koopman/DL memory theory)"
  - "agentic-system symmetry-breaking framework"
  - "集智俱乐部 记忆五重境界 review"
  - "MiMo Code computation/memory/evolution architecture"
---

# QED × WGM 形式化路线图

> **目标**: 将 5 种设计哲学（Takens/Koopman/DL/ODE/Foundation 记忆 + 对称-破缺）转化为 omega-architect 的 Lean 4 证明策略，系统化地形式化 QED 和 WGM 领域的代数推理命题。

---

## 0. 设计哲学 → Omega 证明策略映射

| 哲学来源 | Omega 证明策略 | 实现位置 |
|:---------|:---------------|:---------|
| **Takens 延迟嵌入** | ErrorMemory 的预测性提取 — 用错误序列嵌入预测最优 tactic | `omega/loop/error_memory.py` (增强) |
| **Koopman 升维线性化** | Theorem 空间的引理计数向量 → 线性路由矩阵 A | `omega/search/aggregator.py` (增强) |
| **对称-破缺 (SymmetryBreaker)** | 证明中的四类对称性识别(Translational/Functional/Temporal/Rotational) | `omega/classifier/` (新增模块) |
| **多尺度记忆 (MiMo Code)** | 4 层记忆: 错误级→定理级→会话级→跨项目 | `omega/loop/` (架构改进) |
| **Neural ODE 连续化** | 连续 ODE 耦合模的直接 Lean 表述 | `omega/formal/qed_wgm/` (新域) |
| **Foundation 跨系统迁移** | 跨领域引理复用 → SearchAggregator 扩展 | `omega/search/` (增强) |

---

## 1. Phase 0: JC 模型 (已完成 ✅)

| 定理 | 行数 | 代数类型 |
|:-----|:----:|:---------|
| Fock 空间维度 | 15 | `Nat` 递归 |
| 升降算符作用 | 18 | 代数重写 |
| Rabi 本征值 | 20 | 多项式根 |
| Dressed 态正交性 | 15 | 内积代数 |

**设计哲学映射**: 破缺真空对称性 → 得到 dressed 态。每个定理的证明都是对一个对称性（Fock 空间整数索引、升降算符 Hermite 共轭、Rabi 分裂简并）的破缺操作。

---

## 2. Phase 1: WGM SBS 耦合模代数 (2-3 天, ~80 lines)

**预算**: $1 (≈60 LLM calls)
**依赖**: Mathlib `Analysis/ODE` 基础

### 定理清单

| # | 定理 | Lean 行数 | 代数约束 | 复杂度 |
|:-:|:-----|:---------:|:---------|:------:|
| P1.1 | **Manley-Rowe 关系**: $\frac{d}{dt}\|a_p\|^2 = -\frac{d}{dt}\|a_s\|^2$ | 20 | 无损耗 | ⭐ |
| P1.2 | **无损耗能量守恒**: $\sum\|a_i\|^2 = const$ | 15 | ODE 链式法则 | ⭐ |
| P1.3 | **稳态相位锁定**: $\text{Im}(a_p a_s^* a_b) = 0$ | 15 | 复数代数 | ⭐ |
| P1.4 | **小信号增益近似**: $a_s \propto e^{g t}$ | 30 | 线性 ODE 解 | ⭐⭐ |

### 证明策略

```lean4
theorem manley_rowe (sys : SBS_System) (t : ℝ) (hγ : sys.γ_p = 0 ∧ sys.γ_s = 0 ∧ sys.γ_b = 0) :
    deriv (λ t => ‖sys.a_p t‖^2) t = -deriv (λ t => ‖sys.a_s t‖^2) t := by
  -- 代入 SBS 方程 → simp [SBS_equations] → ring
  -- 对称类型: Translational (γ 的尺度不变性)
```

**设计哲学映射**:
- **Takens**: 泵浦振幅的单变量历史 → 可预测 Stokes/声子的行为（因为耦合关系隐含在 3-ODE 的延迟嵌入中）
- **对称-破缺**: 破缺"无耦合"对称性（三个独立模式）→ 建立三波耦合守恒律
- **Koopman**: 三变量耦合 ODE → 在功率空间中是线性的

---

## 3. Phase 2: WGM 光频梳谱 (1 周, ~150 lines)

**预算**: $2 (≈120 LLM calls)
**依赖**: Mathlib `Algebra/GroupPower` + `Data/Int/Basic`

### 定理清单

| # | 定理 | Lean 行数 | 代数约束 | 复杂度 |
|:-:|:-----|:---------:|:---------|:------:|
| P2.1 | **梳齿加法群**: $f_n + f_m = f_{n+m} + f_{ceo}$ | 25 | `ℤ` 群作用 | ⭐ |
| P2.2 | **频率差均匀化**: $f_{n+1} - f_n = f_{rep}$ | 20 | 多项式 `ring` | ⭐ |
| P2.3 | **CEO 偏移模性**: $f_{ceo} \mod f_{rep}$ 唯一 | 30 | 同余 `ZMod` | ⭐⭐ |
| P2.4 | **整数频率对合**: $f_{-n} = 2f_{ceo} - f_n$ | 25 | `ℤ` 对称性 | ⭐ |
| P2.5 | **频率梳覆盖性**: $\forall f \in S,\exists! n: f = f_n$ | 50 | `Set` + `ExistsUnique` | ⭐⭐⭐ |

### 证明策略

```lean4
theorem comb_uniformity (f_ceo f_rep : ℝ) (hrep : f_rep > 0) (n : ℤ) :
    (f_ceo + (n+1 : ℤ) • f_rep) - (f_ceo + n • f_rep) = f_rep := by
  ring
```

**设计哲学映射**:
- **对称-破缺 Rotational**: $n \to n+1$ 是 ℤ 上的平移对称性 → 证明频率差不受 $n$ 影响 = 破缺平移对称性的"冗余性"推论
- **对称-破缺 Temporal**: 梳齿等间距 ≈ 时间域上的周期脉冲序列
- **MiMo Code 的 4 层记忆**: CEO 偏移是"全局记忆"（跨所有梳齿的常数），而梳齿索引是"局部记忆"

---

## 4. Phase 3: QED Clifford 代数 (2 周, ~300 lines)

**预算**: $3 (≈180 LLM calls)
**依赖**: Mathlib `LinearAlgebra/CliffordAlgebra` + `LinearAlgebra/TensorProduct`

### 定理清单

| # | 定理 | Lean 行数 | 代数约束 | 复杂度 |
|:-:|:-----|:---------:|:---------|:------:|
| P3.1 | **反交换关系**: $\{\gamma^\mu, \gamma^\nu\} = 2g^{\mu\nu}$ | 80 | Clifford 代数 | ⭐⭐⭐ |
| P3.2 | **Dirac 平方**: $(\gamma^\mu p_\mu)^2 = p^2$ | 50 | Clifford 简化 | ⭐⭐ |
| P3.3 | **手征性**: $[\gamma^5, \gamma^\mu] = 0$ 或？ | 40 | 反交换关系推演 | ⭐⭐ |
| P3.4 | **迹的循环性**: $\text{Tr}(\gamma^\mu\gamma^\nu) = 4g^{\mu\nu}$ | 60 | 矩阵代数 | ⭐⭐⭐ |
| P3.5 | **Fierz 恒等式**: $\sum_a (\gamma^a)_{ij}(\gamma_a)_{kl}$ | 70 | 展开+化简 | ⭐⭐⭐⭐ |

### 证明策略

```lean4
-- 使用 Mathlib 的 CliffordAlgebra 实现 γ 矩阵
def gamma : ℕ → CliffordAlgebra (QuadraticForm ℝ 4) := ...

theorem clifford_anticomm (μ ν : ℕ) (hμ : μ < 4) (hν : ν < 4) :
    gamma μ * gamma ν + gamma ν * gamma μ = 2 • (g μ ν) := by
  -- 由 CliffordAlgebra 定义直接得出
  exact CliffordAlgebra.ι_mul_ι_add_ι_mul_ι _ _
```

**设计哲学映射**:
- **对称-破缺**: γ-矩阵反交换关系 = 时空对称性的旋量实现
- **Koopman 升维**: Clifford 代数把 Lorentz 群的非线性表示 → 在 Clifford 代数空间线性化
- **Takens**: Dirac 旋量的"历史延迟"体现在 γ-矩阵链的增广模式中

---

## 5. Phase 4: WGM 微扰理论 (3 周, ~500 lines)

**预算**: $4 (≈240 LLM calls)
**依赖**: Phase 3 Clifford 代数 + Mathlib `Analysis/InnerProductSpace`

### 定理清单

| # | 定理 | Lean 行数 | 代数约束 | 复杂度 |
|:-:|:-----|:---------:|:---------|:------:|
| P4.1 | **一阶校正公式**: $\Delta\omega^{(1)} = \langle\psi_0\|V\|\psi_0\rangle$ | 80 | 内积代数 | ⭐⭐ |
| P4.2 | **二阶校正公式**: $\Delta\omega^{(2)} = \sum_{k\neq0}\frac{|\langle\psi_k\|V\|\psi_0\rangle|^2}{\omega_0-\omega_k}$ | 120 | 求和+分母非零 | ⭐⭐⭐ |
| P4.3 | **模式正交性**: $\int \psi_m^*\psi_n \, d\Omega = \delta_{mn}$ | 100 | 积分代数 | ⭐⭐⭐ |
| P4.4 | **WGM 频率量化**: $\omega_{l,m} = \frac{c}{R}\sqrt{l(l+1)} + \text{corrections}$ | 100 | 球谐函数 | ⭐⭐⭐⭐ |
| P4.5 | **应力-应变谱分裂**: $\Delta\omega_{\text{stress}} = \epsilon \cdot c_{\text{photoelastic}}$ | 100 | 张量代数 | ⭐⭐⭐ |

---

## 6. 跨阶段设计哲学集成

### 6.1 ErrorMemory 增强 (Phase 1 完成时实施)

借鉴 **Takens 延迟嵌入** 到 ErrorMemory 的查询：

```python
# omega/loop/error_memory.py 新增
def suggest_by_delay_embedding(error_sequence: list[str], k: int = 3) -> str:
    """
    将最近 k 个 error 类别的序列作为延迟嵌入向量，
    匹配 ErrorMemory 中具有相同嵌入向量的历史修复。
    """
    embedding = "_".join(error_sequence[-k:])
    return fuzzy_match(embedding, self.error_to_fix_map)
```

### 6.2 SearchAggregator 增强 (Phase 2 完成时实施)

借鉴 **Koopman 线性化** 到定理路由：

```python
# omega/search/aggregator.py 新增 KoopmanRouter
class KoopmanRouter:
    """将定理映射到引理使用频率向量 v ∈ ℕ^k，线性预测最优路径"""
    
    def build_lemma_vector(self, theorem: str) -> np.ndarray:
        # 对定理做 LeanSearch/Loogle → 计算用到哪些引理
        ...
    
    def predict_path(self, v: np.ndarray) -> str:
        # v_{t+1} = A · v_t, A 从历史轨迹学习
        ...
```

### 6.3 SymmetryClassifier 新增模块 (Phase 3 完成时实施)

```python
# omega/classifier/symmetry.py 新增
class SymmetryClassifier:
    """
    将编译错误/证明状态分类为四类对称性破缺，
    指导最优的破缺策略。
    """
    TRANSLATIONAL = "translational"  # 参数平移 → ring
    FUNCTIONAL   = "functional"     # 行为重复 → simp with patterns
    TEMPORAL     = "temporal"       # 时间周期 → induction
    ROTATIONAL   = "rotational"     # 旋转不变 → group action
```

---

## 7. 预算与里程碑

| Phase | 内容 | Lean 行数 | 预算 | 时间 | 交付物 |
|:------|:-----|:---------:|:----:|:----:|:-------|
| **0** | JC 模型 | 68 | $3.20 ✅ 已花 | ✅ 完成 | `omega/formal/jc_model/` |
| **1** | SBS 耦合模 | 80 | $1 | 2-3 天 | `omega/formal/qed_wgm/sbs/` |
| **2** | 频率梳 | 150 | $2 | 1 周 | `omega/formal/qed_wgm/comb/` |
| **3** | Clifford 代数 | 300 | $3 | 2 周 | `omega/formal/qed/` |
| **4** | WGM 微扰 | 500 | $4 | 3 周 | `omega/formal/qed_wgm/perturbation/` |
| **总计** | — | ~1,098 | ~$12 | ~8 周 | — |

---

## 8. 与 AGENTS.md 的融合点

> **注**: AGENTS.md 更新推迟到后续迭代。以下为暂存的设计点：

```
AGENTS.md 新增子章节: "Layer 2.5: Design-Philosophy-Inspired Strategies"
├── Strategy A: Takens Delay Embedding → ErrorMemory enhancement
├── Strategy B: Koopman Linearization → SearchAggregator enhancement
├── Strategy C: Symmetry Classification → new classifier module
├── Strategy D: Multi-Scale Memory → memory hierarchy in InnerLoop
└── Strategy E: Cross-Domain Transfer → SearchAggregator extension
```

同时更新 `docs/plan/index.md`：

```
| **F0** | **QED×WGM Formalization** | 🔴 P0 | draft | [qed-wgm-formalization-roadmap-v1.md](./qed-wgm-formalization-roadmap-v1.md) |
```

---

## 9. 启动检查

Phase 1 启动前需确认：
- [ ] `lake` 项目已存在且可用
- [ ] Mathlib `Analysis/ODE` 已同步
- [ ] `omega/formal/qed_wgm/` 目录已创建
- [ ] SBS_System 结构体定义已完成
- [ ] 无损耗条件下的链式法则 `deriv` 可用

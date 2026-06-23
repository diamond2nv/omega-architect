---
plan_id: jc-model-formalization-v1
title: "Jaynes-Cummings 模型 Lean 4 形式化 — 终验方案"
version: 0.1
date: 2026-06-24
author: hermes-agent
status: draft
budget: "$5 (≈300 LLM calls)"
timeline: "2 周 (Week 3-4 of QED×WGM roadmap)"
implements: "QED×WGM Phase 0 — JC 模型有限截断形式化"
target_journal: "Quantum (首选) / arXiv (备选)"
---

# Jaynes-Cummings 模型 Lean 4 形式化实施方案

> **目标：** 通过 omega-architect 管线，将 Jaynes-Cummings 模型的 4 个 P0 核心定理编译为可执行的 Lean 4 证明，保存完整 trace (JSONL) 和 Lean 代码，发表于 Quantum 或 arXiv。

## 最终输出清单

| 交付物 | 路径 | 说明 |
|:-------|:-----|:------|
| Lean 证明代码 | `omega/formal/jc_model/` | 4 个 P0 定理 + 辅助定义 |
| JSONL trace | `~/.omega/experiments/jc-model-2026-06.jsonl` | 每步 T1/T2 交互记录 |
| 验证报告 | `docs/research/qed/jc-formal-report.md` | 编译结果、成本、发现 |
| 论文草稿 | `docs/research/qed/jc-formal-paper/` | Quantum 格式的 LaTeX 摘要/论文 |

## 4 个 P0 定理

| # | 定理 | Lean 行数 | 物理意义 |
|:-:|:-----|:---------:|:---------|
| 1 | 有限截断 Fock 空间定义 $N_{\text{max}}$ 维 | ~30 | JC 模型 Hilbert 空间基础 |
| 2 | 产生湮灭算符矩阵表示 + $[a,a^\dagger]=1$ | ~40 | 对易关系的矩阵证明 |
| 3 | Hamiltonian 对角化 → Rabi 分裂 $\Omega_R=2g\sqrt{n+1}$ | ~50 | 核心物理结果 |
| 4 | 穿衣态 $\|\pm, n\rangle$ 存在性与正交性 | ~30 | 本征态形式化 |

## 预算分解

| 活动 | LLM 调用 | 成本 ($) | 说明 |
|:-----|:--------:|:--------:|:------|
| T1 物理方程验证 | 50 | ~0.75 | 检查 JC 1963 原文公式 3 遍交叉验证 |
| Blueprint 分解 | 30 | ~0.50 | 4 定理 → T2 可编译子目标 |
| T2 Lean 编译 (神循环) | 150 | ~2.50 | lake env lean 每轮~30s |
| 错误修正回环 | 50 | ~1.00 | 类型错误/导入/语法 |
| 最终验证 + JSONL 整理 | 20 | ~0.25 | 全部 4 定理通过编译 |
| **合计** | **~300** | **~$5.00** | |

## 执行步骤

### Step 1: 基础设施准备

```bash
mkdir -p docs/research/qed/jc-formal-paper/
mkdir -p omega/formal/jc_model/
```

### Step 2: 逐定理形式化

每个定理按 TDD 风格执行：

```
定理 N 定义 → `omega prove` → 编译失败 → 修正 → 重试
                                  ↓ 成功
                  记录 JSONL → 保存 Lean → 下一个定理
```

具体命令：
```bash
cd /home/shenli/Gitlab/Agentic4Sci/omega-architect

# 定理 1: 截断 Fock 空间
omega prove --imports "import Mathlib" \
  --samples 4 --rounds 3 --timeout 120 \
  "theorem truncated_fock_dim (N : ℕ) : Fintype.card (Fin (N+1)) = N+1 :="

# 定理 2: 产生湮灭算符 + [a,a†]=1
omega prove --imports "import Mathlib" \
  --samples 4 --rounds 3 --timeout 120 \
  "theorem commutator_identity (n : ℕ) (i j : Fin (n+1)) : ... :="

# ... 类似
```

### Step 3: JSONL 数据管理

```python
# trace_db.py — 从 ~/.omega/experiments/*.jsonl 提取并整理
from omega.data import load_jsonl, RecordWriter

# 加载 trace
traces = load_jsonl("~/.omega/experiments/jc-model-2026-06.jsonl")

# 按定理分组统计
for theorem_id in ["fock_dim", "commutator", "rabi_split", "dressed_states"]:
    entries = [t for t in traces if t.get("theorem") == theorem_id]
    print(f"{theorem_id}: {len(entries)} attempts, "
          f"success={'✅' if any(e.get('success') for e in entries) else '❌'}")
```

### Step 4: 论文草稿

如果 4 个定理全部编译通过 → 写 Quantum 论文摘要：

```
JC Model Formalized in Lean 4: A Verified Foundation for Cavity QED
       and Whispering-Gallery-Mode Integrated Photonics

Authors: Shen Li (omega-architect)
Target: Quantum / arXiv:quant-ph

Abstract:
We present the first complete Lean 4 formalization of the Jaynes-Cummings
model — the fundamental Hamiltonian describing light-matter interaction
in cavity QED. The formalization covers: (1) a truncated Fock space
with explicit finite-dimensional matrix representation, (2) the
canonical commutation relation [a,a†]=1 verified as a matrix identity,
(3) diagonalization of the JC Hamiltonian yielding the vacuum Rabi
splitting Ω_R = 2g√(n+1), and (4) the existence and orthogonality of
dressed states |±,n⟩. The entire proof (~150 lines) compiles under
Physlib + Mathlib in Lean 4. ...
```

### Step 5: 发表决策树

```
全部 4 定理编译通过？
├── ✅ 是 → 检查 Boyd/Grudinin/JC 原文推导链
│   ├── ✅ 发现错误 → 写 PRL (放弃 Quantum)
│   └── ❌ 无错误 → 写 Quantum 论文 (~3-5 pages)
└── ❌ 部分失败 → 记录失败原因 → arXiv
    ├── ⚠️ 基础设施缺口 → arXiv: 附缺口报告
    └── ⚠️ 物理错误 → arXiv: 发现的物理问题
```

## 风险管理

| 风险 | 概率 | 影响 | 缓解 |
|:-----|:----:|:----:|:------|
| Physlib QuantumInfo 缺少有限截断 Fock 空间定义 | 🟡 中 | 高 | 用 `Matrix (Fin dim) (Fin dim) ℂ` 自制 — 不依赖 QuantumInfo |
| Lean 编译超时 (≥120s) | 🟡 低 | 中 | 增加 `--timeout 200` |
| $5 预算不足 (≥300 次 → ≥400 次) | 🟢 低 | 低 | 每多 100 次 ≈ $1.50，可申请追加 |
| 论文压 JC 1963 原文发现符号错误 | 🟢 低 | 正面 | 正是 Tooby-Smith 模式的最优结果 |

## 交付验证清单

- [ ] `omega formal/jc_model/fock_space.lean` — 编译通过
- [ ] `omega formal/jc_model/commutator.lean` — 编译通过
- [ ] `omega formal/jc_model/hamiltonian.lean` — 编译通过
- [ ] `omega formal/jc_model/dressed_states.lean` — 编译通过
- [ ] `~/.omega/experiments/jc-model-2026-06.jsonl` — 非空，可 `load_jsonl`
- [ ] `docs/research/qed/jc-formal-report.md` — 含成本、时间、结果
- [ ] 论文草稿 `docs/research/qed/jc-formal-paper/` — 初稿完成

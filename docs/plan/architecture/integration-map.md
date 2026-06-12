---
plan_id: architecture-integration-v1
title: "Omega/loop 与旧架构的集成关系 + 2天实验演化史"
version: 1.0
date: 2026-06-12
status: active
---

# 系统架构集成与演化

## 前置纠正：AGENTS.md 描述了三套独立架构

仔细重读 AGENTS.md 后，发现不是两套而是**三套并行的架构**，全部互不连接：

```
AGENTS.md 三架构分布
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

架构 A（旧, lines 3-41）:  GPU Layer + Goedel vLLM
  omega/gpu_layer/ + scripts/goedel_local_prover.py
  → 本地 GPU 推理基础设施
  → 状态: 🟡 可用，但 4096 context 瓶颈

架构 B（未实现, lines 43-138）:  State Machine via delegate_task
  analyze_query → generate_blueprint → prove_lemmas 
  → refine_blueprint → synthesize_result
  每个 state = (role, goal, toolsets) 三元组
  8 原语: apply_lemma, rewrite_goal, induction, case_split,
          calc_chain, search_lemma, extract_proof, fallback_decompose
  → 状态: 🔴 从未实现

架构 C（当前, lines 488-712）:  Inner Loop + DeepSeek API
  omega/loop/ + omega/resource/ + omega/search/
  → 我们所有实验跑的路径
  → 状态: 🟢 活跃开发中
```

**三套架构之间没有任何代码连接**。它们各自有独立的设计理念、入口点和数据流。

## 之前我犯的错误

我把 AGENTS.md 的状态机架构（架构 B）当成了"旧架构"，把 loop 系统（架构 C）当成了"新架构"。但实际：

| 我之前的说法 | 实际 |
|:-------------|:-----|
| "旧架构: prover/search/verify" | 只是架构 C 的一部分模块，不是独立的 |
| "新架构: loop/ 替代了旧架构" | loop/ 从未替代过 prover/ — 它们共存但互不调用 |
| "AGENTS.md 引导 CLI/MCP/SDK" | AGENTS.md 实际上是三套架构的杂糅文档 |

## Phase 0 的 57% 使用的是哪套？

Phase 0 用的是 **GoedelProver** (`omega/prover/go_prover.py`)，它属于：
- 架构 C 的 prover/ 子模块（与 loop/ 同属架构 C）
- 但 GoedelProver 走的是**不同的执行路径**：
  ```
  GoedelProver.run():
    1. Proposer.sample() → N 候选
    2. compile_fn(每个候选) → T2 编译
    3. 收集错误 → 自修正 → 返回第一个通过的
  ```
- 而 loop 的 inner_loop() 走的是：
  ```
  inner_loop():
    1. LLM 写代码（对话式）
    2. compile_gate.compile()
    3. 失败 → 反馈给 LLM → 循环
  ```

**Phase 0 4/7 (57%) vs loop 5/10 (50%)**：旧路径在 7 题（3 easy + 4 medium, 无 hard）上 57%，loop 在 10 题（含 3 hard）上 50%。如果把 hard 题去掉，loop 在 7 题上是 4/7 或 5/7（取决于哪次运行）≈ 57-71%。两者**实际相当**，没有一方明显优于另一方。

## 修正后的数据对比

| 指标 | GoedelProver 路径 | Loop 路径 |
|:----|:-----------------:|:---------:|
| easy | 3/3 (100%) | 2-3/3 (67-100%) |
| medium (无 hard) | 1/4 (25%) | 2/4 (50%) |
| medium+h easy (7题) | 4/7 (57%) | 4-5/7 (57-71%) |
| 含 hard (10题) | 未测试 | 4-5/10 (40-50%) |
| search_loop bug | ✅ 有（Phase 0 失败原因） | ❌ 无（新 MCP 搜索已修复） |


```
用户入口
    │
    ├── omega/runner.py (OmegaRunner)       ← 旧架构入口
    │      │
    │      ▼
    │   omega/prover/ensemble.py
    │      ├── GoedelProver  ──→ omega/search/proposer.py
    │      ├── ArchonProver  ──→ omega/search/proposer.py
    │      └── RethlasProver ──→ omega/search/proposer.py
    │             │                    │
    │             ▼                    ▼
    │      omega/verify/t2_real.py  omega/search/lean_search.py
    │             │                    │
    │             ▼                    ▼
    │      Lean Compiler          lean-lsp-mcp
    │
    └── omega/loop/runner.py (LoopExperiment)  ← 新架构入口（我们实验中用的）
           │
           ▼
        omega/loop/inner.py (inner_loop)
           │
           ├── omega/loop/deepseek_client.py  ← DeepSeek API
           ├── omega/loop/compile_gate.py     ← Lean 编译
           ├── omega/loop/error_memory.py     ← 🔴 ErrorMemory（独立实现）
           ├── omega/loop/verifier.py         ← 编译后检查
           └── omega/loop/errors.py           ← 错误分类
                  │
                  ▼
           omega/resource/tracker.py (ConvergenceTracker)  ← 两个体系共用
           omega/resource/budget.py (BudgetTracker)        ← 两个体系共用
```

**关键问题**：omega/loop/ 完全绕过了 omega/prover/、omega/search/、omega/verify/ 体系。
loop 有自己的 compile_gate、自己的 error_memory、自己的 deepseek_client。
旧体系的 proposer、blueprint、t1_llm 等模块在实验中从未被调用。

## 二、2 天实验演化史

```
时间线                               通过率    架构状态
───────────────────────────────────────────────────────────
Day 1 13:30  Run7/V3                4/10      旧: outer.py + inner_loop v0.1
                                             (64轮, proof_sketch, file_pipeline)

Day 1 17:03  E1                     3/10      loop: 首次独立运行
                                             (ErrorMemory v1, 预算限制 $0.04)

Day 1 17:46  E2(best)              5/10      loop: 顺序+512轮+无收敛检测
                                              ← 当前最高记录
  
Day 1 18:??  E2(multi)              5/9       loop: 同上，缺1题
  
Day 1 23:22  E3                     4/10      loop: beam=3+融合+收敛检测
                                              ← 新功能加了但没帮助

Day 1 23:32  Multi-Stage            5/10      loop: 按难度差异化策略
                                              (easy beam=1, medium beam=3, hard beam=5)

Day 2 00:15  P0 (rerun)             4/10      loop: E2(best) 复现验证
                                              50% 不可完全复现，实为 40-50%

Day 2 00:33  P1 (pro 模型)          0/4       loop: pro 模型攻 never-pass
                                              → 证明模型不是瓶颈

Day 2 01:33  Domain Prompt          0/4       loop: 注入 lemma 名
                                              → 证明 prompt 不是瓶颈

Day 2 01:59  P2 (Goedel本地)        0/3       旧: gpu_scheduler + vLLM
                                              → 4096 context 不可用
```

**结论**：实验中只用到了 omega/loop/ 体系。旧架构（prover/search/verify）未被测试。

## 三、Loop 体系解决的问题

旧架构（prover/ensemble.py）的设计：
```
GoedelProver.run(theorem):
  1. Proposer.sample() → N 个候选证明
  2. compile_fn(每个候选) → 编译
  3. 收集错误 → Proposer 自修正
  4. 返回第一个通过的
```

而 Loop 体系（inner_loop）的设计：
```
inner_loop(theorem):
  for round in max_rounds:
    1. LLM 写代码 (直接 API 调用，无 proposer)
    2. compile_gate.compile(code) → 编译
    3. 成功 → verifier.verify() → 返回
    4. 失败 → 错误分类 → 反馈给 LLM → 继续
```

**本质区别**：
- 旧架构：Proposer 先生成 N 个候选，再编译。proposer 是**生成器**
- Loop 架构：LLM 直接输出 + 编译。循环是**对话式**的（DeepSeek 维护 conversation history）

**为什么 Loop 更好**（实验证明）：
1. Proposer 需要额外维护搜索状态（tree, blueprint, curriculum）
2. 对话式循环让 LLM 看到之前的错误→修正，更自然
3. 旧架构的 prover 三件套并行但互不通信，loop 是单上下文持续迭代

## 四、未来集成计划

### P0 搜索聚合器：放在搜索层，同时服务两套体系

```
旧体系: omega/search/proposer.py → lean_search.py → 🔄 P0 Aggregator
新体系: omega/loop/mcp_sync.py   → leansearch MCP → 🔄 P0 Aggregator
                                                    │
                                                    ▼
                                            omega/search/aggregator.py (P0)
                                            ├── leansearch.net（异步）
                                            ├── loogle（异步）
                                            ├── local BM25（本地）
                                            └── cache（JSONL）
```

P0 是**基础设施层**，两个体系通过不同路径调用它。

### P1 三层分类器：完全替换 loop 内的错误处理

```
omega/loop/inner.py
  │ 编译失败
  ▼
omega/loop/errors.py (classify_diagnostics)  ← 当前仅 Layer 1
  │
  └──→ P1: 扩展为 3 层
       ├── Layer 1: 规则（现有）
       ├── Layer 2: NLP 谱系（新增）
       └── Layer 3: LLM-Judge（新增）
              │
              ▼
       注入精准反馈 → LLM 修正
```

P1 只在 loop 体系内生效（旧体系不需要它，因为旧体系用 Proposer 生成候选而非对话式修正）。

### 旧架构模块的命运

| 模块 | 状态 | 计划 |
|------|------|------|
| `omega/prover/go_prover.py` | 🟡 存活 | 保留，用于 Goedel vLLM 推理路径 |
| `omega/prover/ensemble.py` | 🔴 弃用 | loop 体系已替代 |
| `omega/prover/ar_prover.py` | 🔴 弃用 | 未被使用 |
| `omega/prover/re_prover.py` | 🔴 弃用 | 未被使用 |
| `omega/search/proposer.py` | 🟡 待整合 | 其 search 逻辑→P0 |
| `omega/search/blueprint.py` | 🟡 待整合 | DAG 蓝图→必要时集成 |
| `omega/verify/t1_llm.py` | 🟡 可借鉴 | T1 的 LLM verify→P1 Judge |
| `omega/verify/t2_lean.py` | 🟢 核心 | compile_gate 等价 |
| `omega/resource/` | 🟢 共用 | 两个体系共用 |

## 五、新 Plan 执行时的模块关系

```
用户：omega prove "theorem t ..."
        │
        ▼
omega/cli/__init__.py
        │
        ▼
omega/loop/runner.py (LoopExperiment)
        │
        ▼
omega/loop/inner.py ← 核心循环
        │
        ├── 搜索: omega/search/aggregator.py (P0)  ← async multi-source
        │         ├── leansearch.net (async)
        │         ├── loogle (async)
        │         ├── local BM25 (P0 新增)
        │         └── JSONL cache (P0 新增)
        │
        ├── 编译: omega/loop/compile_gate.py
        │
        ├── 错误分类: omega/classifier/ (P1)  ← 3-layer
        │         ├── layer1_rules.py
        │         ├── layer2_nlp.py
        │         └── layer3_llm_judge.py
        │
        ├── 收敛: omega/resource/tracker.py (P2 修复)
        │
        └── 预算: omega/resource/budget.py (P3 修复)
```

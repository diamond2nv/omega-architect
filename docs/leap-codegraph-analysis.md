# Omega-Architect LEAP Readiness: CodeGraph Deep Scan + Optimization

> **日期**: 2026-06-12
> **扫描工具**: CodeGraph (4,745 nodes, 4,539 edges, 206 files)
> **基线**: LEAP (Google DeepMind, arXiv 2606.03303, Putnam 12/12)

---

## 1. 全景：CodeGraph 扫描结果

```
omega/
├── agent/          → AgentOrchestrator (state machine, DECOMPOSE_TASK)
├── benchmark/      → TestSuite, BudgetProfile
├── classifier/     → P1: 3-layer error classifier
├── cli/            → omega prove/bench/config/learn
├── engine/         → HybridV2, ModeRouter, Orchestrator(engine), trajectory
├── gpu_layer/      → detector, backends, scheduler, runtime, capability
├── learn/
│   ├── policy/     → BasePolicy, LLMPolicy, RouterPolicy
│   └── rl/         → LuffyMixedTrainer (EA-GRPO), DialogueBuffer
├── loop/           → inner.py (DFS core), compile_gate, error_memory, MCP
├── plan/           → PlanManager (4 sub-plans), allocator, path_plan, gpu_plan
├── prover/         → Goedel, Rethlas, Archon, Ensemble, compiler, playbook
├── research/       → hfpclawer, arxiv, paperstore, training/LightGBM
├── resource/       → model_registry, model_router, budget, tracker, config
├── search/         → blueprint.py (DAG!), dec.py (EA-GRPO), aggregator, tree
├── skills/         → Hermes skill bridge
└── verify/         → T1 (LLM), T2 (Lean/MCP/Real)
```

---

## 2. LEAP 关键发现：Omega 已有 60-70% 的基础设施

### ✅ Omega 已有 ≈ LEAP (但分散、未连接)

| 组件 | LEAP | Omega | 文件 | 差距 |
|:-----|:-----|:------|:-----|:-----|
| **Proof DAG** | AND-OR DAG (lemma memoization) | **`Blueprint` DAG** (lemmas+edges+dependencies) | `search/blueprint.py:110` | 在 search/ 目录，Engine 和 Agent 不使用它 |
| **Blueprint 生成** | LLM → informal blueprint → formal sketch | `generate_blueprint()` + `refine_blueprint()` | `search/blueprint.py` | 有生成+修正，但未连接 Agent |
| **DAG 状态追踪** | OR nodes (goal/lemma) + AND (decomposition) | `LemmaNode` {status, proof, dependencies, diagnosis} | `search/blueprint.py:65` | 已完整 |
| **诊断系统** | LLM reviewer → backtrack | `DiagnosisType` (TOO_HARD, FALSE_STATEMENT, MISSING_DEP, TIMEOUT) | `search/blueprint.py:53` | **需要 LLM Reviewer 做过滤** |
| **并行证明** | 独立子目标并行 | `prover.run(lemma.header)` loop | `search/blueprint.py` (docstring) | 有雏形 |
| **Pass@K 评测** | Pass@128 | `OmegaPassKManager` + `PassKReport` | `search/passk.py` | ✅ 已有 |
| **编译反馈修正** | Lean compiler + revision | `inner_loop` (max_rounds=512) + `CompileGate` | `loop/inner.py:653` | ✅ 成熟 |
| **形式证明奖励** | Binary compile reward | **EA-GRPO** (edit-distance-aware) | `search/dec.py:182` | **Omega 独有优势** |
| **模式路由** | — | `ModeRouter` (DFS/Beam/Hybrid scoring) | `engine/router.py:288` | 需加 LEAP 模式 |
| **Hard 定理流程** | — | `EngineOrchestrator` (ANALYZE→BLUEPRINT→PROVE→VERIFY→SYNTHESIZE) | `engine/orchestrator.py:360` | 结构接近但更简单 |
| **Agent 流程** | Decomposer→Solver→Synthesizer | `AgentOrchestrator` (ANALYZE→SELECT→DECOMPOSE→EXEC→VERIFY→SYNTHESIZE) | `agent/orchestrator.py:51` | 通用 agent，非 LEAP 专用 |

---

## 3. 核心差距：3 个 GAP 导致 LEAP 无法落地

### GAP 1: Blueprint DAG ↔ Orchestrator 未连接 (P0, ~150行)

**问题**: `search/blueprint.py` 的 Blueprint DAG 和 `engine/orchestrator.py` 的 EngineOrchestrator 是两套独立系统。EngineOrchestrator 的 `GENERATING_BLUEPRINT` 状态只是名称，实际 invoke 的是 LLM 而非 `search/blueprint.Blueprint`。

**CodeGraph 证据**:
```
omega/engine/orchestrator.py   ──→ GENERATING_BLUEPRINT state (prompt LLM)
omega/search/blueprint.py       ──→ Blueprint DAG (完全独立，未被引入)

omega/engine/orchestrator.py   import 列表中: 无 omega.search.blueprint
omega/search/blueprint.py      callers: 无 (仅被 test 使用)
```

**修复**: `Orchestrator.run()` 调用 `Blueprint.generate()` → 存储 DAG → `unproven()` 迭代 → `Solver` 证明每个节点 → 失败时 `RefineBlueprint()`。

### GAP 2: 无 LLM Reviewer 过滤 (P0, ~120行)

**问题**: LEAP 的关键消融实验证明：无 LLM Reviewer 时，Putnam A5 即使 8 次 rollout 也无法解决（vs 2 次成功）。Reviewer 阻止"语法正确但语义无用的分解"循环。

**当前 omega 情况**:
- `search/blueprint.py:53` 的 `DiagnosisType` 有 `TOO_HARD`/`FALSE_STATEMENT`/`MISSING_DEP` 等
- 但无实际 reviewer agent 调用
- 分解质量只有"编译检查"而无语义检查

**修复**: 在 `engine/orchestrator.py` 中增加 `DecompositionReviewer`。CPU 模式用规则（子目标复杂度 < 父目标），API 模式用 DeepSeek-v4-flash (0.0005$/次)。

### GAP 3: 两个 Orchestrator 重复 (P1, ~200行归并)

**问题**: CodeGraph 发现两个 `Orchestrator` 类：

| 位置 | 用途 | 状态 |
|:-----|:------|:-----|
| `omega/agent/orchestrator.py:51` | 通用 Agent 状态机 (SELECT_SKILL, etc.) | 用于 Hermes 集成 |
| `omega/engine/orchestrator.py:360` | Hard 定理证明专用 (BLUEPRINT, PROVE_LEMMAS) | 用于定理证明 |

这两个 Orchestrator 功能重叠但接口不同。未来的 LEAP 模式需要：
- Agent Orchestrator 做 high-level 任务分解
- Engine Orchestrator 做 low-level 证明执行
- 两者共享 `Blueprint` 数据结构

**修复**:  统一 DAG 数据结构（`Blueprint`），Agent→Engine 的单向调用链，减少接口重复。

---

## 4. Omega 独有优势（LEAP 没有的）

| Omega 特性 | 文件 | 对 LEAP 融合的价值 |
|:-----------|:-----|:-------------------|
| **EA-GRPO 奖励** | `search/dec.py:182` | 训练时更偏好 edit-distance 小的证明 |
| **三分类器系统** | `classifier/` | 编译错误→精确分类→加速修正循环 |
| **BudgetTracker** | `resource/budget.py` | 硬限制 DeepSeek API 成本 (vs LEAP 无预算控制) |
| **GPU 层** | `gpu_layer/` | 未来 vLLM 加速 |
| **ModeRouter** | `engine/router.py:288` | 自动路由简单→DFS / 复杂→LEAP |
| **hfpclawer 论文库** | `research/sources/hfpclawer.py` | LEAP 无——遇到未知定理可检索相关论文 |

---

## 5. 优化路径：让 Omega 达到 LEAP 成熟度

### P0 优化 (2-3天，CPU-only 可开发)

```
当前结构：

AgentOrch → engine/ → ModeRouter → inner_loop / hybrid / prover
                                      (无 DAG, 无 blueprint 连接)

P0 目标：

AgentOrch → engine/ → ModeRouter → hybrid (新 LEAP 模式)
                                       ├─ DAG: Blueprint (search/blueprint.py)
                                       ├─ Agent: Decomposer → Solver → Synthesizer
                                       └─ Filter: DecompositionReviewer
```

**具体文件变更**:

| 文件 | 操作 | 行数 | 说明 |
|:-----|:-----|:----:|:-----|
| `engine/orchestrator.py` | 修改 | +80 | 引入 `search.blueprint.Blueprint` 替代纯 LLM 调用 |
| `engine/hybrid.py` | 增加 | +150 | Phase 4: LEAP-mode (Blueprint DAG + Agentic Prove) |
| `engine/router.py` | 修改 | +30 | `_score_leap()` + ModeRouter 第四模式 |
| `search/blueprint.py` | 增加 | +60 | `DecompositionReviewer` 类（规则/API 双模式） |
| `plan/__init__.py` | 修改 | +10 | `resolve_auto()` 路由到 LEAP 模式 |

### P1 优化 (1周，需 DeepSeek API)

| 文件 | 操作 | 行数 | 说明 |
|:-----|:-----|:----:|:-----|
| `agent/orchestrator.py` | 重构 | +100 | 去掉 `DECOMPOSE_TASK` 通用逻辑，专注 LEAP 三 Agent |
| `engine/orchestrator.py` | 修改 | +100 | 实现完整 LEAP 3-Agent 管线 + DAG 单调修正 |
| `search/blueprint.py` | 增加 | +120 | Lemma 缓存系统（跨定理语义复用 + 向量索引） |
| `learn/rl/luffy_mixed_trainer.py` | 修改 | +30 | EA-GRPO ← LEAP binary reward 融合 |
| `benchmark/lean_imo.py` | 新建 | +100 | Lean-IMO-Bench 评测入口 |

### P2 优化 (1月+, 需 Lean Toolchain + GPU)

| 文件 | 操作 | 行数 | 说明 |
|:-----|:-----|:----:|:-----|
| `gpu_layer/dag_scheduler.py` | 新建 | +200 | GPU 并行 DAG 节点展开 |
| `research/sources/lemma_retrieval.py` | 新建 | +200 | hfpclawer × blueprint lemma 检索 |
| `search/blueprint.py` | 修改 | +100 | Learn-to-search: 从失败 DAG 路径学习 |

---

## 6. 量化目标

| 指标 | 当前 | P0 | P1 | P2 |
|:-----|:----:|:--:|:--:|:--:|
| MiniF2F formal rate | ~40% | 55% | 65% | 70% |
| Lemma 复用率 | 0% | 20% | 35% | 45% |
| 无效分解回溯率 | 0% | 30% | 50% | 60% |
| 复杂定理(3+步)成功率 | ~20% | 40% | 50% | 60% |
| 新代码行 | 0 | ~330 | ~450 | ~500 |
| 测试覆盖 | 717 | +60 | +80 | +100 |

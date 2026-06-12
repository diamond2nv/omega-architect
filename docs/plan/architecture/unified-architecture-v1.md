---
plan_id: unified-architecture-v1
title: "Ω-Architect 三层统一架构设计"
version: 0.1
date: 2026-06-12
author: omega-agent (deepseek-v4-pro)
status: draft
---

# Ω-Architect 三层统一架构

## 设计原则

1. **分层不分裂**：每一层是上一层的调用者，不是替代者
2. **模式不取代**：不同执行路径统一为"运行模式"，由路由层自动选择
3. **资源统一**：所有模式共用同一套 BudgetTracker + ConvergenceTracker + ModelRegistry
4. **入口统一**：一个 CLI 入口，模式选择透明

## 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI / API / SDK 统一入口                                        │
│  omega prove / omega bench / omega serve                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│  Layer 3: Orchestration（多 Agent 状态机 / Skills）              │
│  ─────────────────────────────────────────────                   │
│  适用：hard 定理、需分解的复杂目标                                │
│                                                                  │
│  Orchestrator (delegate_task)                                    │
│  ├── analyze_query     → 解析用户目标                            │
│  ├── generate_blueprint → 输出 DAG（非单定理子目标）              │
│  ├── prove_lemmas      → 用 Layer 2 并行证明 DAG 中引理          │
│  │   └── 每个引理自动路由到 Mode A/B                            │
│  ├── refine_blueprint  → 全局调整蓝图                             │
│  └── synthesize_result → 合并为最终定理                           │
│                                                                  │
│  8 原语（AGENTS.md Skills）:                                     │
│  apply_lemma / rewrite_goal / induction / case_split /           │
│  calc_chain / search_lemma / extract_proof / fallback_decompose  │
│                                                                  │
│  调用方式:                                                        │
│  orchestrator.delegate(goal, toolsets=["terminal","file"])       │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 调用
┌───────────────────────────▼─────────────────────────────────────┐
│  Layer 2: Proof Engine（证明引擎）                                │
│  ──────────────────────────────────                              │
│  三种运行模式，由 Router 自动选择:                                │
│                                                                  │
│  Mode A: 对话式 (Dialogue)          Mode B: 生成式 (Sampling)    │
│  ┌─────────────────────────┐       ┌─────────────────────────┐  │
│  │ inner_loop()            │       │ GoedelProver.run()      │  │
│  │ 适用: hard 复杂证明     │       │ 适用: easy 快速尝试     │  │
│  │ DeepSeek API 对话       │       │ Proposer → N 候选 → T2  │  │
│  │ compile → fix → repeat │       │ 自修正 → 首通过返回     │  │
│  └─────────────────────────┘       └─────────────────────────┘  │
│                                                                  │
│  Mode C: 混合 (Hybrid)                                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 先用 Mode B 并行采样 N 个候选                              │   │
│  │ 如果全部失败，用 Mode A 在最佳候选上做修正                  │   │
│  │ 如果仍失败，升级到 Layer 3 做定理分解                      │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  共享基础设施:                                                    │
│  ├── P0 Search Aggregator (leansearch + loogle + local BM25)     │
│  ├── P1 Error Classifier (规则 + NLP谱系 + LLM-Judge)            │
│  ├── CompileGate / T2 Verification                               │
│  └── ErrorMemory (Layer 2 NLP 谱系替代)                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 调用
┌───────────────────────────▼─────────────────────────────────────┐
│  Layer 1: Hardware & Resource（统一资源抽象）                     │
│  ─────────────────────────────────────────                        │
│                                                                  │
│  ┌──────────── Remote API ────────────┐  ┌──── Local GPU ────┐  │
│  │  DeepSeek API (flash/pro)          │  │  vLLM (Goedel)    │  │
│  │  自动: API key 检测 → 连接         │  │  Ollama (Qwen3)   │  │
│  │  回退: 网络超时 → cache 降级       │  │  Transformers     │  │
│  └────────────────────────────────────┘  └────────────────────┘  │
│                                                                  │
│  Cross-cutting:                                                   │
│  ├── BudgetTracker (统一: tokens/cost/time/attempts)             │
│  ├── ConvergenceTracker (修复后的: 非单调感知)                    │
│  └── ModelRegistry (所有模型定价/能力的单一真相来源)              │
└─────────────────────────────────────────────────────────────────┘
```

## 路由决策逻辑

模式选择器基于定理特征 + 可用资源 + 历史数据：

```python
class ModeRouter:
    """Select optimal execution mode for a theorem."""

    def select(self, theorem: TheoremSpec, context: RuntimeContext) -> ExecutionMode:
        scores = {
            ExecutionMode.SAMPLING: self._score_sampling(theorem, context),
            ExecutionMode.DIALOGUE: self._score_dialogue(theorem, context),
            ExecutionMode.HYBRID:   self._score_hybrid(theorem, context),
        }
        return max(scores, key=scores.get)

    def _score_sampling(self, theorem, ctx) -> float:
        """Mode B 得分: easy + 本地 GPU + 低预算"""
        score = 0.0
        if theorem.difficulty == "easy":       score += 3.0
        if ctx.local_gpu_available:            score += 2.0
        if ctx.budget_usd < 0.10:              score += 1.0
        if theorem.domain in ctx.sampling_ok:  score += 1.0
        return score

    def _score_dialogue(self, theorem, ctx) -> float:
        """Mode A 得分: hard + API 可用 + 高预算"""
        score = 0.0
        if theorem.difficulty in ("medium", "hard"): score += 3.0
        if ctx.api_available:                    score += 2.0
        if ctx.budget_usd >= 0.10:               score += 1.0
        return score

    def _score_hybrid(self, theorem, ctx) -> float:
        """Mode C 得分: 之前 Mode B 失败过"""
        score = 0.0
        if theorem.name in ctx.previous_failures: score += 4.0
        if theorem.difficulty == "hard":          score += 2.0
        if ctx.api_available and ctx.local_gpu:  score += 1.0
        return score
```

历史模式选择数据持久化到 `~/.omega/route_history.jsonl`，自动优化路由策略。

## 执行模式细节

### Mode A: 对话式 (Dialogue)

```python
# omega/engine/dialogue.py
def run_dialogue(theorem: str, config: EngineConfig) -> ProofResult:
    """Mode A: 对话式证明。当前的 inner_loop。"""
    return inner_loop(
        theorem_header=theorem,
        config=InnerLoopConfig(
            max_rounds=256,
            dead_loop_detection=False,  # P2 修复
            model=config.model,
            search_aggregator=SearchAggregator(),  # P0
            error_classifier=ThreeLayerClassifier(),  # P1
        ),
        budget=config.budget,
    )
```

### Mode B: 生成式 (Sampling)

```python
# omega/engine/sampling.py
def run_sampling(theorem: str, config: EngineConfig) -> ProofResult:
    """Mode B: 生成式证明。当前的 GoedelProver + Proposer。"""
    compile_fn = make_compile_callback(config)
    prover = GoedelProver(
        compile_fn=compile_fn,
        num_samples=config.num_samples,
        max_correction_rounds=config.max_correction_rounds,
        search_aggregator=SearchAggregator(),  # P0
    )
    return prover.run(theorem_header=theorem)
```

### Mode C: 混合 (Hybrid)

```python
# omega/engine/hybrid.py
def run_hybrid(theorem: str, config: EngineConfig) -> ProofResult:
    """Mode C: 混合。先采样再修正。"""
    # Phase 1: 并行采样 N 候选
    sampling_result = run_sampling(theorem, SamplingConfig(
        num_samples=config.num_samples,
        max_correction_rounds=1,  # 只需 1 轮修正
    ))
    if sampling_result.success:
        return sampling_result

    # Phase 2: 在最佳候选上做对话式修正
    best_code = sampling_result.best_attempt
    dialogue_result = run_dialogue(
        theorem=f"{theorem}\n{best_code}",  # 注入最佳候选
        config=DialogueConfig(max_rounds=config.dialogue_rounds),
    )
    return dialogue_result
```

### Layer 3: 多 Agent 分解 (Orchestration)

```python
# omega/engine/orchestration.py
def run_orchestration(goal: str, config: EngineConfig) -> ProofResult:
    """Layer 3: 多 Agent 状态机。AGENTS.md 架构 B 的实现。"""
    orchestrator = Orchestrator(
        toolsets=["terminal", "file", "web"],
        max_iterations=5,
    )

    # 8 原语（Skills）
    primitives = {
        "apply_lemma": ApplyLemmaSkill(),
        "rewrite_goal": RewriteGoalSkill(),
        "induction": InductionSkill(),
        "case_split": CaseSplitSkill(),
        "calc_chain": CalcChainSkill(),
        "search_lemma": SearchLemmaSkill(search_aggregator=SearchAggregator()),
        "extract_proof": ExtractProofSkill(),
        "fallback_decompose": FallbackDecomposeSkill(),
    }

    return orchestrator.run(
        entry_state="analyze_query",
        goal=goal,
        primitives=primitives,
        proof_engine=run_hybrid,  # 子目标走 Mode C
    )
```

## 预算统一模型

所有模式共用同一 BudgetTracker，但不同模式对不同维度敏感：

| 维度 | Mode A (对话) | Mode B (采样) | Mode C (混合) | Layer 3 (编排) |
|:----|:-------------:|:-------------:|:-------------:|:--------------:|
| **tokens** | 高（对话累积） | 中（批量生成） | 中+高 | 高（多层开销） |
| **cost USD** | 中（API） | 低（本地） | 中 | 高 |
| **time** | 线性增长 | 并行恒定 | 相加快 | 多轮叠加 |
| **attempts** | 1/轮 | N/轮 | N+轮 | 子目标×轮 |

预算分配策略：

```python
def allocate_budget(theorem: TheoremSpec, mode: ExecutionMode) -> BudgetConfig:
    """按模式分配预算资源。"""
    base = BudgetConfig(max_cost_usd=0.50, max_time_s=300, max_attempts=50)

    if mode == ExecutionMode.SAMPLING:
        return base._replace(
            max_cost_usd=0.10,      # 本地推理免费
            max_time_s=120,          # 2 分钟
            max_attempts=200,        # N=20 候选 × 10 修正
        )
    elif mode == ExecutionMode.DIALOGUE:
        return base._replace(
            max_cost_usd=0.30,       # API 费用
            max_time_s=300,           # 5 分钟
            max_attempts=50,          # 50 轮对话
        )
    elif mode == ExecutionMode.HYBRID:
        return base._replace(
            max_cost_usd=0.50,       # 混合模式上限
            max_time_s=600,           # 10 分钟
            max_attempts=250,         # 200 采样 + 50 对话
        )
    elif mode == ExecutionMode.ORCHESTRATION:
        return base._replace(
            max_cost_usd=2.00,       # 多 Agent 开销
            max_time_s=3600,          # 1 小时
            max_attempts=1000,
        )
```

## CLI 统一入口

```bash
# 自动模式（Router 自动选择）
omega prove "theorem t : 1+1=2 :="

# 指定模式
omega prove "..." --mode dialogue
omega prove "..." --mode sampling --num-samples 8
omega prove "..." --mode hybrid
omega prove "..." --mode orchestrate  # 多 Agent 状态机

# 全局资源控制
omega prove "..." --budget-usd 0.50 --time-min 10
omega prove "..." --model deepseek-v4-flash
omega prove "..." --local-gpu  # 强制使用本地 GPU
```

## AGENTS.md 重构方案

当前 713 行 → 重构为：

```
AGENTS.md (约 300 行)
├── # Ω-Architect 架构总览（三层）
│   ├── 设计原则
│   └── 架构图（上述 Layer 1-3）
│
├── ## Layer 1: 硬件与资源抽象
│   ├── GPU Layer（当前 omega/gpu_layer/ → 简化）
│   ├── ModelRegistry（当前稳定，保留）
│   ├── BudgetTracker（修复 tool_call 去重）
│   └── Cache Strategy（P0 JSONL cache）
│
├── ## Layer 2: 证明引擎
│   ├── Mode A: Dialogue（inner_loop → omega/engine/dialogue.py）
│   ├── Mode B: Sampling（GoedelProver → omega/engine/sampling.py）
│   ├── Mode C: Hybrid（omega/engine/hybrid.py）
│   ├── Mode Router（omega/engine/router.py）
│   └── 共享组件（P0 Search, P1 Classifier, CompileGate）
│
├── ## Layer 3: 多 Agent 编排
│   ├── 状态机总览
│   ├── 8 原语（Skills）
│   └── Orchestrator（omega/engine/orchestration.py）
│
├── ## CLI 用户指南
│   ├── 快速开始
│   ├── 命令参考
│   └── 配置
│
└── ## 实验记录
    └── 指向 docs/plan/experiments/ 的链接
```

## 验证计划

| 验证项 | 方法 | 标准 |
|:-------|:-----|:-----|
| Mode Router 正确性 | 跑已知定理，验证模式选择符合预期 | 10 题路由准确率 >90% |
| 多模式兼容性 | 同一定理用三种模式各跑一次 | 结果一致 (compile pass/fail) |
| 预算统一性 | 各模式 reach budget limit 时正确停止 | budget_exhausted 不误报 |
| 入口统一性 | CLI 三种调用方式均正常工作 | ✅ |
| Mode C > Mode A/B | hard 题用 Mode C 比 Mode A/B 更好 | 通过率 ≥ 当前 50% |
| AGENTS.md 完整性 | 新 AGENTS.md 可独立指导开发者 | 所有命令和 API 可执行 |

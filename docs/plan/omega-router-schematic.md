---
title: "Ω-Architect 三层路由架构详解 & 实施路线"
date: 2026-06-12
author: Hermes Agent
project: omega-architect
target: MiniF2F 244题 → 95%+ pass rate
model: deepseek-v4-flash
---

## 1. 架构全景：三层独立路由系统

```
                    ┌──────────────────────────────────┐
                    │    CLI入口: omega prove/bench     │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────▼───────────────────┐
                    │ Layer 1: ModelRouter (模型选择)    │
                    │  定理复杂度 → 选 DeepSeek/Goedel/  │
                    │  Ollama → 预算感知降级 → ML覆盖    │
                    │  omega/resource/model_router.py   │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────▼───────────────────┐
                    │ Layer 2: ModeRouter (策略选择)     │
                    │  难度估计 → DFS/Beam/Hybrid 计分   │
                    │  → 最高分策略 → 执行轨迹           │
                    │  omega/engine/router.py           │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────▼───────────────────┐
                    │ P1 Classifier (横切:错误分类)       │
                    │  L1规则(<1ms)→L2 NLP谱系(4算法)    │
                    │  → L3 LLM Judge(缓存)             │
                    │  omega/classifier/                │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────▼───────────────────┐
                    │ InnerLoop (微循环引擎)              │
                    │  LLM生成→编译→分类→反馈→调整→收敛   │
                    │  omega/loop/inner.py (1302行)     │
                    └──────────────────────────────────┘
```

### 每一层的决策粒度

| 层 | 问题 | 输入 | 输出 |
|----|------|------|------|
| ModelRouter | "选哪个模型？" | theorem_header + budget_snapshot | "deepseek-flash" / "goedel-v2-8b" |
| ModeRouter | "选哪种策略？" | theorem + resource + history | DFS / Beam / Hybrid |
| P1 Classifier | "这是什么错误？" | error_msg | "TYPE_MISMATCH" / "UNKNOWN_IDENT" |
| InnerLoop | "下一步代码是什么？" | 当前代码 + 编译结果 + 错误类 | Lean 4 代码 |

## 2. NLP 算法谱系（共享体系）

**关键洞察**：同样的 4 算法被复用于**两个完全不同**的用例：

| 算法 | 用途1: 难度估计 | 用途2: 错误分类 | 权重 |
|------|----------------|----------------|------|
| TokenJaccard | 定理头部 token vs _DIFFICULTY_CORPUS | error_msg vs _ERROR_TEMPLATES | 1.2 |
| EditDistance | Levenshtein 编辑距离 | 相同实现 | 0.8 |
| BM25Retrieval | TF-IDF 稀疏检索（最可靠） | 相同实现 + ErrorMemory 增强 | 1.5 |
| EmbeddingSimilarity | TF-IDF 词袋加权兜底 | 相同实现 | 0.7 |

**节省**: ~430 行代码 0 改即用，`DifficultySpectrum` 直接引用 `layer2_nlp.py` 的类。

### 难度语料库（MiniF2F 实验标注）

| 难度 | 典型定理 | 通过率（对话模式） |
|------|---------|------------------|
| EASY | `theorem mathd_algebra_478 : (2:ℝ)^3 = 8` | ~100% |
| MEDIUM | `theorem add_comm (a b:ℕ): a+b=b+a` | ~50% |
| HARD | `theorem imo_1959_p1` | ~0% (需 Hybrid) |

## 3. ModelRouter (Layer 1) — 模型选择

### 决策流程
```
theorem_header → _estimate_complexity()
  → analyze_theorem_pattern() (模式识别)
  → compute_theorem_flags() (运行时标记)
  → apply_postprocess() (安全/覆盖/升级/粘性)
  → 输出: "simple" / "medium" / "hard"

→ ML override: LightGBM 置信>0.7 则覆盖规则
→ _rank(): 按 tier 匹配 + cost 排序
→ _apply_plan_snapshot(): 预算紧张降级
→ 最终: model_id (e.g. "deepseek/deepseek-v4-flash")
```

### 可用模型表

| Model ID | Preferred Tiers | Cost | 可用性 |
|----------|----------------|------|--------|
| `goedel/goedel-v2-8b` | simple, medium | $0 (local) | localhost:8001 |
| `deepseek/deepseek-v4-flash` | medium, hard | ~$0.0005/call | DEEPSEEK_API_KEY |
| `local/qwen3-coder:30b` | simple | $0 (local) | localhost:11434 |

### 历史学习
- JSONL 持久化 → `~/.omega/model_router_history.json`
- 按 model_id × complexity 统计 success_rate + avg_elapsed_s
- 对未来路由可做贝叶斯调整

## 4. ModeRouter (Layer 2) — 策略选择

### 3 模式的技术对比

| 属性 | DFS (Dialogue) | Beam (Sampling) | Hybrid (v2) |
|------|---------------|----------------|-------------|
| **算法** | 深度优先搜索 | 束搜索 (K=6-8) | 对话优先 → 采样 → 再对话 |
| **底层实现** | `inner_loop()` | `go_prover.py` + `_parallel_compile()` | `run_hybrid_v2()` |
| **编译模式** | 串行 | 并行 (ThreadPool) | 混合 |
| **预算模式** | 线性 | K倍 | ≤2×Dialogue + 1×Sampling |
| **适合** | easy/medium | 多方法探索 | hard/stuck |
| **轮次上限** | 50 | K×(1+correction) | 20+8×3+30 |
| **stuck 检测** | ConvergenceTracker | N/A | _is_stuck() |

### 计分函数（当前权重，数据驱动可调）

```python
_score_dfs(difficulty, ctx):
  base=5.0, easy+3, medium+1, API+2, failed-2/fail, stuck-3

_score_beam(difficulty, ctx):
  base=3.0, local GPU+3, easy+local+3, budget<0.10+2

_score_hybrid(difficulty, ctx):
  base=2.0, hard+3, fails≥2+4.5, stuck+5, budget<0.30-3
```

**实验验证**: Hybrid v1 (先采样后对话) 通过率不提升，耗时 2.1x → v2 翻转顺序

## 5. "黑白棋子循环" — 微循环架构详解

### 每个棋子的模块归属

| 棋子 | 模块 | 行数 | 职责 | 旧版来源 |
|------|------|------|------|---------|
| **① 编辑** | `inner.py` L653-872 | ~220 | LLM API 调用 + 代码提取 + beam 多候选 | 全新（旧版在 `inner.py` 但更简陋） |
| **② 编译** | `compile_gate.py` | ~216 | SHA256 缓存 + 13类错误分类 | 源自 `t2_real.py` 但重写 |
| **③ 分类** | `classifier/` 3层 ❄️ | ~1200 | L1(规则)→L2(NLP 4算法)→L3(LLM 缓存) | 全新 |
| **④ 反馈** | `inner.py` L1037+ | ~260 | _ERROR_STRATEGY_HINTS + LSP code_actions | 全新（旧版只返回原始错误） |
| **⑤ 调整** | `inner.py` 子引理 + outer.py | ~180 | 自适应策略切换 + sub_lemma_prompt | 全新 |
| **⑥ 收敛检测** | `tracker.py` | ~266 | 窗口滑动 stuck/diverging 信号 | 演化自旧版 ConvergenceTracker |
| **⑦ 错误记忆** | `error_memory.py` | ~394 | JSONL 持久化 + 4级错误签名 | 全新 |

### 单次循环迭代步骤

```
1. LLM API call → response (含代码或 tool_calls)
2. 有 tool_calls? → MCP leansearch/loogle/multi_attempt → 继续循环
3. 无 tool_calls → 提取代码 _extract_lean_code()
4. 多候选? → _parallel_compile() 并行编译
5. 单候选? → CompileGate 单线程编译
6. 编译成功? → VerifierAgent 二次验证
   ├── verifier 通过 → 返回 result.success=True
   └── verifier 拒绝 → feedback 注入, 继续
7. 编译失败? → P1 三层分类器分类
8. ConvergenceTracker 记录 epoch
9. 检测 stuck/diverging → 提前终止
10. BudgetTracker 检测 → 超预算终止
11. 循环
```

## 6. 模块化复用统计

| 复用模式 | 涉及文件 | 节省行 | 说明 |
|---------|---------|--------|------|
| NLP 谱系 | layer2_nlp ↔ difficulty_spectrum | ~430 | 0改即用 |
| BudgetTracker | budget.py → DFS/Beam/Hybrid | ~327 | 共享实例 |
| 编译缓存 | compile_gate.py → 所有模式 | ~216 | SHA256 去重 |
| ConvergenceTracker | tracker.py → DFS/Hybrid | ~266 | stuck 提前终止 |
| ErrorMemory | error_memory.py → inner.py | ~394 | 跨定理 |
| MCP Client | mcp_client.py + mcp_sync.py | ~340 | 统一 LSP 接口 |

**总计 ~2,100 行共享代码**，若独立写 4 份约 ~8,400 行。

## 7. 当前状态

### ✅ 已完成
- Phase 0: 基座 (DeepSeek SDK, CompileGate, MCP Client)
- Phase 1: 搜索优化 (强制代码, 子引理, MCP 优雅处理)
- Phase 2: 自适应策略 + 缓存 (DialogueCache, ErrorMemory)
- Phase 2.5: 三层路由 (ModelRouter, ModeRouter, P1 Classifier)
- 架构文档 (AGENTS.md, DEVELOPMENT_ROADMAP.md, 本 plan)
- 测试: 159 核心测试全通过

### 📋 剩余实施任务（按优先级）

#### P0: 测试覆盖 + 修复
- [x] `test_inference_under_100us` 阈值修复（WSL 1265µs → 2000µs）
- [ ] 为 ModeRouter 添加完整单元测试
- [ ] 为 Hybrid v2 添加集成测试
- [ ] CLI 入口测试 (omega prove)

#### P1: CLI 统一入口
- [ ] `omega prove theorem...` — 走 ModeRouter → 执行
- [ ] `omega bench MiniF2F` — 批量跑测试集
- [ ] `omega status` — ModelRouter 健康报告
- [ ] `omega route` — 路由决策预览（debug 模式）

#### P2: Layer 3 Orchestration
- [ ] 8 原语定义 (apply_lemma / rewrite_goal / induction / case_split / calc_chain / search_lemma / extract_proof / fallback_decompose)
- [ ] Orchestrator 状态机 (analyze → blueprint → prove_lemmas → refine → synthesize)
- [ ] 子智能体编排 (delegate_task)

#### P3: 增量改进
- [ ] MLRoute 训练管线 (`feature_extraction → label_generation → train_lgbm`)
- [ ] BudgetPlan 升级 (运筹优化)
- [ ] MiniF2F 244 题全量实验

## 8. 架构术语对照（复盘用）

| 旧名称 | 新名称 | 原因 |
|--------|--------|------|
| Inner Loop | InnerLoop | 去掉空格统一命名 |
| Dialogue Mode | DFS Strategy | 学术用语（深度优先搜索） |
| Sampling Mode | Beam Strategy | 学术用语（束搜索） |
| Hybrid Mode v1 | 废弃 | 实验证明无效 |
| 难度计分 | DifficultySpectrum | NLP 谱系替换关键词 |
| 错误分类 | ThreeLayerClassifier | 三层级联替换单一 regex |
| 收敛跟踪 | ConvergenceTracker | 更好命名 |

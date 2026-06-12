---
title: "Ω-Architect 全量执行计划 v2"
subtitle: "Engineering Track + Learning Track + 依赖关系 + 里程碑"
date: 2026-06-12
target: "MiniF2F 244题 95%+ pass@1"
model: "deepseek-v4-flash (基础) → 多域 LoRA (GRPO) → DeepSeek-V4-Pro (最终)"
budget: "$630 (学习管线) + $200 (工程管线) + $20 (LUFFY 论文复现测试) = ~$850 总量"
---

## 架构总览：双轨并行（LUFFY 升级）

```
时间 →   W1        W2        W3        W4        W5        W6
        ┌─────────────────────────────────────────────────────────┐
工程轨  │ P0✅  │  P1 CLI  │  P2 Layer3  │──P3 MiniF2F─244──│
        │ 测试  │          │  Orchestr.  │   全量实验       │
        └───────┴──────────┴─────────────┴──────────────────┘
             │           │            │               │
             ▼           ▼            ▼               ▼
        ┌─────────────────────────────────────────────────────────┐
学习轨  │  L0基座  │  L1 GRPO   │  L2 LUFFY   │  L3多域  │L4蒸馏│L5 Pro│
        │  Policy  │  纯on-pol  │  Mixed-Pol  │  专精    │      │ 升级  │
        │          │  icy基线   │  icy+Shaping│          │      │      │
        └─────────────────────────────────────────────────────────┘
                                  ↑
                            关键升级点
                纯 GRPO → LUFFY Mixed-Policy GRPO
                (on-policy only → on + off)
```

### LUFFY 核心技术与我们架构的直接映射

| LUFFY | Omega 对应模块 | 插入位置 |
|-------|---------------|---------|
| **Mixed-Policy GRPO**<br>7 on-policy + 1 off-policy 轨迹同组归一化 | `omega/learn/rl/` | 替换纯 GRPO 训练器 |
| **Off-policy 专家轨迹**<br>DeepSeek-R1 生成 + Math-Verify 验证 | `omega/loop/dialogue_cache.py` | DialogueCache 本身就是专家轨迹池 |
| **Policy Shaping**<br>f(x)=x/(x+γ) 重加权低概率 token 梯度 | `omega/learn/rl/grpo_trainer.py` | 添加到 off-policy importance ratio |
| **On-policy rollouts**<br>当前策略采样 7 次 | `omega/learn/policy/llm_policy.py` | `Policy.rollout()` → 调用 `inner_loop()` |
| **Verifiable Reward**<br>只奖励正确性，无 format/length 奖励 | `omega/learn/reward/reward_model.py` | Lean 编译结果就是完美验证器 |
| **KL=0 无需 KL 惩罚**<br>since reward is verifiable | `omega/learn/rl/grpo_trainer.py` | β=0，简化工实现 |

```
时间 →   W1        W2        W3        W4        W5        W6
        ┌─────────────────────────────────────────────────────────┐
工程轨  │ P0✅  │  P1 CLI  │  P2 Layer3  │──P3 MiniF2F─244──│
        │ 测试  │          │  Orchestr.  │   全量实验       │
        └───────┴──────────┴─────────────┴──────────────────┘
             │           │            │               │
             ▼           ▼            ▼               ▼
        ┌─────────────────────────────────────────────────────────┐
学习轨  │  L0基座  │  L1 GRPO   │  L2 Reana-  │  L3多域  │L4自蒸馏│L5 Pro│
        │  Policy  │  单域实验  │  lyze校正   │  专精    │        │ 升级  │
        └─────────────────────────────────────────────────────────┘
            交叉点1       交叉点2        交叉点3         最终合并
         Policy 包装     GRPO 训练      Reanalyze      全量实验
         现有行为       数据来自CLI    旧轨迹再利用
```

---

## Phase 0: 已完成（W1）

### 工程轨 ✅

| 模块 | 状态 | 行数 | 说明 |
|------|------|------|------|
| DeepSeek SDK 集成 | ✅ | 338行 | tool_calls + thinking mode |
| CompileGate | ✅ | 216行 | SHA256 缓存 + 13类错误 |
| 三层错误分类器 | ✅ | ~1200行 | L1规则(1ms) → L2 NLP 4算法 → L3 LLM |
| ConvergenceTracker | ✅ | 266行 | 窗口 stuck/diverging 检测 |
| BudgetTracker | ✅ | 327行 | tokens/cost/time/attempts 四维 |
| MCP Client | ✅ | 340行 | lean-lsp-mcp 持久连接 |
| InnerLoop v0.3 | ✅ | 1302行 | 搜索限制 + 自适应策略 + dialogue cache |
| Plan Layer | ✅ | ~1100行 | BudgetPlan + GPUPlan + PathPlan + ProofAllocator |
| ModelRouter v2 | ✅ | 741行 | Plan-aware 模型选择 + LightGBM 覆盖 |
| ModeRouter | ✅ | 386行 | DFS/Beam/Hybrid 策略选择 |
| DifficultySpectrum | ✅ | 401行 | 4算法难度估计 |
| Trajectory 抽象 | ✅ | 412行 | ProofState/Action/Trajectory/SearchStrategy |
| Hybrid v2 | ✅ | 409行 | 对话优先→采样→再对话 |
| DialogueCache | ✅ | ~100行 | JSONL 成功对话缓存 |
| ErrorMemory | ✅ | 394行 | 4级错误签名 + 跨定理复用 |
| 测试 | ✅ | 300 tests | 全回归通过 |

### 学习轨 ✅

| 模块 | 状态 | 说明 |
|------|------|------|
| 当前策略（隐式） | ✅ | LLM + ModeRouter + Classifier 三处分布 |
| DialogueCache 轨迹 | ✅ | 成功轨迹已落盘（作为 replay buffer 种子） |
| LightGBM 训练管线 | ✅ | feature_extraction → label_generation → train_lgbm |
| research 管线 | ✅ | KnowledgeProver + 5 source 搜索 |

---

## Phase 1: 双轨启动（W2，当前）

### P1: CLI 统一入口（工程轨，5 天）

**目标**：`omega prove` 替代 `python -c "from omega.loop.inner import inner_loop..."`

| 子任务 | 技术路线 | 产出 |
|--------|---------|------|
| 1.1 `omega prove "theorem..."` | cli/__init__.py 已有骨架 | 完整 prove 命令 |
| 1.2 `omega prove theorem.lean` | 文件输入支持 | 批量定理列表 |
| 1.3 `omega prove --mode dfs|beam|hybrid` | ModeRouter 集成 | 模式选择 |
| 1.4 `omega bench` | benchmark/suite.py 直接调用 | MiniF2F 批量跑 |
| 1.5 `omega route` debug | ModeRouter 预览 | 决策理由 + 得分 |
| 1.6 `omega status` | ModelRouter 健康报告 | 各模型可用性 |

**工程细节**：

```
omega prove "theorem t : 1 + 1 = 2 := by"
  → cli/__init__.py 解析 theorem_header
  → ModeRouter.route() → 选 DFS/Beam/Hybrid
  → strategy.run(theorem)
  → 输出: ✅ / ❌, rounds, cost, proof_code

omega prove theorem.lean --mode dfs
  → 从 .lean 文件提取定理
  → 批量执行（逐个或并行）
  → 输出: JSONL 结果文件

omega route "theorem t : ..."
  → ModeRouter.route() 总览
  → 打印: 难度, 域名, 3种策略得分, 选择理由

omega bench --file dataset/minif2f_10.jsonl
  → 加载测试集
  → 批量执行 → 统计 pass@k, avg rounds, avg cost
```

**测试**：

```
tests/test_cli.py:
  test_prove_easy     → 真实调用 CLIRunner("prove 1+1=2")
  test_prove_file     → 从 .lean 文件读取定理
  test_bench_empty    → MiniF2F 1 题（验证跑通）
  test_route_preview  → ModeRouter 决策输出格式
  test_status_health  → ModelRouter 健康报告
```

---

### L0: Policy 抽象（学习轨，2 天，与 P1 并行）

**目标**：`Policy` 接口包装现有策略行为，不改变任何运行逻辑

| 子任务 | 技术路线 | 产出 |
|--------|---------|------|
| 0.1 `Policy` ABC | `omega/learn/policy/base.py` | `act(s) → a`, `update(trajs, rewards)` |
| 0.2 `LLMPolicy` | 包装 `inner_loop()` 为 Policy 调用 | 与当前 100% 一致 |
| 0.3 `RouterPolicy` | 包装 ModeRouter 为 Policy | 同样可训练接口 |
| 0.4 Policy context | `PolicyContext(debug, budget, mode)` | 调试支持 |
| 0.5 回归测试 | 验证 Policy 行为 == 原行为 | 全部通过 |

```python
# omega/learn/policy/base.py — 核心抽象

class Policy(ABC):
    """π(a|s) — 给定证明状态生成下一动作的策略。"""

    @abstractmethod
    def act(self, state: ProofState, ctx: PolicyContext) -> ProofAction:
        """从当前状态选择下一个动作（tactic 或 search）。"""
        ...

    def batch_act(self, states: list[ProofState]) -> list[ProofAction]:
        """批量推理（并行加速，用于 beam search）。"""
        return [self.act(s, PolicyContext()) for s in states]

    def update(self, trajectories: list[Trajectory], rewards: list[float]):
        """从经验更新策略参数（GRPO/PPO 入口）。"""
        ...


class LLMPolicy(Policy):
    """通过 LLM API 调用的策略 —— 当前模式的无损包装。

    与 inner_loop 的行为完全一致：
    - LLM 生成 Lean 代码 → 编译 → 错误反馈 → 再生成
    - 搜索限制、自适应策略、MCP 工具 全部保留
    """
    ...


class RouterPolicy(Policy):
    """通过 ModeRouter 选择搜索策略的策略。

    将 ModeRouter 的 3 策略决策包装为 Policy.act() 调用。
    选 stratey → strategy.run(theorem) → 返回 trajectory。
    """
    ...
```

**测试**：

```
tests/test_learn_policy.py:
  test_llm_policy_acts   → LLMPolicy.act() 返回 ProofAction
  test_router_policy_acts → RouterPolicy.act() 返回 ProofAction
  test_policy_consistency → 与当前 inner_loop 输出一致
```

---

## Phase 2: 工程加固 + 学习训练（W3）

### P2: Layer 3 Orchestration（工程轨，5 天）

**核心**：复杂定理分解为子目标，用多 agent 状态机并行证明。

```
Orchestrator (状态机):
    analyze_query → generate_blueprint → prove_subgoals (并行)
    → refine_blueprint → verify → synthesize_result
```

**8 原语实现**：

| 原语 | 接口 | 实现 |
|------|------|------|
| apply_lemma | str → ProofState | `ApplyLemmaPrimitive` |
| rewrite_goal | Pattern → ProofState | `RewriteGoalPrimitive` |
| induction | On Var → ProofState | `InductionPrimitive` |
| case_split | On Hyp → ProofState | `CaseSplitPrimitive` |
| calc_chain | Steps → ProofState | `CalcChainPrimitive` |
| search_lemma | Query → list[Lemma] | `SearchLemmaPrimitive` |
| extract_proof | Trajectory → code | `ExtractProofPrimitive` |
| fallback_decompose | Theorem → Subgoals | `FallbackDecomposePrimitive` |

**测试**：

```
tests/test_orchestrator.py (25+ tests):
  test_apply_lemma_primitive
  test_orchestrator_single_lemma
  test_orchestrator_multi_subgoal
  test_orchestrator_fallback_on_stuck
```

---

### L1: LUFFY Mixed-Policy GRPO 单域实验（学习轨，5 天，与 P2 并行）

**目标**：在代数域上跑通 LUFFY Mixed-Policy GRPO + Policy Shaping

| 子任务 | 产出 | 预计预算 |
|--------|------|---------|
| 1.1 Mixed-Policy GRPO loss + grouped reward | 7+1 混合组归一化 | $0 |
| 1.2 Policy Shaping f(x)=x/(x+γ) | 防止熵坍缩 | $0 |
| 1.3 Off-policy trace 采样（从 DialogueCache） | expert 轨迹注入 | $0 |
| 1.4 代数域 200 题 LUFFY 训练（10 轮） | pass@k 提升 10-20% | $50 |
| 1.5 Ladder L0 vs L1 对比 | 效率曲线 | $10 |

**核心差异 vs 纯 GRPO**（原计划升级点）：

| 维度 | 纯 GRPO（原计划） | LUFFY | 收益 |
|------|-------------|-------|------|
| 采样方式 | 8× on-policy | 7 on + 1 off | 专家轨迹拉高 baseline |
| 训练弱模型 | 不适用 | ✅ 已验证（LLaMA3.1-8B） | 我们基座不是推理模型 |
| KL 惩罚 | 需要（自适应熵控制） | β=0（无需） | 简化实现 |
| Off-policy 校正 | Reanalyze（复杂） | Importance Sampling + Shaping | 更简单，已验证 |
| 收敛理论 | 无 | ✅ Theorem 1: O(1/√K) | 有理论保证 |
| 论文结果 | 无 | +6.4 avg on 6 math benchmarks | 直接相关

**GRPO 训练循环**：

```
for epoch in range(N):
    # ── 从 CLI 命令行获取定理 ──
    theorems = load_domain_dataset("algebra", n=200)
    
    # ── 每定理 LUFFY 混合采样（7 on-policy + 1 off-policy）──
    trajectories = []
    for theorem in theorems:
        for _ in range(7):
            traj = policy.rollout(theorem)   # on-policy: 用 inner_loop
            trajectories.append(traj)
        off_traj = sample_expert_trace(theorem)  # off-policy: 从 DialogueCache / R1
        trajectories.append(off_traj)
    
    # ── 组内归一化奖励（含 expert 轨迹）──
    rewards = grouped_reward(trajectories, group_size=8)
    # expert 轨迹奖励通常更高 → 拉高 group baseline
    
    # ── LUFFY Mixed-Policy 更新 ──
    loss = luffy_mixed_loss(trajectories, rewards,
                            clip_epsilon=0.2,
                            shaping_gamma=0.1)   # Policy Shaping!
    # β=0 (无需 KL 惩罚，verifiable reward 场景已验证)
    loss.backward()
    optimizer.step()
    # 无需自适应熵控制 — Policy Shaping 自动防止熵坍缩
```

---

## Phase 3: 学习升级 + 工程收尾（W4）

### L2: LUFFY + Reanalyze 补充（学习轨，3 天）

**注意**：LUFFY 的 Mixed-Policy 已自带 off-policy 校正（Importance Sampling + Shaping），Reanalyze 降级为**可选补充**，仅用于进一步提升 DialogueCache 积累的旧轨迹利用率。

| 子任务 | 产出 | 预计预算 |
|--------|------|---------|
| 2.1 Reanalyze 轻量化实现 | 仅对精选高奖励旧轨迹做重分析 | $0 |
| 2.2 LUFFY + Reanalyze 联合实验 | 对比 LUFFY alone vs LUFFY+Reana | $20 |
| 2.3 结论：确定是否值得保留 Reanalyze | 决策报告 | $10 |

**预期结论**：大概率不需要 Reanalyze — LUFFY 的 off-policy 校正已经足够。

### P3a: MiniF2F 244 题基线（工程轨，3 天）

| 子任务 | 产出 |
|--------|------|
| 3a.1 全量基线跑通 | 244 题 pass@1 baseline |
| 3a.2 分类结果 | 按域名/难度/通过率报告 |
| 3a.3 失败模式分析 | clustered error types |

---

## Phase 4: 多域专精 + 全量实验（W5）

### L3: 五域专精（学习轨，5 天）

| 域 | 训练集 | 题数 | 成本 |
|----|--------|------|------|
| Algebra | mathd_algebra + ring/field | 300 | $40 |
| Number Theory | mathd_numbertheory + prime/dvd | 200 | $30 |
| Combinatorics | mathd_combinatorics + choose | 150 | $25 |
| Analysis | Real 定理 + inequality | 100 | $20 |
| IMO | IMO 1959-2025 可形式化 | 150 | $35 |

### L4: 自蒸馏 + Consolidation（学习轨，3 天）

| 子任务 | 产出 | 成本 |
|--------|------|------|
| 5 域 LoRA adapter 合并 | 单一 base + domain router | $0 |
| Consolidation SFT | 合流训练 | $50 |
| 自蒸馏 O(1M) | 高 dropout 防退化 | $30 |
| Ladder L3-L4 对比 | 效率曲线 | $20 |

### P3b: MiniF2F 244 题正式实验（工程轨，2 天）

| 子任务 | 产出 | 成本 |
|--------|------|------|
| 训练后模型全量跑 | pass@1, pass@3, pass@10 | $100 |
| 域路由器集成 | 推理时自动选 adapter | $0 |
| 最终报告 | L0-L5 完整对比 | $0 |

---

## Phase 5: Pro 基座升级（W6）

### L5: DeepSeek-V4-Pro 迁移（学习轨，3 天）

| 子任务 | 产出 | 成本 |
|--------|------|------|
| Pro 基座 GRPO 训练 | 保留 LoRA adapter | $100 |
| 全量 244 题 epoch | 95%+ pass@1 | $200 |
| 最终消融报告 | 5条 scaling curve | $0 |

---

## 依赖关系图

```
W1 [已完成]
  ├── 工程: Ph0 基座 ✅
  └── 学习: 隐式策略 + DialogueCache ✅

W2 [当前 — 并行]
  ├── 工程 → P1 CLI (5天)
  │     └── 依赖: cli/__init__.py 骨架(已有)
  └── 学习 → L0 Policy 抽象 (2天)
        └── 依赖: trajectory.py (已有)

W3 [并行]
  ├── 工程 → P2 Orchestration (5天)
  │     └── 依赖: P1 CLI (用于测试 orchestrator 命令)
  └── 学习 → L1 GRPO 单域 (5天)
        └── 依赖: L0 Policy (用于 rollout)

W4 [并行]
  ├── 工程 → P3a MiniF2F 基线 (3天)
  │     └── 依赖: P1 CLI (用于批量跑 bench)
  └── 学习 → L2 Reanalyze (3天)
        └── 依赖: L1 GRPO (需要有数据校正)

W5 [会合]
  ├── 工程 → P3b MiniF2F 正式实验 (2天)
  │     └── 依赖: P3a基线 + L3-L4训练模型
  └── 学习 → L3 多域 + L4 自蒸馏 (5天)
        └── 依赖: L1 GRPO + L2 Reanalyze

W6 [收尾]
  └── 学习 → L5 Pro升级 (3天)
        └── 依赖: L3-L4 (adapter 可迁移到 Pro)
```

---

## 预算明细

| Phase | 子项 | 成本 ($) | 累计 ($) |
|-------|------|---------|---------|
| W1 | 已完成 | 0 | 0 |
| W2 | P1 CLI 测试 (API 调用) | 5 | 5 |
| W2 | L0 Policy 回归测试 | 5 | 10 |
| W3 | P2 Orchestration 实验 | 20 | 30 |
| W3 | L1 GRPO 代数域 (10 轮 × 200 题 × 8 组) | 50 | 80 |
| W4 | P3a MiniF2F 基线 (244 题 × 1 次) | 50 | 130 |
| W4 | L2 Reanalyze 实验 | 30 | 160 |
| W5 | L3 五域 (Algebra/NT/Combin/Analysis/IMO) | 150 | 310 |
| W5 | L4 自蒸馏 + Consolidation | 80 | 390 |
| W5 | P3b MiniF2F 正式 (训练后 × 3 次) | 100 | 490 |
| W6 | L5 Pro 基座迁移 + 全量 | 300 | 790 |
| | **预留余量 (15%)** | 120 | **~910** |

---

## 文件结构（完成后）

```
omega/
├── learn/                         #   全新 Policy 学习层
│   ├── __init__.py
│   ├── policy/
│   │   ├── base.py                # Policy ABC
│   │   ├── llm_policy.py          # π_θ: LLM + LoRA
│   │   └── router_policy.py       # ModeRouter 包装
│   ├── reward/
│   │   ├── reward_model.py        # 奖励函数
│   │   └── process_reward.py      # 逐步奖励
│   ├── replay/
│   │   └── trajectory_buffer.py   # 优先级采样
│   ├── rl/
│   │   ├── grpo_trainer.py        # GRPO 训练器
│   │   ├── ppo_trainer.py         # PPO（可选）
│   │   └── reanalyze.py           # MuZero 重分析
│   ├── consolidation/
│   │   ├── sft_mixer.py           # 多域 SFT 合并
│   │   └── self_distill.py        # 自蒸馏
│   └── experiments/
│       └── hill_climbing.py       # 爬坡调度器
├── cli/                           # ✅ CLI 统一入口
│   └── __init__.py                # prove / bench / route / status
├── engine/
│   ├── router.py                  # ✅ ModeRouter
│   ├── trajectory.py              # ✅ ProofState/Action/Trajectory
│   ├── hybrid.py                  # ✅ Hybrid v2
│   └── orchestrator.py            #    Layer 3 (待实现)
├── loop/
│   ├── inner.py                   # ✅ InnerLoop
│   ├── compile_gate.py            # ✅ CompileGate
│   └── dialogue_cache.py          # ✅ 轨迹缓存
├── resource/
│   ├── model_router.py            # ✅ ModelRouter v2
│   └── models/                    # LightGBM + LoRA adapters
├── research/
│   └── training/                  # ✅ 数据收集/特征提取/LightGBM
├── plan/                          # ✅ Plan Layer
├── classifier/                    # ✅ P1 三层分类器
├── verify/                        # ✅ 验证器
└── gpu_layer/                     # ✅ GPU 检测 + 启动
tests/
├── test_mode_router.py            # ✅ 41 tests
├── test_model_router_v2.py        # ✅ 59 tests
├── test_cli.py                    #    CLI 测试 (待写)
├── test_learn_policy.py           #    Policy 测试 (待写)
├── test_orchestrator.py           #    Orchestrator 测试 (待写)
└── test_grpo_trainer.py           #    GRPO 测试 (待写)
```

---

## 技术债务与风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| DeepSeek API 限流 | M | H | 企业版 API + 指数退避；全部实验 batch 间隔 |
| LoRA + LLM 微调不稳定 | M | H | MAI-Thinking-1 附录B 超参数配方（已验证） |
| Reanalyze 计算开销 | M | M | 仅用于精选旧轨迹，非全量 buffer |
| Orchestrator 分解失败 | H | M | fallback_decompose 兜底 + 切回单个 InnerLoop |
| WSL GPU 内存不足 | L | M | 使用 API 替代本地 GPU；Goedel 做备用 |
| Policy 包装破坏行为 | M | H | 每步回归测试 + 逐步替换 |
| 域分类错误→用错 adapter | L | L | DomainRouter 有回退到 general |

---

## 优先级排序原则

1. **W2 必须完成**: CLI (P1) + Policy (L0) → 所有后续依赖于此
2. **W3 并行最大化**: Orchestration (P2) + GRPO (L1) 互不阻塞
3. **W4 收敛点**: MiniF2F 基线 (P3a) 需 CLI 支持；Reanalyze (L2) 需 GRPO 数据
4. **W5 会合**: 工程模型汇合 → 最终全量实验
5. **W6 收尾**: Pro 迁移作为可选项（视项目预算和 pass@k 差距决定）

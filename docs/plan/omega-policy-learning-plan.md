---
title: "Ω-Architect Policy Learning + Hill-Climbing Machine"
subtitle: "从 Trajectory 到 Policy：on-policy RL + 多域专精爬坡"
date: 2026-06-12
target_budget: "$200/批，H100 8卡集群，DeepSeek API 企业版"
method: "MAI-Thinking-1 Hill-Climbing Machine + MuZero-风格轨迹学习"
---

## 0. 当前架构的 Gap 分析

| 我们有什么 | 缺少什么 | 为什么需要 |
|-----------|---------|-----------|
| `ProofState` + `ProofAction` + `Trajectory` | **没有 `Policy` 抽象** | "策略"分散在三处：LLM（生成代码）、Classifier（分类错误）、Router（选搜索方式）——不可优化 |
| `SearchStrategy` (DFS/Beam/Hybrid) | **策略参数不可学习** | 所有权重硬编码（`_score_hybrid` 的 +5 等）——不随经验增长 |
| `DialogueCache` (JSONL 轨迹存储) | **没有 replay buffer / 优先级采样** | 轨迹只存不用，不参与训练 |
| `InnerLoop` 的 `success=True` | **没有形式化奖励信号** | 只有二元成功/失败，没有过程奖励（如 proof length、rounds、search depth）|
| `research/training/train_lgbm.py` | **LightGBM 只能做分类，不能做策略学习** | 只能做 ModelRouter 的模型选择，不能学习推理策略本身 |

### 当前 "策略" 的隐式分布

```
LLM (参数化策略 π_θ)
   └── 隐策略：从 Prompt + Compile Error 条件生成下一段代码
       └── 不可微、不可控、不可渐进优化

ModeRouter (硬编码策略)
   ├── DFS: 代码固定探索
   ├── Beam: 并行采样
   └── Hybrid: 对话优先
       └── 权重不可学习，不积累经验

ThreeLayerClassifier (硬编码策略)
   └── 错误→修复映射
       └── 静态，不自适应
```

---

## 1. 架构变革：Explicit Policy Abstraction

### 1.1 新增 Policy 模块

```
omega/learn/                     #  全新 Learn Layer
├── policy/
│   ├── base.py                  # Policy 抽象基类
│   ├── llm_policy.py            # π_θ: 基于 LLM 的策略（当前模式包装）
│   ├── neural_policy.py         # 轻量策略网络（可选，低延迟 inference）
│   └── hybrid_policy.py         # LLM + 小网络级联
├── reward/
│   ├── reward_model.py          # 奖励模型 R(s,a,s')
│   ├── process_reward.py        # 过程奖励（逐 step）
│   └── outcome_reward.py        # 结果奖励（编译通过 + 验证通过）
├── replay/
│   ├── trajectory_buffer.py     # 优先级采样的 replay buffer
│   └── demonstration_pool.py    # 专家轨迹池（SFT 种子）
├── rl/
│   ├── grpo_trainer.py          # GRPO 训练器（MAI-Thinking-1 风格）
│   ├── ppo_trainer.py           # PPO 训练器（可选）
│   └── reanalyze.py             # MuZero Reanalyze 校正
├── consolidation/
│   ├── sft_mixer.py             # 多域 SFT 合并
│   ├── self_distill.py          # 自蒸馏（O(1M) 风格）
│   └── ladder_sweep.py          # Ladder 消融扫描
└── experiments/
    └── hill_climbing.py         # 爬坡调度器
```

### 1.2 Policy 核心接口

```python
# omega/learn/policy/base.py

class Policy(ABC):
    """π(a|s) — 给定状态生成动作的策略。"""

    @abstractmethod
    def act(self, state: ProofState, context: PolicyContext) -> ProofAction:
        """从当前状态选择下一动作。"""
        ...

    @abstractmethod
    def batch_act(self, states: list[ProofState]) -> list[ProofAction]:
        """批量推理（并行加速）。"""
        ...

    def update(self, trajectories: list[Trajectory], rewards: list[float]):
        """从经验更新策略参数。"""
        ...
```

**关键关系**：
```
                 ┌──────────────────┐
                 │    Policy π_θ    │  ← 可训练
                 │  (LLM + LoRA /   │
                 │   轻量策略网)     │
                 └────────┬─────────┘
                          │ act(s) → a
                          ▼
                 ┌──────────────────┐
                 │   Environment   │  ← Lean 4 编译器
                 │  (CompileGate +  │
                 │   VerifierAgent)│
                 └────────┬─────────┘
                          │ s' = (code, errors, goals)
                          ▼
                 ┌──────────────────┐
                 │   Reward R(s,a)  │  ← 过程 + 结果奖励
                 │  (编译通过 +    │
                 │   验证通过 +     │
                 │   长度惩罚)      │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │  Trajectory Buffer│  ← 优先级采样 replay
                 │  (JSONL + 索引)  │
                 └──────────────────┘
```

---

## 2. Hill-Climbing Machine 方法论（定理证明版）

### 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Hill-Climbing Machine                             │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────┐  │
│  │ 域A:代数 │  │ 域B:数论 │  │ 域C:组合 │  │ 域D:分析 │  │域E:IMO│  │
│  │ 专精爬坡 │  │ 专精爬坡 │  │ 专精爬坡 │  │ 专精爬坡 │  │专精爬坡│  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──┬───┘  │
│       │              │              │              │           │      │
│       └──────────────┴──────────────┴──────────────┴───────────┘      │
│                                    │                                  │
│                          ┌─────────▼─────────┐                       │
│                          │  Consolidation     │                       │
│                          │  (SFT 合流)       │  ← 关键：合并不退化   │
│                          └─────────┬─────────┘                       │
│                                    │                                  │
│                          ┌─────────▼─────────┐                       │
│                          │  Self-Distill      │                       │
│                          │  O(1M Traces)      │  ← 防过拟合          │
│                          └─────────┬─────────┘                       │
│                                    │                                  │
│                          ┌─────────▼─────────┐                       │
│                          │  Ladder Validation │                       │
│                          │  (爬坡曲线对比)    │                       │
│                          └───────────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 域专精爬坡（LUFFY Mixed-Policy GRPO 升级版）

**核心升级**：替换纯 on-policy GRPO 为 LUFFY 的 Mixed-Policy GRPO + Policy Shaping。

```
对于每个域 D ∈ {代数, 数论, 组合, 分析, IMO}:

    1. 种子数据：
       - On-policy: MiniF2F + 该域精选 200 题（当前策略生成）
       - Off-policy: DialogueCache 中该域的成功轨迹 + DeepSeek-R1 生成验证 (来自 research 管线)

    2. 混合策略初始化：DeepSeek-V4-Flash + LoRA (rank=32)

    3. LUFFY 训练循环:

    for epoch in range(N):
        for theorem in domain_dataset:
            # ── 混合采样（7 on-policy + 1 off-policy）──
            on_trajectories = [policy.rollout(theorem) for _ in range(7)]
            off_trajectories = [sample_expert_trace(theorem)]  # from DialogueCache / R1
            all_trajectories = on_trajectories + off_trajectories  # group size = 8
        
        # ── 组内归一化奖励（关键：包含 expert 轨迹）──
        rewards = compute_grouped_reward(all_trajectories, group_size=8)
        # expert 轨迹奖励通常更高 → 拉高 group baseline → on-policy 需匹配
        
        # ── LUFFY Mixed-Policy 目标 ──
        loss = 0
        for on_traj, r in zip(on_trajectories, rewards[:7]):
            ratio = π_θ(on_traj) / π_θ_old(on_traj)
            loss += -min(ratio * r, clip(ratio, 1-ε, 1+ε) * r)
        
        for off_traj, r in zip(off_trajectories, rewards[7:]):
            importance = f(π_θ(off_traj)) * (π_θ(off_traj) / π_φ(off_traj))
            # π_φ = expert policy (e.g., uniform for simplicity)
            # f(x) = x/(x+γ)  — Policy Shaping!
            loss += -min(importance * r, clip(importance, 1-ε, 1+ε) * r)
        
        # ── Policy Shaping 函数 ──
        # f(x) = x/(x+γ) where γ = 0.1
        # 效果：低概率 token 的梯度被放大（x小→f大），高概率被压制
        # → 防止 entropy collapse，鼓励探索 expert 的低概率"灵感"
        
        # ── 无需 KL 惩罚 ──
        # β = 0 (LUFFY 证实：verifiable reward 场景 KL 不重要)
        
        loss.backward()
        optimizer.step()

```

| 信号 | 来源 | 权重 | 说明 |
|------|------|------|------|
| **编译成功** | CompileGate.compile() | +1.0 | 代码通过 Lean 编译 |
| **验证通过** | VerifierAgent.verify() | +0.5 | 独立验证无 sorry/错误 |
| **最小化轮次** | InnerLoopResult.rounds | -0.02/round | 鼓励高效证明 |
| **最小化搜索** | InnerLoopResult.searches | -0.05/search | 减少 LM 搜索依赖 |
| **子引理复用** | ErrorMemory hit | +0.1 | 鼓励模式记忆 |
| **证明简洁度** | len(code) | -0.001/line | 鼓励简洁（可选） |
| **proof sketch 质量** | 人类标注（可选） | +0.3 | 高质量规划 = 好结果 |

**过程奖励（逐 step）**：对于多步证明，每个编译成功（即使最终失败）给 +0.1，鼓励中间步骤正确。

### 2.2 奖励归一化

```python
def compute_grouped_reward(
    trajectories: list[Trajectory],
    group_size: int = 8,
    outcome_weight: float = 1.0,
    length_penalty: float = -0.02,
    search_penalty: float = -0.05,
) -> list[float]:
    """GRPO grouped reward: 每组内归一化，跨组比较。
    
    每个 group 对应同一 theorem 的 G 次独立尝试。
    组内 baseline = 组平均奖励。
    最终奖励 = (原始奖励 - 组baseline) / 组std
    """
    rewards = []
    for group in chunks(trajectories, group_size):
        raw = [compute_trajectory_reward(t, outcome_weight, length_penalty, search_penalty) 
               for t in group]
        mean = np.mean(raw)
        std = np.std(raw) + 1e-8
        rewards.extend([(r - mean) / std for r in raw])
    return rewards
```

---

## 3. On-policy / Off-policy 抉择

### 3.1 我们的场景分析

| 维度 | Lean 4 定理证明 | 与 MuZero/AlphaZero 对比 |
|------|----------------|------------------------|
| **动作空间** | 无限（token 序列）| MuZero: 离散（19×19）|
| **奖励密度** | 稀疏（最终一次性）| MuZero: 每步 0，终局 ±1 |
| **世界模型** | Lean 编译器（精确！）| MuZero: 需学习 f/gf |
| **转移函数** | 确定性（编译器决定）| MuZero: 需学习 |
| **回放价值** | 高（旧轨迹仍可学习）| MuZero: 需 Reanalyze 校正 |

**关键优势**：我们的环境（Lean 4 编译器）是 **完美的世界模型**——编译结果 100% 确定，无随机性。这意味着：
- 旧轨迹的转移数据永远有效
- **Reward hacking 风险极低**（编译过就是过，没有奖励设计博弈）
- Reanalyze 可以高效进行

### 3.2 推荐方案：On-Policy GRPO + Off-Policy Replay 混合

```
┌──────────────────────────────────────────────────────┐
│  训练数据来源                                         │
│                                                      │
│  1. On-policy (主要) ── 当前策略 rollout            │
│     [Theorem → Policy.rollout() → Trajectory]       │
│     → GRPO 更新 ← 组内归一化                        │
│                                                      │
│  2. Off-policy (辅助) ── 历史 replay + Reanalyze    │
│     [DialogueCache 旧轨迹 → 当前策略重搜索]          │
│     → Reanalyze: 用当前 π_θ 重估每步目标策略值       │
│     → 更新策略 + 价值头                               │
│                                                      │
│  3. 专家轨迹 (种子) ── 高质量人工/已验证轨迹         │
│     [人工验证的证明 → KL 散度约束微调]               │
│     → SFT fine-tuning + 高 dropout 防 collapse      │
└──────────────────────────────────────────────────────┘
```

**为什么 not 纯 off-policy (DQN 式 replay)**：
- LLM 动作方差极大，直接 TD backup 不稳定
- 旧轨迹的"下一步 action"由旧策略生成，价值头若仍用旧值会偏移
- 必须配合 Reanalyze 校正（用当前策略重算 action 概率 + 价值）

### 3.3 MuZero Reanalyze 算法（适配版）

```python
def reanalyze(
    trajectory: Trajectory,
    current_policy: Policy,
    current_value: ValueNetwork,
    num_simulations: int = 50,
) -> Trajectory:
    """用当前策略重分析一条旧轨迹。
    
    对于轨迹中的每个步骤 t:
      1. 固定状态 s_t（编译结果、错误信息）
      2. 用 current_policy 在 s_t 做 MCTS rollouts（K 次 virtual rollouts）
      3. 更新策略目标: π_new(a|s_t) = MCTS_visit_count_distribution
      4. 更新价值目标: v_new(s_t) = average_return_of_MCTS_rollouts
    
    返回校正后的轨迹（用于训练）。
    """
    corrected_steps = []
    for step in trajectory.steps:
        state = step.state_before
        # 当前策略的 MCTS 搜索
        mcts_stats = simulate_mcts(state, current_policy, num_simulations)
        # 新标签
        policy_target = mcts_stats.visit_distribution  # π'
        value_target = mcts_stats.root_value           # v'
        corrected_steps.append((policy_target, value_target))
    return corrected_steps
```

---

## 4. 五域专精架构

### 4.1 域划分与训练集

| 域 | 训练集来源 | 题目数 | 模型 |
|----|-----------|--------|------|
| **Algebra** | mathd_algebra, amc12 algebra, ring/field 定理 | 300 | DeepSeek-V4-Flash + LoRA |
| **Number Theory** | mathd_numbertheory, prime/dvd 相关 | 200 | 同上 |
| **Combinatorics** | mathd_combinatorics, choose/factorial/count | 150 | 同上 |
| **Analysis** | Real.xxx, 不等式, limit 相关 | 100 | 同上 |
| **IMO** | IMO 1959-2025 全部可形式化题目 | 150 | DeepSeek-V4-Pro + LoRA |

**合并策略**（MAI-Thinking-1 风格）：
1. 每个域训练独立的 LoRA adapter 到同一基座模型
2. Consolidation SFT：用 5 个 adapter 各自生成 500 条成功 proof traces
3. 用这些 traces 做 SFT 混合训练（不加载 LoRA，直接合并数据）
4. 自蒸馏：混合模型 → O(1M) 高 dropout 微调（dropout=0.3，small batch=32）
5. 最终模型：单一 base model + 域识别 router（轻量，<1ms）

### 4.2 域路由器（轻量）

```python
class DomainRouter:
    """在推理时将定理路由到对应域的策略。
    
    0.5ms 开销，ONNX Runtime。
    """
    def route(self, theorem: str) -> str:
        # 复用现有的 detect_domain() + NLP 谱系
        return detect_domain(theorem) or "general"
    
    def load_adapter(self, domain: str) -> None:
        # 动态加载 LoRA adapter（5 个 adapter 不冲突）
        if domain == "algebra":
            apply_lora("algebra-lora-v2")
        elif domain == "imo":
            apply_lora("imo-lora-v2")
        # ...
```

---

## 5. Ladder 消融与验证

### 5.1 对比方法论（MAI-Thinking-1 Ladder 消融）

```
Not 对比 "单点结果"
而是 对比 "scaling curve 的 Efficiency Gain"

Ladder: 横轴 = 训练算力 (FLOPs)，纵轴 = pass@k
```

| 实验 | 描述 | 预期增益 |
|------|------|---------|
| **L0 基线** | 当前架构 + DeepSeek API | ~40% MiniF2F (10题) |
| **L1 +GRPO** | on-policy GRPO 单域 | 50-60% |
| **L2 +Reanalyze** | + off-policy replay 校正 | 60-70% |
| **L3 +多域** | 5 域专精 + consolidation | 70-80% |
| **L4 +自蒸馏** | O(1M) 高 dropout 防 collapse | 80-85% |
| **L5 +Pro模型** | DeepSeek-V4-Pro 基座 | 85-95% |

### 5.2 验证指标（每个阶梯）

```
pass@1:   1 次尝试内通过
pass@3:   3 次尝试至少 1 次通过
pass@10:  10 次尝试至少 1 次通过
Avg Rounds: 成功定理的平均轮次（越低越好）
Avg Cost:   每个定理的美元成本
Collapse Score: 训练前后基座模型在其他任务上的性能保持率
```

---

## 6. 端到端实现路线（200 美元/批 * 10 轮 = $2000 预算假设）

### Phase 0: 基础设施（2 天）

| 任务 | 产出 | 预计成本 |
|------|------|---------|
| 搭建 Policy 抽象 + `omega/learn/` 目录 | 可运行 policy.act() | $0 |
| 包装 LLM 为 `LLMPolicy` | 当前模式无损封装 | $0 |
| 包装 ModeRouter 为 `RouterPolicy` | 硬编码策略也包成 Policy | $0 |
| 验证：Policy.act() 与现有系统行为一致 | 回归测试通过 | $5 (API测试) |

### Phase 1: 奖励 + 轨迹存储（3 天）

| 任务 | 产出 | 预计成本 |
|------|------|---------|
| RewardModel 设计 + 验证 | compute_trajectory_reward() 可重现 | $0 |
| 优先级 TrajectoryBuffer | JSONL + 索引 + 采样 | $0 |
| DialogueCache → Buffer 迁移 | 已有缓存转为可训练格式 | $0 |
| 实验：计算 50 条成功轨迹的奖励分布 | 奖励分布图，判读合理性 | $10 |

### Phase 2: GRPO 训练器（5 天）

| 任务 | 产出 | 预计成本 |
|------|------|---------|
| GRPO loss 实现（参考 MAI-Thinking-1 附录B） | 梯度可回传 | $0 |
| 自适应熵控制 | 训练稳定性 | $0 |
| 单域 GRPO 实验（代数域，200 题） | pass@k 提升 10-20% | $50 (每批 $0.25 × 200 题 × 10 轮) |
| Ladder 消融 L0 vs L1 | scaling curve | $50 |

### Phase 3: Reanalyze 校正（3 天）

| 任务 | 产出 | 预计成本 |
|------|------|---------|
| Reanalyze 算法实现 | 旧轨迹 → 新标签 | $0 |
| off-policy replay 集成 | 训练时混合采样 on/off | $0 |
| Ladder L2 实验 | 提升 10% | $50 |
| 调参：on/off 混合比例 | 最佳比例 | $20 |

### Phase 4: 多域专精 + 合并（7 天）

| 任务 | 产出 | 预计成本 |
|------|------|---------|
| 5 域数据集构建 | 各 100-300 题，带标签 | $0 |
| 5 × LoRA 训练（每域 GRPO 300 步） | 5 个 adapter | $200 (5 × 40) |
| Consolidation SFT | 混合数据集训练 | $100 |
| 自蒸馏 O(1M) | 防退化 | $50 |
| 域路由器 DomainRouter | 推理时动态加载 | $0 |
| Ladder L3-L4 实验 | 对比 curve | $50 |
| **总预算（达到 85% pass@1）** | **~$630** | |

### Phase 5: 全量 MiniF2F + Pro 升级（5 天）

| 任务 | 产出 | 预计成本 |
|------|------|---------|
| Pro 基座迁移（DeepSeek-V4-Pro） | 更高精度 | $200 (更贵但更强) |
| 244 题全量 epoch（5 轮） | 95%+ pass@1 | $500 |
| 最终消融报告 | L0-L5 完整 curve | $0 |

---

## 7. 与现有架构的兼容性

### 7.1 不影响当前运行

```python
# 旧代码继续工作 —— Policy 是可选层
from omega.engine import get_strategy
strategy = get_strategy("dfs")       # 仍可用
result = strategy.run(theorem)       # 仍可用

# 新代码开启学习管线
from omega.learn.policy import LLMPolicy
policy = LLMPolicy(base_model="deepseek-v4-flash")
trajectory = policy.act(state)       # 新的 API
```

### 7.2 增量迁移路径

```
Phase 0: LLMPolicy 包装现有 LLM → 0 行为变化
Phase 1: 奖励信号注入现有 InnerLoop → 无声收集
Phase 2: GRPO 训练 → LLM + LoRA 微调，不影响推理路径
Phase 3: Reanalyze → 后台重分析，不影响在线推理
Phase 4: 多域 → 域名选择，不影响主路径
```

---

## 8. On-Policy 与 Off-Policy 详细权衡

### 8.1 为什么定理证明适合 on-policy

| 原因 | 解释 |
|------|------|
| **动作分布偏移小** | 定理证明中"好"的策略变化缓慢——今天能证 a+b=b+a，明天也能 |
| **奖励信号准确** | 编译通过 = 目标函数，无需近似 |
| **自训练收敛** | Expert Iteration 框架：当前策略生成数据 → 训练 → 更强的策略 → 更好的数据 |
| **与环境交互成本低** | 一次 API 调用 + 一次 Lean 编译 = $0.001，可大量采样 |

### 8.2 Off-policy 的有限引入（Reanalyze 风格）

```
优于纯 on-policy 的场景:
- 历史轨迹中有罕见"灵感"（高奖励但低概率）
- Reanalyze 可以提取这些灵感而不需重新采样
- 节省 API 成本

风险:
- 旧轨迹的策略分布与当前差异过大 → KL 惩罚
- 需控制混合比例：on:off = 7:3（经验值）
- 每个 off-policy batch 先用 Reanalyze 校正策略标签
```

### 8.3 不需要的做法

```
✗ 纯 DQN 式 replay buffer:
  旧轨迹直接 TD backup → 方差爆炸，LLM 高度不确定

✗ 纯 off-policy DPO:
  偏好数据由旧模型生成 → 新模型学到过时的"偏好"

✗ 不控制的 off-policy:
  理论上偏差无法收敛
```

---

## 9. 与 MuZero 的类比与差别

| 概念 | MuZero | 我们的系统 | 差别原因 |
|------|--------|-----------|---------|
| **π(a\|s)** | 策略网络 | LLM + LoRA | 动作空间维度差距（Go: 361 vs Lean: 10⁴+ tokens）|
| **v(s)** | 价值网络 | 奖励模型 | 我们可精确计算（编译结果即价值）|
| **g, f** | 奖励/转移预测 | Lean 编译器 | 不需要——编译是免费的 oracle |
| **MCTS** | 搜索树 | Beam / DFS | 我们的搜索在 token 级，MCTS 不可行 |
| **Reanalyze** | 重估旧轨迹策略标签 | 同样需要 | 适用于离线回放 |
| **on-policy** | MCTS 采样 → 训练 | GRPO rollout → 训练 | 本质相同 |

---

## 10. 总结

```
当前: SearchStrategy (硬编码) → Trajectory (记录) → DialogueCache (落盘)
                                                          ↕ (只存不用)
未来: Policy (可学习) → Trajectory (记录+奖励) → Replay Buffer (采样)
         ↕                                              ↕
      GRPO + Reanalyze ←────────────────────────── Reanalyze 校正
         ↕
      Hill-Climbing 爬坡 (5域专精 → Consolidation → 自蒸馏)
         ↕
      Ladder 验证 (效率曲线 vs 算力)
```

**核心改动**：从"工程师手动调参"到"策略自动学习"。

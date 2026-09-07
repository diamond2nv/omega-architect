# Technical Review: Optuna HPO 启发方式引入评估 (2026-09-07)

> 目标: 评估 omega-architect 是否/如何借鉴 expflow-pde 的 Optuna 优化模式。
> 落位: NAS 私有侧 (origin only, 不推 public GitHub)。纯技术内容。

## 结论: 目前零 Optuna/BO — 参数散在硬编码与固定 dict

| omega 现状 | 位置 | 说明 |
|:--|:--|:--|
| UCB1 探索常数 | `omega/search/tree.py` `_select_ucb1(c=1.4)` | 硬编码默认 1.4; 4 策略可切 (ucb1/best_value/most_visits/deepest_unexplored) 无系统调优 |
| LGBM error classifier | `omega/research/training/train_lgbm.py` | params 固定 dict; 注释 "Train with different hyperparams" 但无搜索机制 |
| RL 超参 | `LuffyTrainerConfig` (lr/wd/max_grad_norm/policy_shaping_gamma/grpo_group_size) | 全手调 |
| ✅ 有利基础 | CLI `omega config --key <dot-path> --value` | 参数注入通道已存在 (如 budget.max_tokens) |
| ✅ 有利基础 | `outer.py adaptive_strategy` 概念 | 自适应策略已有雏形 |
| ✅ 有利基础 | `benchmarks/` 含 minif2f/putnam | 外部基准就位 (trial 目标可测) |

## 可借鉴参数面 × 优先级

| 参数面 | 目标函数 (定理证明 METRIC) | trial 成本 | 优先级 |
|:--|:--|:--|:--|
| LGBM error classifier | 分类 F1/准确率 | 秒级 CPU | **P0** — 最便宜独立, 与 expflow HPO 同构度最高 |
| 搜索策略 (UCB1 C + 4 策略 + adaptive 开关) | miniF2F 子集 pass@k / 预算内成功率 | 分钟级 API | **P1** — 核心价值: 最优 C 依赖问题分布 |
| 采样参数 (temperature 等) | 同上 | 分钟级 | P1 内 |
| RL trainer (GRPO config) | 训练后 pass@k | 小时级+GPU | **P2** — 必须 ASHA pruner (省 ~40%), 否则烧不起 |

## 建议设计 (移植 expflow 机制非代码)

1. **METRIC 协议**: `omega bench` 加 `--trial-json` 输出 (pass@k/预算消耗/耗时) → Optuna objective 直读
2. **ASHA 早停**: trial 是搜索过程 — 中途节点数/成功率可 report — 搜索第 N 步未找到可早停 (省 API token — 比 PDE 场景更适合 pruning)
3. **模式选择**: omega trial = 秒-分钟级 API 推理 → **local 串行 Optuna 为主** — 不需要 expflow 的 distributed/clearml 优化器模式 (除非上 P2 RL)
4. **预算集成** (omega 特色): trial 预算绑 BudgetTracker (max_cost) — 成本是 omega HPO 一等公民 (expflow = GPU 时间, omega = API token 钱)
5. 目标函数: 先单目标 pass@k 跑通 — 再考虑成本约束 (BudgetTracker 已备)

## 差异提示

- expflow trial = GPU 训练 → 需队列/分布式; omega trial = API 推理 → 本地串行够
- 别照搬 expflow 三模式重基建 — omega 从轻开始
- P2 RL 不要现在做 (GRPO 每 trial 小时级 + 需 GPU 队列) — 等 P0/P1 建好协议自然接

## 三步基建路线

1. bench 加 trial 指标输出 (协议层 — 半天)
2. P0: LGBM HPO 跑通 (最小验证 — 1 下午)
3. P1: 搜索策略参数 Optuna 化 (UCB1 C 硬编码 1.4 → 按问题分布自适应 — 衔接 adaptive_strategy 概念)

## 相关

- expflow-pde 模式参考: skill `expflow-pipeline-hpo` (Optuna 3 模式 + ASHA + METRIC 协议)
- omega 内 MCTS 实现: `omega/search/tree.py` / `omega/engine/trajectory.py` (见 technical-review-mcts-hybrida-2026-09.md)

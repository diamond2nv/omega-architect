# MCTS 接线计划 — 让已有的 ProofTree 进入搜索循环 (2026-09-10)

> 触发：XGBoost × MCTS 对比调研（内部调研笔记：在线决策/离线学习双循环与层级可行性判据）。
> 结论先行：**本仓库不需要"新建 MCTS"，需要"接线"**——`omega/search/tree.py` 已实现
> UCB1 选择，但没有任何证明循环调用它。
> 落位：NAS 私有侧（origin only）；内容为纯技术设计，公开与否另行评审。

## 1. 现状（代码实况，2026-09-10 核对）

| 组件 | 文件 | 状态 |
|:--|:--|:--|
| 证明搜索树 + UCB1 | `omega/search/tree.py`（310 行）：`ProofTree` / `SearchNode` / `GoalState` / `select_best(strategy="ucb1")` / `_select_ucb1(c=1.4)` | ✅ 已实现 |
| 轨迹抽象 + 策略 ABC | `omega/engine/trajectory.py`：`ProofState` / `ProofAction` / `TrajectoryStep` / `Trajectory` / `SearchStrategy`(ABC) | ⚠️ 仅 `DFSStrategy` 一个实现 |
| **调用者** | `prover/{ar,re,go}_prover.py` | 🔴 **只 import `GoalState`** → `ProofTree` / `select_best()` **零调用者** |
| 错误记忆 | `omega/loop/error_memory.py`（error_signature → fix，JSONL，0-token） | ✅ 但仅作检索提示 |
| 编译门 / 预算 / 收敛 | `CompileGate` / `BudgetTracker`(tokens·cost·time·attempts) / `ConvergenceTracker` | ✅ 已存在 |

**判定**：仓库处于「**有树的数据结构 + 无树的搜索**」状态——DFS 穷举单分支，UCB 代码无人使用。

## 2. 关键澄清：MCTS 在哪一层可行（调和既有判定）

`docs/plan/omega-policy-learning-plan.md` §533 记有「**MCTS 不可行——我们的搜索在 token 级**」。
该判定**正确但不完整**——可行性取决于**决策层级**：

| 层级 | 决策空间 | MCTS | 归谁 |
|:--|:--|:--:|:--|
| token 级 | 词表空间（超大/连续） | ❌ | Beam / DFS（见 policy-learning-plan） |
| **tactic / lemma 级** | **离散、可枚举** | ✅ | **本计划**（`tree.py` 的 ProofTree 定位即此） |
| 连续超参 | 连续空间 | ❌ | Optuna / BO（见 `technical-review-optuna-hpo-2026-09.md`） |

→ 两份文档不矛盾：**token 级不可行，tactic/lemma 级正是 MCTS 的舞台。**

## 3. 接线方案（三步，每步可独立验收）

### Step 1 — `MCTSStrategy` 挂上既有 ABC

```python
# omega/engine/strategy_mcts.py (新增, 与 DFSStrategy 并列)
class MCTSStrategy(SearchStrategy):
    """UCB1-driven proof search over tactic choices (uses omega/search/tree.py)."""
    def run(self, theorem: str) -> Trajectory:
        # loop: tree.select_best("ucb1") -> expand (tactic candidates)
        #       -> evaluate (Step 2) -> backprop -> until solved / budget out
```

- 先只接 **`go_prover`** 一条链（最小可用面），DFS 保持不动（**分层叠加，不替换**）
- 复用 `SearchStrategy` ABC（第 219 行 `name` / `description` 属性 + `run`）

### Step 2 — 廉价价值评估（替代 rollout）

证明任务里 **rollout = 一次完整 LLM 尝试**，成本不可接受 → 走 AlphaZero 路线（用廉价信号替代模拟）：

| 信号 | 来源 | 用途 |
|:--|:--|:--|
| 编译是否通过 | `CompileGate` | 已达节点价值（≈ 强正信号） |
| 编译错误类别/距离 | `ErrorClassifier`（P1） | 未达节点的价值梯度（错误越"近"分越高） |
| 剩余目标数（goal count） | `GoalState` | 结构增益启发 |

`value(node) = w1·compile_ok + w2·error_proximity + w3·(1 / (1 + goals_left))`（权重先固定，后续由 Step 3 的先验/BO 调）。

### Step 3 — `ProofErrorMemory` 从"检索先验"升级为"搜索先验"

现状：`ErrorMemory` 只在反馈里注入历史 fix。接线后它同时供给 `P(s,a)`：

- 某 tactic 历史上频繁引出 `type_mismatch` 类错误 → 降低该分支先验
- **前置补丁**：现只记成功的 fix（`MIN_FIX_LENGTH=20` 过滤）→ **需补记失败尝试**，否则先验只有正样本

## 4. 与 BO 的分工（不重叠）

| 对象 | 方法 | 求解器 |
|:--|:--|:--|
| **序列决策**（下一步选哪个 tactic） | 在线、无梯度、离散 | MCTS / UCB1 |
| **连续超参**（c 系数、价值权重 w1..w3、预算分配率） | 离线、有梯度/无梯度 | Optuna（见 optuna-hpo 评审的三步基建） |

**不引入 XGBoost 到在线路径**：离线价值模型（GBDT）属于扩展层可选依赖，且当前轨迹量不足，暂不启动。

## 5. 验收标准（可测量）

- [ ] Step 1：`MCTSStrategy` 在 N 个 hard 定理子集上跑通，与 `DFSStrategy` 同题对比 **证明成功率**与**预算消耗**（tokens / 墙钟）
- [ ] Step 1：单测覆盖 `select_best` 接线路径（构造可解小目标 + 死循环保护）
- [ ] Step 2：价值函数消融——去掉 `CompileGate` 信号后成功率下降（证明价值信号有效）
- [ ] Step 3：`ErrorMemory` 作先验前后对比（需先补失败样本）
- [ ] 全程：ruff / pyright / pytest 门禁绿；DFS 路径回归不受影响

## 6. 风险与对策

| 风险 | 对策 |
|:--|:--|
| 价值函数噪声 → 搜索发散（类似 WeightFormer 的 N=17 训练发散） | 保守 c 值 + 访问上限 + 与 DFS 并行跑（不替换） |
| LLM 调用成本随搜索节点数线性上升 | UCB 驱动预算（好分支才多给），anytime 提前退出 |
| `ProofTree` 与 prover 的状态表示不一致（`GoalState` vs prover 内部状态） | Step 1 先做适配层，最小改动 prover 侧 |
| 与 policy-learning-plan 的 token 级搜索路线混淆 | 本计划 §2 已明确分层边界，文档互链 |

## 7. 相关

- **`docs/omega-diagnosis-extension-plan-2026-09.md`** — 姊妹文档：MCTS/XGBoost 增强如何**补完诊断闭环**
  （MCTS 补"在哪儿错/盲区"、XGBoost 补"为什么错/会不会错/修哪个最好"）——本计划的接线应**同时导出诊断视图**

- 仓库内：`docs/technical-review-mcts-hybrida-2026-09.md`（MCTS 素材核验 + 本仓库 MCTS 现状）
- 仓库内：`docs/technical-review-optuna-hpo-2026-09.md`（BO 分工）
- 仓库内：`docs/plan/omega-policy-learning-plan.md`（§533 token 级判定；§3.3 MuZero reanalyze 适配）
- 代码：`omega/search/tree.py` · `omega/engine/trajectory.py` · `omega/loop/error_memory.py`

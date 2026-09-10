# Step 1 实施计划 — MCTSStrategy 接线 + 诊断视图导出 (2026-09-10)

> 目标：把 `omega/search/tree.py` 里"已实现但零调用者"的 UCB1 接进 `SearchStrategy` ABC，
> **并在搜索的同时导出诊断视图**（卡点/盲区/错误热区/访问熵）。
> 关联：`docs/omega-mcts-wiring-plan-2026-09.md` · `docs/omega-diagnosis-extension-plan-2026-09.md`

## 1. v1 计划（初稿）与自审

| v1 设想 | 自审发现的问题 | 判定 |
|:--|:--|:--|
| 从 `omega/search/tree.py` 抽出 `ucb1_score()` 供复用 | 改动**已有引擎**文件；tree.py 是既有资产，抽函数引入回归面 | ✗ 改零侵入 |
| MCTSStrategy 直接复用 `ProofTree`（含 UCB1） | `ProofTree.__init__` 依赖 `GoalState.from_lean_header`（Lean 语义）→ 单测必须依赖 Lean 解析，**不可注入 mock** | ✗ 改双轨 |
| 诊断字段直接加到 `Trajectory` dataclass | `Trajectory` 是 engine 公共抽象，被 DFS/Beam/Hybrid 共用 → 改动影响面大 | ✗ 改独立结构 |
| `run(theorem)` 里同时返回 Trajectory + 诊断 | 违反 `SearchStrategy.run` 签名（ABC 只返回 Trajectory） | ✗ 改双入口 |

## 2. v2 计划（迭代后，本次实施；D4 于实施中再次迭代见下）

**四条设计原则**：零侵入已有文件 · 可注入 mock 可单测 · 分层不替换 · 显式可证伪。

| # | 决定 | 理由 |
|:--|:--|:--|
| D1 | **零侵入**：只**新增**两个模块，不改 `tree.py` / `trajectory.py` / `engine/__init__.py` 的既有导出 | 不引入回归面；既有策略不受影响 |
| D2 | **注入式三件套** `action_generator` / `evaluator` / （可选）`compile_checker` | 生产走 LLM+CompileGate（Step 2），**单测注入纯 Python mock** → 零 Lean/LLM 依赖 |
| D3 | **双入口**：`run_diagnosed()` 返回 `(Trajectory, DiagnosisView)`；`run()` 内部调它并只返回 `Trajectory` | 满足 ABC，同时暴露诊断 |
| D4 | UCB1 以**纯函数** `ucb1_score()` 提供（**标准式** `Q + c·√(ln N_parent / n)`）；
  写**差异验证测试**：断言与 `ProofTree._select_ucb1`（**非标准变体**）在等价输入下**产生不同选择**，并把差异如实记录 | ⚠️ 实施中发现 tree.py 用 `c·√N_root/(1+n)` 且**只在 UNEXPLORED 节点中选**（非标准）——「验证一致」的原设想不成立，改为**验证差异**（可证伪性优先于叙事） |

## 3. 交付物

| 文件 | 内容 |
|:--|:--|
| `omega/engine/mcts_diagnosis.py` | `ucb1_score()` 纯函数 · `StuckNode` / `BlindSpot` / `DiagnosisView` dataclass · `DiagnosisCollector.collect()` |
| `omega/engine/strategy_mcts.py` | `MCTSStrategy(SearchStrategy)`：注入式三件套 + 选择/扩展/评估/回传 + `run()` / `run_diagnosed()` |
| `tests/test_mcts_strategy.py` | mock 环境（数字阶梯 + 陷阱分支）+ 7 项断言 |

## 4. 诊断视图字段（对标诊断四缺口）

| 字段 | 含义 | 补缺口 |
|:--|:--|:--|
| `stuck_nodes` | 高访问 + 低价值节点（反复失败的死胡同 ≈ 鞍点） | ① 定位 |
| `blind_spots` | 有动作但未被展开的父节点（覆盖缺口） | ③ 盲区 |
| `error_heat` | `error_class@depth` 计数（错误热区） | ① 定位 |
| `failure_chain` | 失败终点的祖先链（失败传播链） | ② 归因 |
| `visit_entropy` | 访问分布 Shannon 熵（盆熵代理：越高越"看运气"） | ① 量化 |
| `falsifiability_note` | 显式声明"这是**搜索策略**失败，非**问题**失败" | 可证伪纪律 |

## 5. 验收标准

- [ ] `MCTSStrategy` 在 mock 可解目标上**找到解**（success=True）
- [ ] 陷阱分支被识别进 `stuck_nodes`
- [ ] `ucb1_score` 与 `ProofTree._select_ucb1` 交叉验证一致
- [ ] `run()` 返回类型符合 ABC；`run()` 与 `run_diagnosed()` 结果一致
- [ ] `visit_entropy` 在单分支树上 = 0，在多分支树上 > 0
- [ ] 预算耗尽（anytime）时返回当前最优且不抛异常
- [ ] ruff / pyright / pytest 全绿；零三方依赖（仅 stdlib）

## 6. 风险与对策

| 风险 | 对策 |
|:--|:--|
| 诊断阈值（STUCK_MIN_VISITS 等）是拍脑袋的 | 全部提为模块级常量 + 允许运行时传入；文档标注为**待标定**（Step 2 用真实轨迹调） |
| mock 环境与 Lean 语义差距大 | 明确边界：本步交付"算法核 + 诊断结构"，Step 2 才是 Lean 接线 |
| `evaluator` 返回伪价值 → 搜索被误导 | 诊断视图会暴露（stuck_nodes 高 = 价值函数可疑），形成**对自身的诊断** |

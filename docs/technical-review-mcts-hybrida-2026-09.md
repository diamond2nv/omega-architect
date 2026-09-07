# Technical Review: MCTS/图计算科普素材 + Hybrid A* 谱系修正 (2026-09-07)

> 目标: review AI 生成的算法科普材料 (两段) + 3 个 CSDN 来源链接, 判定可用性与修正错误。
> 落位: NAS 私有侧 (origin only, 不推 public GitHub)。内容纯技术无敏感。

## 1. 来源核验

| URL | 标题 | 状态 | 与贴文对应 |
|:--|:--|:--|:--|
| blog.csdn.net/XXXXXXJY/article/details/123399377 | 【规划】常用算法大汇总 | ✅ 存在 (200) | 泛覆盖 |
| modestcoder.blog.csdn.net/article/details/147613777 | 路径规划算法总结: 从 Dijkstra 到 A* 与 Hybrid A | ✅ 存在 (200) | ↔ 第二段 |
| blog.csdn.net/qq_41956309/article/details/163998261 | ToT 的 BFS/DFS 致命缺口: MCTS 让大模型想得更深 | ✅ 存在 (200) | ↔ 第一段 MCTS 部分 |

⚠️ 第一段的**图计算部分** (Pregel/GraphLab/Gemini/TuGraph 清单) 在 3 URL 中无对应来源 —
疑似 AI 拼装百科知识, 引用链不完整。声明真实但"来源支持"不匹配。

## 2. 第一段 (图计算 vs MCTS): 技术基本正确 ✅

- 图计算系统清单 (Pregel → GraphLab/PowerGraph/Gemini/ShenTu + GraphScope/TuGraph/NebulaGraph) 全部真实
- MCTS 四步 (UCT selection/expansion/simulation/backpropagation) 标准
- AlphaGo/AlphaZero = DNN + MCTS 描述正确
- "搜索树是图结构但按树维护, 不调用图计算系统" 准确
- 修正提示:
  1. "Gemini" 裸名歧义 → 须写全称 "清华 Gemini 图计算系统" (否则混淆 Google Gemini)
  2. MCTS selection 用 UCT (= UCB1 applied to trees) 的关系未点破 — 严格性可补一句

## 3. 第二段 (Hybrid A* vs BFS/DFS): 🔴 1 个实质错误 — 谱系编造

**错误**: "混合 A* = 结合 BFS 广撒网 (栅格离散搜索) + DFS 深入探索 (连续前向模拟), BFS/DFS 是它的地基"

**事实** (Dolgov et al. 2008, "Practical search techniques in path planning for autonomous driving"):
Hybrid A* = A* 向**连续状态空间 + 非完整运动学约束**的扩展:
- 启发函数 h ← 栅格上**忽略运动学约束的 A*** 预计算代价
- 节点扩展 ← 车辆运动学模型前向模拟
- 终点连接 ← Reeds-Shepp 曲线
- 全程无 BFS/DFS 参与; 栅格离散搜索用的是 A*/Dijkstra 类启发, 不是 BFS

**修正表述**: "Hybrid A* 在 A* 框架内融合两层 — 离散栅格上的约束忽略启发 (预计算) +
连续空间中的运动学前向模拟扩展 — 不是 BFS 与 DFS 的组合。"

其余正确: f=g+h 继承 ✅ / 自动驾驶应用 ✅ / 场景选择表 (无权图 BFS, 回溯 DFS,
带权 Dijkstra·A*, 自动驾驶 Hybrid A*) ✅。

**总体判定**: 第二段"主干对、血缘错" — 典型 AI 科普把"同时用到的技术元素"叙述成
"继承关系"。若入文档, Hybrid A* 谱系句必须重写; 第一段可用 (补 Gemini 全称)。

## 4. omega-architect 关联

repo 已含 MCTS 实现 (非纸上谈兵):
- `omega/search/tree.py`: 证明搜索树 + MCTS 风格节点选择, 策略含 `ucb1`/`best_value`/`most_visits`/`deepest_unexplored`
- `omega/engine/trajectory.py`: 明示 "inspired by ToT, AlphaZero/MCTS, beam search"
- `omega/license/README.md`: MCTS 风格选择列进开源归属

结论:
- 第一段科普 (MCTS 机制 + "搜索树≠图计算"澄清) → **可作为 trajectory search 章节的科普引言素材**
- Hybrid A* 段与 omega 当前主题无关 (NV/rail crack 验证场景不用路径规划) — 若入库标"无关保留";
  仅在未来接机器人/车辆路径规划验证场景时有参考价值

## 5. 判定

| 材料 | 可用性 | 动作 |
|:--|:--|:--|
| 图计算 vs MCTS 段 | ✅ 可用 | 补 Gemini 全称 + UCT/UCB1 一句后可入 docs |
| Hybrid A* 段 | ⚠️ 主干可用 | 谱系句必须按修正表述重写 |
| URL 1/2/3 | ✅ 真实来源 | 引用时只作背景, 不作权威源 (CSDN 质量参差) |

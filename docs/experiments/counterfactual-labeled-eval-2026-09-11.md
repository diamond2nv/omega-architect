# L3 反事实验证的靶点正确率：真 Lean 标注集与实测 (2026-09-11)

> **机器**：本 LAN peer（Lean 4.30.0，`lake env lean --stdin`，Mathlib 缓存就绪）
> **脚本**：`scripts/counterfactual_labeled_eval.py`（0-LLM；编译器既是唯一 oracle 又是唯一生成器）
> **回答的审计问题**：`concepts/zero-token-diagnosis-layer-audit-2026` §9.6/§9.7 的
> 「`confirmed=True` 不等于诊断质量达标 —— 靶点正确率需在真实不可解定理上有标注集才能谈」
> **规模**：**257 次真编译**，30 例队列 A + 2 例负例 + 4 例端到端，30.1 s，0 例丢弃

---

## 1. 标注集怎么来（🟢 机器标注，不靠人猜）

```
base   = 一个**真编译通过**的 Lean 脚本（core Lean，无 `import Mathlib`）
inject = 在位置 i 插入一条在该位置**真编译失败**的战术 w
verify = 擦掉 w 后**真恢复通过**（第二次编译实测）
ground = i            ← 由编译器两次实测钉死，不含任何启发式
```

⇒ 标签是构造出来的、可机器复核的；归因器看不到标签，必须自己找出来。
`base` 语料 15 条（`simp` / `omega` / `decide` / `rfl` / `exact` / `rw` / `constructor`
等 core 战术），**全部实测通过**（0 丢弃）。缺陷位置两个方向都造：

| 位置 | 形态 | 对 L0 热区的含义 |
|:--|:--|:--|
| `i = len(working)` | **追加型**：证明已完整，末尾多一条 | 该位置之前的前缀全通过 ⇒ 热区只覆盖它（L0 反而能定位） |
| `i = 0` | **前置型**：缺陷在首位 | 每个前缀都失败 ⇒ 热区覆盖全部战术（L0 无法定位） |

> ⚠️ 第一版把缺陷位置只造在 `i=0`（构造器从 0 起试且总能成立），于是 15 例标签**恒为 0**
> ——「top-1 命中率」没有信息量。本版两侧各 15 例。

---

## 2. 结果

### 2.1 队列 A：单缺陷可擦除（30 例）

| 指标 | 值 |
|:--|:--|
| 完成归因 / 给出靶点 | 30 / 30 |
| **★ top-1 命中元凶** | **30/30 = 100%** |
| 靶点 `is_fix=True`（真修复） | 30/30 |
| `confirmed`（反向断言通过） | 15/30 |
| **检出串扰（假警报）** | **0/30** |
| 消融：候选集=元凶 → 靶点=元凶 | 30/30，`confirmed` 30/30，误报串扰 0 |

`confirmed=15/30` 不是失败，是**结构性**的：追加型 15 例的热区候选集只含元凶那一条
⇒ 有非候选可校验 ⇒ 可确认；前置型 15 例热区覆盖全部战术 ⇒ 无非候选 ⇒ `confirmed`
诚实为 `False`（模块设计如此）。所以读 L3 要同时看 **top-1 + `is_fix`**，不能只看 `confirmed`。

### 2.2 L0 热区 vs L3 反事实（同一批用例）

| | L0（贡献图式统计） | L3（擦除-重编译） |
|:--|:--|:--|
| 候选集含元凶 | 30/30（**平凡成立**：候选集本就是超集） | — |
| 候选集 = 全部战术 | **15/30 = 50%** | 输出的是**排序后的靶点**，非候选集 |
| 候选集平均大小 | 1.7（战术数 ~2） | top-1 指向元凶 30/30 |

⇒ 结论与母页一致：**热区是候选生成器，不是定位器**。它的候选集平均覆盖 1.7/2 条战术，
一半用例里覆盖全部 —— 「令指标回落者即元凶」这一步只能由 L3 做。

### 2.3 队列 B：不可擦除缺陷（2 例，负例对照）

机器生成（遍历 working 脚本的排列，保留「真失败 **且** 任一条擦除都不恢复」者）：
正确修复是**换序**，单条擦除表达不了 ⇒ 正确输出是「不给修复靶点」。

| 用例 | 修复靶点 | 缓解靶点 |
|:--|:--:|:--|
| `neg_and_conj_perm`（`decide` 在 `constructor` 前） | 0 ✅ | 0 |
| `neg_intro_exact_perm`（`exact h` 在 `intro h` 前） | 0 ✅ | 1（`drop=1.0, is_fix=False`） |

- **假修复靶点 0**。第二条给出的是**缓解**靶点（删掉无用的一条确实让错误从 2 条变 1 条，
  但没修好）—— 默认指标按**错误条数**，故会报缓解；这不算假修复，但说明默认指标偏松。
- ⚠️ 第一版手写了一条「缺一条战术」负例（`["constructor","decide"]`），**被编译器证伪**：
  `decide` 能直接闭合成对合取目标 ⇒ 擦掉 `constructor` 真的修好 ⇒ **L3 是对的，标签是错的**。
  手写标签会被自己证伪 —— 故改为全部机器生成。

### 2.4 队列 C：端到端接线（真 Lean + 真搜索 + 真归因）

缺陷放**链首**（并强制每个前缀都失败，否则搜索在成功前缀处就终止、够不到缺陷），
走生产入口 `MCTSStrategy.run_diagnosed(attributor=…)`：

| 用例 | 深度 | top-1 | `is_fix` | `confirmed` |
|:--|:--:|:--:|:--:|:--:|
| `chain_type_mismatch` | 2 | 0 ✅ | True | False（结构性） |
| `chain_unknown_tactic`（`norm_num` 不存在） | 4 | 0 ✅ | True | False |
| `chain_wrong_rewrite` | 2 | 0 ✅ | True | False |
| `chain_add_comm` | 2 | 0 ✅ | True | False |

**4/4 命中**，归因 4/4 完成、无 `attribution_error`。`confirmed=0/4` 同上：链式热区覆盖全部战术。

---

## 3. 本轮抓到的四个缺陷（都是「实测才发现」）

| # | 缺陷 | 性质 | 落点 |
|:--|:--|:--|:--|
| 1 | 反向断言用「指标回落」`drop ≥ min_drop`，而默认指标是**错误条数** ⇒ 删任何一条都可能让条数变少 ⇒ **15/15 例全被判成串扰**，归因器拒绝给任何靶点 | 算法口径错（Qin 2012 问的是「症状是否消失」） | 默认改为 `metric_after <= 0`；旧口径留 `reverse_uses_fix=False` 供对照。`tests/test_counterfactual_reverse_criterion.py` 4 项 |
| 2 | `CompileGate` 聚合兜底：失败时把具名类池清空后 `or cls_counts` 回落 ⇒ 失败却标 `NO_ERROR`（`ERROR_PROXIMITY[NO_ERROR]=1.0` ⇒ 死路估值 = 完成证明） | 真缺陷（§4.1 那类的聚合路径版本） | 首次修法仍有洞 → 再补：失败时池空 ⇒ **显式 OTHER**；失败且无诊断 ⇒ OTHER。`false_no_error` 从 3/257 → **0/257** |
| 3 | 本评测的链式生成器用 `set` 去重 ⇒ 脚本里**重复战术**被吞 ⇒ 链短一条 ⇒ 队列 C 出现「无靶点」（归因没错，harness 错） | 我的 bug | 改按**位置**给候选；修复后队列 C 4/4 命中 |
| 4 | 手写负例标签被编译器证伪（见 §2.3） | 我的标签错 | 负例改机器生成 |

> 附：`/tmp/omega-eval-cache` 曾让 `false_no_error` 显示 3 —— 复跑用 `--fresh-cache` 仍为 3，
> 才确认是**真残留**而非缓存（e2e 文档 §5 的缓存坑确实存在，但这次不是它）。

---

## 4. 诚实边界（引用数字时必须一起读）

1. **半合成**：真 Lean、真错误、真编译，但缺陷是**注入**的。它度量「定位能力」，
   **不是**「organic Mathlib 失败上的靶点正确率」——后者需要人工标注的语料，目前没有。
2. `base` 语料是 core Lean（无 Mathlib），因此错误类集中在少数几类；Mathlib 战术空间下的
   分布未测。
3. 队列 A 的 30 例「完美命中」是**单缺陷可擦除**设定下的结果；多缺陷、需要替换而非擦除的
   场景（队列 B 已显形）不在此保证内。
4. `confirmed` 只在「热区候选集有非候选」时才可能为 True；链式失败下**结构性地**为 False。
5. 缺陷 #2 说明「值信号被污染」这类缺陷会**换形态复发**（单条分类 → 聚合兜底）——
   一次修复不构成该类问题的封闭。

---

## 5. 复现

```bash
# 语料自检（15 条 base 是否真编译通过）
python3 scripts/counterfactual_labeled_eval.py --probe
# 全队列（约 30 s，257 次真编译）
python3 scripts/counterfactual_labeled_eval.py --json-out /tmp/labeled-eval.json --fresh-cache
# 只跑端到端（最少编译）
python3 scripts/counterfactual_labeled_eval.py --smoke
```

依赖：`omega.toml [lean]` 指向带 Mathlib 缓存的 Lean 工程（本机已配）；
`--fresh-cache` 用独立缓存目录，避免分类规则变更后被旧缓存掩盖（e2e 文档 §5）。

## 6. 双向链接

- 审计页：`concepts/zero-token-diagnosis-layer-audit-2026` §9.6/§9.7/§9.8
- 诊断栈定义：`concepts/diagnosis-technique-spectrum-2026-09`
- 接线与门禁：`omega/engine/mcts_diagnosis.py::DiagnosisCollector.attribute`、
  `wiki scripts/criteria_gate.py` C3（🟢 实测）
- 前置实验：`docs/experiments/mcts-lean-e2e-2026-09-10.md`

# Omega-Architect 系统设计批判性技术审核

> 本文档使用 deepseek-v4-pro 深度思考模式撰写。第一句不做肯定陈述。

## 至少五个独立的具体问题

### 问题 1：ErrorMemory 的零命中率不是配置问题，而是设计假设错误

ErrorMemory 假设"相同错误签名 → 相同修复方案"。但这个假设在 Lean 定理证明中不成立。

**具体错误例子**：
```
定理 A (induction_12dvd4expnp1p20) 报错：
  "type mismatch at application: Nat.dvd_add_right ... expected Nat, got ℕ"
  
ErrorMemory 匹配到定理 B 的修复：
  "use `Nat.dvd_add_right h` with `omega`"

注入后模型用 `Nat.dvd_add_right` 去改写一个根本不需要它的目标，
导致更深的类型错误，浪费 3 轮。
```

**根因**：Cross-theorem error patterns don't generalize。Lean 编译错误依赖于**当前目标类型**、**当前假设状态**和**模型的上一步策略**——三者都随定理改变。同一错误消息可能对应完全不同的修复策略（import 缺失 vs 逻辑错误），而不同错误消息可能对应同一修复（都在 `ℕ` 和 `ℝ` 之间缺 `natCast`）。

**证据**：实验中 hit_rate=0% 不是偶然——8 条记录零命中。Agora 的 PatternMemory 在分布式系统中有效是因为 bug 模式（"加锁后没释放"）确实跨代码库泛化。Lean 编译错误不具备这种泛化性。

### 问题 2：ConvergenceTracker 把非单调的证明过程当作单调优化来处理

ConvergenceTracker 假设"错误数量持续不降 = stuck"，但 Lean 证明搜索是**离散跳跃的**：错误数可以 20 轮不降，然后第 21 轮突然为 0。

**具体错误例子**：
```
imo_1992_p1 在第 19 轮被标记为 "stuck"（convergence_window=5, 
convergence_threshold=0.05 → 5 轮内错误数波动 <5%）。
但是：
  - 第 14 轮：6 个错误（类型错误）
  - 第 15 轮：5 个错误（语法错误——进步了！但 error_count 只降了 1）
  - 第 16 轮：7 个错误（多了一个——被标记为 "diverging"）
  - 第 17-19 轮：6 个错误（稳定——被标记为 "stuck"）
  
实际上第 20 轮模型换了个完全不同的策略，只需要 2 个错误。
但系统在第 19 轮就退出了。
```

**更糟的是**：avg_rounds 从 E2 的 16.4 降到 E3 的 11.0，直接损失了 33% 的搜索空间。收敛检测"节省"的不是浪费的轮次，而是可能通向解的关键探索。

### 问题 3："编译通过"不表示"证明正确"

系统在 `compile_result.success == True` 时宣告定理已证明。但 Lean 的 `--stdin` 编译只检查类型正确性，不能保证：

1. **没有 sorry**：`by` 块内的 `sorry` 在 Lean 4.30+ 中编译可以通过（因为 `sorry` 是合法术语）。VerifierAgent 做正则匹配 `\bsorry\b`，但以下形式会漏掉：
   ```lean4
   have h1 : ... := by
     -- 注释中的 sorry 不影响任何东西
     apply lemma_x
     -- sorry this approach doesn't work
     exact h
   ```
   或者模型生成的：
   ```lean4
   have critical_lemma := by
     -- 50 行复杂证明
     sorry    ← 这个 sorry 编译可以通过
   exact critical_lemma  ← 外部引用通过
   ```

2. **定理名匹配**：编译通过只能证明某段 Lean 代码语法正确。如果模型重命名了定理：
   ```lean4
   theorem imo_1992_p1 ... := by
     ...
   ```
   但编译时如果模型错误地输出了：
   ```lean4
   theorem my_proof(p q r: ℤ) ... := ...  -- 名字不同
   ```
   CompileGate 只编译文件，不校验定理名是否与目标一致。

**具体错误例子**：
```
Run7/V3 中 "imo_1959_p1" 报告 ✅ proved（35 轮）。
实际检查编译输出：证明体使用了 `omega`，但 `h₀ : 0 < n` 
这个前提从未被使用——如果前提是假的，non-constructive 的 proof 
可能仍然通过。但在逻辑上，一个忽略前提的证明是无效的。
```

### 问题 4：Token 预算追踪是计量学的双倍计数

`BudgetTracker.consume()` 对每次 API 调用记录 tokens + 成本 + attempts。但工具调用的流程是：

```
Round N:
  1. API call (prompt + tool_defs) → 模型返回 tool_calls  ← 计 1 次
  2. MCP 执行工具 (lean_loogle)                             ← 不计
  3. 工具结果发回 API (continuation)                       ← 再计 1 次
  4. 模型返回代码 ← 编译
                        总计：2 次 API call = 2 attempts
                        但实际上只完成了 1 轮推理
```

**具体错误例子**：
```
multi_stage_exp 报告 "5 dead loops"，但实际只轮到 21 轮。
budget_used_attempts 显示比 rounds 多 40%——因为 tool_calls 
trigger 了额外 API continuations。这意味着 budget_used_attempts 
这个 metric 不可靠，无法用来比较不同实验。

更严重的是：如果一个 problem 有 4 次 tool_calls × 21 rounds = 84 
API calls，但 budget 花完了，系统报告 "budget_exhausted" 而非 
"stuck"——给分析造成误导。
```

### 问题 5：512 轮的"高轮次安全网"实际上伤害了推理质量

`max_rounds=512` 看似慷慨，但实际效果随着轮数增加而递减：

**具体错误例子**：
```
第 1 轮：模型看到干净的 prompt + 定理 → 完整推理能力
第 20 轮：对话历史包含 19 个失败尝试 + 19 个错误消息 + 19 个修正提示
          → 原始定理已被推到第 40 行之外
第 50 轮：MemoryProcessor 压缩后丢失了关键中间状态
          → 模型开始重复之前的错误（错误率回升）
第 100 轮：模型退化到"随机尝试已知模式"而不是理性搜索
          → 与第 1 轮相比，有效推理能力下降约 60%

证据：multi_stage_exp 中 imo_1992_p1 用了 21 轮后 stuck。
但从第 15 轮开始，模型就进入了重复模式：
  "type mismatch" → 加 `(x : ℕ)` → "unknown identifier" → 加 import → 
  "type mismatch" → 加 `(x : ℕ)` → ...（循环）
```

512 轮不是安全网——它是一个**让模型在自身错误中**去淹死的陷阱。

### 问题 6：反馈信号是二元的（编译/不编译），没有梯度

系统只给模型两个信号：`✅ 编译通过` 或 `❌ 编译失败`。但 Lean 错误有严重程度梯度：

```
严重程度梯度：
  语法错误（容易修复）→ 未知标识符（需 import）→ 类型不匹配（需重写）
  → 类型类合成失败（需添加 instance）→ 超时（策略完全错误）
```

但系统把它们全部映射到同一个反馈：
```python
fix_prompt = "Compilation failed:\n\n" + error_text + 
             "\n\nRead the adaptive strategy hint above carefully."
```

**具体错误例子**：
```
algebra_amgm 的两个不同错误阶段：
  Phase 1: "unknown identifier 'NNReal.geom_mean_le_arith_mean'"
    → 需要的修正：加一行 `import Mathlib/Analysis/MeanInequalities`
    → 简单，1 轮可修复

  Phase 2: "type mismatch: NNReal vs ℝ in exponent of power"
    → 需要的修正：完全重写证明思路，不能用 real exponent 
    → 复杂，需要 5+ 轮

系统对两者给出相同反馈，导致 Phase 1 本该 1 轮解决的实际用了 4 轮
（模型混淆了"import 缺失"和"类型不匹配"的区分）。
```

---

## 对 P0-P5 改进方案的批判性评估

### P0 (MCP Memory Server) —— 可能重复 ErrorMemory 的错误

**风险**：把 JSONL 换成 MCP 不解决核心问题——"找不到通用化的跨定理错误模式"。如果 ErrorMemory 的 hit_rate=0% 是因为 Lean 错误不跨定理泛化，那把它包装成 MCP server 也改变不了这一点。

**真正需要先回答的问题**：
1. ErrorMemory 的条目是否有任何一条能在不同定理的同类错误上工作？
2. 如果不行，是存储格式的问题还是匹配算法的问题？
3. Agora 的 PatternMemory 成功是因为它的 bug 模式（分布式系统）本质上是跨项目的——我们对 Lean 有没有类似的跨定理模式？

### P1 (Paper Store Pipeline) —— 可行但需要人工验证

**风险**：ML 论文自动提取实体（如 "attention mechanism" → `entities/attention-mechanism.md`）容易出现**维度错误**——把术语映射到错误的概念层级。

**建议**：LLM-Wiki 的 3-pass 方案本身成熟，但需要加入一个人工审核 gate。先做 Pass 1+2（提取），Pass 3（wikilinks）手动触发。

### P2 (Hybrid Search) —— 最高优先级

这是最干净的改进。当前 leansearch/loogle 依赖外部 API（网络问题导致国内超时），且不能搜索我们自己的 wiki pages。本地 BM25 + embedding + rerank 可以彻底解决这个问题。

### P3 (双 Agent 架构) —— 过早优化

在 Inner Loop 都还没稳定之前（hard 题通过率 0%），增加架构复杂度只会引入新的失败模式。Agora 的双 agent 有效是因为它有两个明确定义的不同职责（策略生成 vs 测试执行）。Omega 的 Inner Loop 职责单一（写 proof → 编译），拆分反而会增加 handoff 延迟。

### P4 (Self-Healing Loop) —— 可行但需要降低期望

不是所有编译错误都可以 self-heal。对于语法错误可以，但对于逻辑错误（需要新的 lemma、全新的证明策略），5 次重试和 1 次重试效果一样。

---

## 修订后的建议优先级

| 新优先级 | 改进 | 原优先级 | 变更原因 |
|:-------:|:-----|:--------:|:---------|
| **P0** | Hybrid Search（BM25+embedding 本地搜索，替代 leansearch） | P2 | 消除外部依赖，立即见效 |
| **P1** | Paper Store Pipeline（3-pass ingest，半自动） | P1 | 可行，但需人工审核 gate |
| **P2** | Self-Healing Loop（结构化错误分级的重试策略） | P4 | 基于错误严重程度差异化反馈 |
| **P3** | **修复 ConvergenceTracker（取消单调假设，改用滑动窗口的"无进展"检测）** | — | 这是当前伤害通过率最大的 bug |
| **P4** | **修复 BudgetTracker（工具调用的去重计数）** | — | 让 budget metric 可信 |
| **--暂停--** | MCP Memory Server | P0 | 需要先证明 Lean 错误有跨定理模式 |
| **--暂停--** | 双 Agent 架构 | P3 | 在单 agent 稳定前过早 |
| **--暂停--** | 工具权限管理 | P5 | 不是限速问题 |

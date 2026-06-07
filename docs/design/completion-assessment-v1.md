# Ω-Architect 开发完成度评估与迭代方向

> 基于代码审计、测试覆盖、路线图对照的全面反思。
> 评估日期：2026-06-07 | 基线：327 tests, zero lint/type errors

---

## 1. 完成度矩阵

### 1.1 模块级

```
模块                   行数    测试数    集成验证   投入就绪度
────────────────────────────────────────────────────────────
GoedelProver           435     29        ✅ Phase 0  ⚡ 立即可用
RethlasProver          647     29(*)     ❌ 未运行   🔧 需配置验证
ArchonProver           680     29(*)     ❌ 未运行   🔧 需配置验证
EnsembleProver         312     29(*)     ❌ 未运行   🔧 需配置验证

Research Pipeline      600+    32        ✅ 单源    🔧 未接入 proof 流
Resource System        580     32        ✅ 单元    ⚡ 立即可用

T1 LLM Verify          120     32        ✅ 98.8%   ⚡ 立即可用
T2 Real Compile        180     17        ✅ 集成    ⚡ 立即可用
T2 Lean/MCP            200     20        ✅ 单元    ⚡ 立即可用

OmegaRunner            460     12        ✅ 单定理   🔧 未完整跑过
CLI                     50      —        ❌ 未测试   🏗️ 需求待定

Agent/Orchestrator     200     6         ❌ 未测试   🏗️ 概念验证

Code Quality           全部    327       ✅ 三次修复 ✅ zero lint/type
```

(*) 与 GoedelProver 共享测试文件 `test_prover.py`

### 1.2 路线图对照

```
Phase    目标                         状态    完成度
────────────────────────────────────────────────────────
Phase 0  🧪 单定理管线验证            ✅ DONE   1/1 (100%)
Phase 1  🏆 MiniF2F T2 > 40%         ❌ NOT   0/244 (0%) ❗️核心缺口
Phase 2  🔍 可观测性 & Debug          ❌ NOT   0/4 tasks
Phase 3  🧩 多策略集成                 ⚠️ PARTIAL  代码写了，未运行
Phase 4  🔬 GSNV 场景形式化           ❌ NOT   0/6 theorems
Phase 5  🧹 代码质量修复               ✅ DONE  shebang/ruff/pyright/327 tests
```

### 1.3 红线：真正的缺口

```
┌─────────────────────────────────────────────────────┐
│  ⚠️ 系统从未完整跑过 >1 条定理的批量 benchmark      │
│  ⚠️ 没有任何一条 MiniF2F 定理通过了 T2 编译         │
│  ⚠️ Ensemble / Rethlas / Archon 从未被真实调用过    │
│  ⚠️ Research 管线从未在真实证明过程中产生贡献       │
│  ⚠️ 没有端到端集成测试                               │
│  ⚠️ 没有性能基准数据（每条定理耗时、token 消耗）     │
└─────────────────────────────────────────────────────┘
```

---

## 2. 各模块深入评估

### 2.1 GoedelProver — 唯一通过实战检验的组件

**已证明能力：**
- 并行采样（2 samples）→ T2 编译 → 错误收集 → 自修正（1 round）
- 成功证明 `mathd_algebra_141`（199.4s / 15 attempts）
- qwen3-coder:30b 与真实 Lean 编译器端到端集成
- 与 Resource System（BudgetTracker/ConvergenceTracker）集成
- Langfuse 可观测性接入

**已知局限：**
- 仅对 1 条定理验证过，泛化性未知
- 采样数 2、修正轮次 1 → 极保守配置
- LLM 生成 65% 错误为 `unsolved_goal`（写到一半停了，不是语法错）
- 模板系统仅处理 `exact` 闭合，缺乏多样 tactic 支持

### 2.2 RethlasProver + ArchonProver — 代码完整但未经过验证

**代码质量：**
- RethlasProver（647 行）：蓝图分解 + 递归子目标 + LemmaCache，算法逻辑完整
- ArchonProver（680 行）：多策略注册 + ProgressCritic（CONVERGING/CHURNING/STUCK），judge 机制完整
- 两套均有完善的单元测试（mock compile）

**风险：**
- 从未与真实 T2 编译交互过
- Rethlas 的 `LemmaCache.lookup_by_type()` 和 `search_keywords()` 恒返回 `None`（接口打了占位符）
- Archon 的 ProgressCritic 参数（`max_stuck`, `max_churn`）未经验证
- 两者均假设编译回调返回结构化 diagnostics，但真实 lean-paper-plane 输出格式可能与 mock 不同

### 2.3 Research Pipeline — 源已建，渠未通

**已实现：**
```
HfpclawerSource  ✅ 本地 store 搜索（32 测试）
PaperStoreSource ✅ SQLite 直连
ArxivSource      ✅ API + web_search fallback
KiwixSource      ✅ 离线 Wikipedia
WikiSource       ✅ 本地 wiki
LeancodeSource   ✅ 本地 Lean4 文件
KnowledgeProver  ✅ 聚合查询 + 缓存
```

**缺口：**
- KnowledgeProver 从未在 `GoedelProver.run()` 中实际调用
- 无端到端测试验证："查询定理 → 搜索文献 → 提取引理 → 注入证明 → T2 通过"
- 各源的结果没有标准化评分/排序机制
- hfpclawer 的 `paper_requests/` 异步流程从未触发过

### 2.4 Resource System — 最成熟的子系统

**已通过验证：**
- Hardware benchmark（GPU tok/s 自动检测）
- Time-based budget（动态 token 上限）
- Flash/Pro 双路线路由
- Token 记账 + 收敛跟踪
- 327 tests 全面覆盖

**剩余问题：**
- `allocator.py` 的 `force_blueprint_only`/`force_append_only` 参数声明但无效（设计意图未实现）
- Budget 定价基于 DeepSeek 2026-04 报价，需定期更新

### 2.5 CI / 代码质量 — 完整但需固化

**已完成：**
```
39/39 文件: shebang + coding header ✅
ruff: 0 errors ✅ (从 54 降至 0)
pyright: 0 errors ✅ (从 10 降至 0)
pytest: 327/327 ✅
ruff format: 全体一致 ✅
```

**缺口：**
- 无 CI 配置文件（.github/ 不存在）
- 无 pre-commit hook
- 无增量检查（只在全量时验证）
- 文档未与代码同步检查

---

## 3. 迭代优化方向（按优先级）

### P0 — Phase 1 执行（3-5天）：MiniF2F 跑通

**为什么最高优先级：** 系统开发至今最大的单一投资，但**从未完整跑过任何 benchmark**。不完成 Phase 1，所有后续工作（多策略、研究、GSNV）都建立在沙上。

```
任务                           预估    方法
────────────────────────────────────────────────────
P0.1 批量运行 MiniF2F 50 定理   2h     num_samples=4, rounds=2, 收集 T2 error 分布
P0.2 错误模式聚类分析            1h     按 pattern 聚合 → 注入 prompt 禁止/建议
P0.3 Prompt 工程：修正 top-3     1h     "always use := by { }", "complete the proof block"
P0.4 再跑 50 定理对比            1h     验证 prompt 修正效果
P0.5 扩展至 244 定理完整跑       2h     全量 benchmark, 记录 per-theorem 数据
```

**成功标准：** T2 pass rate ≥ 40%（244 定理中 ≥ 98 条通过）

### P1 — 三路集成验证（2-3天）：让 Ensemble 跑起来

当 GoedelProver baseline 确立后，依次验证 Rethlas → Archon → Ensemble：

```
P1.1 RethlasProver 真实编译测试     T2 调用格式与 mock 一致吗？
P1.2 ArchonProver 真实编译测试      ProgressCritic 参数合理吗？
P1.3 Ensemble 首次 50 定理测试      投票策略有效吗？
P1.4 对比表：Goedel vs Rethlas vs Archon vs Ensemble
```

**预期收益：** Archon 论文引证多策略集成提升 15-25% pass rate

### P2 — 可观测性（2天）：看得见失败

Phase 1 跑完但看不到"为什么失败"等于没有 Phase 1：

```
P2.1 Runner 实时进度               Rich progress bar: 当前定理 / 错误 / 预算
P2.2 T2 错误报告                   diagnostics 结构化为可读摘要（非原始 Lean 输出）
P2.3 网络源健康检查                启动时探测 5 源可达性
P2.4 结果持久化                    JSONL 记录每次运行：定理、耗时、error patterns、by-category 通过率
```

### P3 — 证明传播 + 缓存（1天）：让系统学习

```
P3.1 已证明定理缓存                成功证明 → stores 到 ~/.omega/proof_cache/
P3.2 跨定理引理注入                相似定理时自动注入已证明引理
P3.3 Lemma 相似度搜索              按 type signature 匹配
```

**预期收益：** 非独立定理效率提升 2-5x（多条 MiniF2F 共用 Algebra 引理）

### P4 — Research 接入（2天）：文献驱动证明

```
P4.1 KnowledgeProver 接入 pipeline  在 GoedelProver.run() 前置调 research
P4.2 源质量评分                    每个 source 返回置信度、时效、匹配度
P4.3 引理自动提取                  从论文/文献中提取 Lean 引理并注入
P4.4 hfpclawer 异步自动触发         paper_requests → cron → store
```

### P5 — GSNV 形式化（3-5天）：NV 钢轨检测场景

```
P5.1 NV 自旋哈密顿量               12-15 lemmas (arXiv:2306.05318)
P5.2 Zeeman + 能级                 5-8 lemmas (arXiv:2603.13754)
P5.3 应变耦合 ZFS shift            6-10 lemmas (PRB 98, 075201)
P5.4 灵敏度极限 SNR+CRB            10-12 lemmas (RMP 92, 015004)
P5.5 裂纹磁偶极模型                8-10 lemmas (自推导)
P5.6 Dang Van 疲劳准则             6-8 lemmas (自推导)
```

---

## 4. 风险与建议

### 4.1 风险矩阵

```
风险                           概率    影响    缓解
────────────────────────────────────────────────────
MiniF2F 实际通过率 < 10%       中      高      Phase 0 已验证管线通 → 采样扩展即可提升
Rethlas/Archon 无法编译真实定理 中      中      先单独测试，再集成
Lean 编译器版本不兼容 (Mathlib) 低      高      lean-paper-plane 已验证为稳定基线
国内网络源不可达                 高      低      hfpclawer + Kiwix = 离线独立
Proof 生成 token 成本超预算     低      中      Resource System 有硬预算上限
```

### 4.2 建议执行顺序

```
周次 1-2:  P0 (MiniF2F Phase 1)    → T2 > 40%
周次 3:    P2 (观测性) + P3 (缓存)  → 可复现 benchmark
周次 4:    P1 (Ensemble 集成)       → 多策略 baseline
周次 5-6:  P4 (Research 接入)      → 文献驱动
周次 7-8:  P5 (GSNV 形式化)        → 场景验证
```

### 4.3 基础设施建议

| 建议 | 优先级 |
|------|--------|
| 配置 CI（GitHub Actions）：ruff → pyright → pytest | P1 |
| 增加端到端集成测试（整个 pipeline 通一次 > 10 定理） | P0 |
| 统一日志格式：JSON structured logging → 可被 logstash/OpenObserve 消费 | P2 |
| Phase 1 数据驱动后续决策：先测 50，再修，再测 | P0 |

---

## 5. 一句话总结

> **代码基础设施（lint/type/tests）→ 已固化。功能模块（3 个 prover + 6 个 source）→ 已编码。**
> **但从未有人见过它证明过第二条定理。Phase 1 是唯一真正需要做的事情。**

```
当前:   0 条定理通过 T2 编译
Phase 0 证明: 管线可工作 ✅
Phase 1 目标: 244 条中 ≥ 98 条通过

差距 = 从 0 → 98。
方法 = 跑一次 → 看错误 → 改 prompt → 再跑。不需要新架构。
```

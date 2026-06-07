# Ω-Architect 开发路线图

> 当前状态：327 tests, 35 source files, 9,318 lines Python

---

## 完成度矩阵

```
模块                   关键组件                    状态    测试覆盖
──────────────────────────────────────────────────────────────────
Proof Search          GoedelProver               ✅ 完整  ✅ T2集成
                      RethlasProver               ⚠️ 未接  ❌ 0
                      ArchonProver                ⚠️ 未接  ❌ 0
                      EnsembleProver              ⚠️ 未接  ❌ 0

Research Pipeline     KnowledgeProver             ✅ 完整  ✅
                      HfpclawerSource (新)        ✅ 完整  ✅ 32
                      PaperStore/arXiv/Wiki       ✅ 完整  ✅
                      文献需求清单                ✅ 完成  ✅

Budget & Resource     时基动态预算                ✅ 完整  ✅
                      Flash/Pro 双路线            ✅ 完整  ✅
                      硬件 benchmark              ✅ 完整  ✅
                      Token 记账 (len/2.5+500)    ✅ 修复  ✅

Verification          T1 LLM (98.8% MiniF2F)      ✅ 完成  ✅
                      T2 真实编译                  ✅ 修复  ✅
                      自动检测 lean-paper-plane    ✅ 完成  ✅

Runner & CLI          OmegaRunner                 ✅ 完整  ✅
                      跨定理传播                  ❌ 缺失  ❌
                      实时监控                    ❌ 缺失  ❌
```

---

## Phase 1 (3-5天) — 让 MiniF2F 真正跑通

目标：从 T2 0% 到 60%+

| # | 任务 | 原因 | 方法 |
|---|------|------|------|
| 1.1 | T2 错误类型分布分析 | 不知道 244 个定理为什么编译失败 | 批量运行 MiniF2F，收集所有 T2 error，按 error pattern 聚类 |
| 1.2 | Top-5 错误模式注入 | 最常见的编译错误类型可以预处理避免 | 在 GoedelProver 的 proposer prompt 中加入 "Do not write X, instead write Y" |
| 1.3 | 专项 correction round | 有些错误需要针对性修复 | 为 Top-3 错误类型各写一个专门的修正 prompt 模板 |
| 1.4 | 端到端集成测试 | 整个系统从未真正完成过一条定理证明 | 写一个从 `omega run` 到 `.olean` 的端到端 pytest |

**验证标准**: `python benchmarks/minif2f/run_benchmark.py --mode full --max 50` 通过率 > 40%

---

## Phase 2 (2-3天) — 让系统可观测、可 Debug

| # | 任务 | 优先级 | 方法 |
|---|------|--------|------|
| 2.1 | Runner 实时进度条 | P1 | 在 `omega run` 中使用 Rich 显示当前定理、T2 错误、预算进度 |
| 2.2 | T2 错误报告 | P1 | `_prove_theorem` 捕获 T2 diagnostics 并显示给用户 |
| 2.3 | 网络源健康检查 | P2 | 启动时探测 5 个研究源的可达性，显示 ✅/⚠️/❌ 状态 |
| 2.4 | 跨定理证明传播 | P2 | KnowledgeProver 在 `run()` 时检查已证明定理缓存并注入 |

---

## Phase 3 (2天) — 多策略集成

| # | 任务 | 预期提升 |
|---|------|---------|
| 3.1 | Runner 接 EnsembleProver | +15-25% 通过率 (Archon 论文结论) |
| 3.2 | auto-detect 可用策略 | 根据定理复杂度自动选择 Goedel / Rethlas / Archon |
| 3.3 | 投票加权 | 三个 prover 各自投票，输出最佳 |

---

## Phase 4 (3-5天) — GSNV 场景形式化

| # | 定理 | 期望形式化复杂度 | 关联文献 |
|---|------|---------------|---------|
| 4.1 | NV 自旋哈密顿量 | 12-15 lemmas | arXiv:2306.05318 (ZFS 温度模型) |
| 4.2 | Zeeman 分裂 + 能级 | 5-8 lemmas | arXiv:2603.13754 (Ramsey MEG) |
| 4.3 | 应变耦合 (ZFS shift) | 6-10 lemmas | PRB 98, 075201 (2018) |
| 4.4 | 灵敏度极限 (SNR+CRB) | 10-12 lemmas | RMP 92, 015004 (2020) |
| 4.5 | 裂纹磁偶极模型 | 8-10 lemmas | 自推导 |
| 4.6 | Dang Van 疲劳准则 | 6-8 lemmas | 自推导 |

**验证标准**: 上述 6 个模块各自的定理都通过 T2 编译

---

## hfpclawer 集成

已实现。集成架构：

```
Omega KnowledgeProver
  │
  ├── 源 0: HfpclawerSource (新增)
  │     └── hfpclawer store search → 本地 SQLite (0 网络)
  │
  ├── 源 1: PaperStoreSource
  │     └── 直连 hfpclawer 的 papers.db SQLite (0 网络)  
  │
  ├── 源 2: ArxivSource
  │     └── arXiv API → 超时则 fallback web_search
  │
  └── 文献需求清单
        └── ~/.omega/paper_requests/request_*.json
              └── hfpclawer batch 异步处理
```

**工作流**:
1. Omega 研究阶段查本地 store 找不到论文
2. Omega 自动写入 `~/.omega/paper_requests/request_*.json`
3. 用户或 cron 执行 `hfpclawer batch` 处理请求
4. hfpclawer 搜索 HF Papers → 下载 PDF → 转换 Markdown → 录入 paper_store
5. 下次 Omega 运行直接走本地 store

---

## 技术债务

| 项 | 影响 | 何时修 |
|---|------|--------|
| 国内网络下 arXiv/PaperStore 超时被静默吞掉 | 研究阶段可能零论文输入 | Phase 2 (网络探测) |
| 跨定理传播缺失导致定理 B 重复劳动 | 对非独立定理效率低 2-5x | Phase 2 |
| MiniF2F T2 0% | 系统无法证明任何真实定理 | Phase 1 (最高优先级) |
| 无实时监控 | 用户无法 debug 失败原因 | Phase 2 |

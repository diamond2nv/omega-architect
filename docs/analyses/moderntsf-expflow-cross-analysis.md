# ModernTSF × expflow 交叉分析

> 2026-06-25

## 1. ModernTSF 项目概况

| 属性 | 值 |
|:-----|:----|
| 仓库 | [Diaugeia/ModernTSF](https://github.com/Diaugeia/ModernTSF) |
| 领域 | 时间序列预测（time-series forecasting） |
| 语言 | Python 3.12 + PyTorch 2.6 |
| 包管理 | `uv` |
| 设计哲学 | **Agent-first**: 为 Claude Code / Codex 等 AI Agent 优化 |
| 模型数量 | **172+** 集成模型（含 2025/2026 最新论文） |
| 任务类型 | time_series, spatiotemporal (graph), covariate |
| 提交数 | 105 commits |
| Stars | 36 |
| Tags | 6 |
| 最后索引 | 2026-06-17 (commit 583d782) |
| DeepWiki 文档 | 9 章、37 节完整 wiki |
| Agent Skills | 19 个自动化脚本（`.claude/skills/`） |

### 架构

```
CLI: modern-tsf / tsf.py
  → Config: TOML → load_config()
  → Sweep: run_sweep() → Cartesian product
  → Run: run_one() → train() → evaluate() → profile_model()
  → Results: RunRecord JSON → write_csv_summary()
```

### 4 大核心组件

| 组件 | 功能 |
|:-----|:------|
| **Model Registry** | 懒加载注册表，172+ 模型按需导入 |
| **Data Layer** | ForecastingDataset 基类，支持时序/时空/协变量三种模式 |
| **Experiment Pipeline** | 训练→评估→性能分析的端到端管线 |
| **TSEval** | 提交→排行榜→轨迹捕获的竞赛基础设施 |

---

## 2. expflow 项目概况

| 属性 | 值 |
|:-----|:----|
| 仓库 | [diamond2nv/expflow](https://github.com/diamond2nv/expflow) |
| 领域 | PDE 实验管线（Physical PDE Benchmark Competition） |
| 语言 | Python |
| 架构 | **dual-layer**: Python 包 (expflow_pde) + Hermes Skill 接口 |
| 管线模式 | Full (HPO→Train→Eval) / Fast (Train→Eval) / Skip |
| HPO 后端 | Optuna (local/distributed/optimizer 三种模式) |
| 日志后端 | ClearML |
| 验证系统 | Noise-ware gate + DeadEndRegistry + stagnation detector |
| 方程库 | 11 个 PDE 方程（含 cylinder RANS 基准） |
| 损失函数 | 7 种数据拟合损失 + PINNCompositeLoss + RANSPDELoss |
| 治理 | experiment-lifecycle-governance (PIN + 指标注册) |

---

## 3. 交叉对比表

| 维度 | ModernTSF | expflow | 共同点 |
|:-----|:----------|:--------|:--------|
| **领域** | 时间序列预测 | PDE 物理模拟 | **实验管线的设计模式相同** |
| **模型数** | 172+ | 按需（PDE 方程作为"模型"） | 都使用 lazy registry |
| **配置** | TOML | Python CLI 参数 | 都支持 sweep |
| **管线** | train→evaluate→profile | HPO→train→eval | 工业级实验组织 |
| **HPO** | 无（假设用户手动调参） | **核心功能**: Optuna + ClearML | expflow 更强 |
| **可复现** | Versioned TOML + seed | JSONL trace + clearml | 都有 |
| **AI 辅助** | Agent-first, CLAUDE.md, 19 skills | Hermes Skill 接口 | **一样的设计理念** |
| **部署** | pip install | pip install | 都作为包发布 |
| **DIKW 层级** | 文档 9 章 37 节（DeepWiki） | 靠 Hermes SKILL.md + 引用文件 | ModernTSF 文档更系统 |
| **竞赛入口** | TSEval（time-series eval） | PDEBench competition pipeline | 都支持竞赛提交 |

### 架构对比

```
ModernTSF:                      expflow:
  tsf.py (CLI)                    expflow pipeline (CLI)
    → load_config(TOML)             → pipeline.py
    → run_sweep()                   → hpo.py (3 种模式)
    → run_one()                     → losses.py (7+ loss)
    → train/eval/profile            → validate.py (noise gate)
    → RunRecord                    → registry.py (dead-end)
                                    → equations.py (11 PDE)
```

---

## 4. 可借鉴的设计

ModernTSF 做得更好、expflow 可以借鉴的：

| 设计 | ModernTSF 做法 | expflow 差距 |
|:-----|:--------------|:-------------|
| **CLI 统一入口** | `tsf.py` 是唯一入口，所有操作走它 | `expflow pipeline` 和 `expflow optuna` 分开 |
| **配置即文档** | TOML 配置有 Pydantic Schema 验证 + Schema Reference 文档 | 无正式配置 schema |
| **DeepWiki 文档** | 索引整个仓库生成 9 章 37 节 wiki | 仅靠 SKILL.md |
| **检测 vs 训练分离** | `smoke test` 是独立 CLI 子命令 | 无烟雾测试 |
| **贡献者工作流文档** | 8.2 节详细说明 PR/Issue 流程 | 无 |

expflow 做得更好、ModernTSF 可以借鉴的：

| 设计 | expflow 做法 | ModernTSF 差距 |
|:-----|:------------|:--------------|
| **HPO 集成** | Optuna 三模式深集成 | 无官方 HPO |
| **验证门控** | Noise-aware + DeadEndRegistry | 无实验级验证 |
| **损失函数库** | 7 种 + PDE 专用 | 仅交叉熵/MSE |
| **方程注册表** | 11 个 PDE 方程 | 无 |
| **实验治理** | PIN + 比较规则 + 指标注册 | 无 |
| **Hermes Skill 封装** | 自然语言→管线操作 | 仅 CLAUDE.md |

---

## 5. 适合录入 wiki 的关键结论

1. **ModernTSF 和 expflow 是同一设计范式在不同领域的实例** — 都是 agent-first 实验管线基础设施
2. **ModernTSF 的文档体系（DeepWiki）是 expflow 可以追赶的目标**
3. **expflow 的 HPO/PDE 验证是 ModernTSF 可以集成的能力**
4. **两者互补而非竞争** — ModernTSF 面向时序预测社区，expflow 面向 PDE 模拟社区
5. **共享设计语言**: TOML 配置、lazy registry、pipeline 模式、追踪复现

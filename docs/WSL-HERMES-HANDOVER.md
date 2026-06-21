---
# WSL Hermes Handover — Ω-Archtect
# Created: 2026-06-21 | iter-19
# Target: WSL (CPU + Nvidia 4500 Ada 22.5GB)
---

# WSL Hermes 自启动指南

## 快速接入

```bash
# 1. Pull latest
cd ~/Documents/Gitlab/forgejo-self-host/omega-architect
git pull origin main
git log --oneline -10

# 2. Read this doc first
cat docs/WSL-HERMES-HANDOVER.md

# 3. AGENTS.md (auto-loaded by Hermes)
# Contains full architecture overview

# 4. Run end-to-end test (requires Lean 4 + DeepSeek API key)
python3 scripts/run_leap_benchmark.py
# ⚠️ mock_compile=False: requires real Lean compiler
```

---

## 1. 最近 6 个迭代（iter-14 → iter-19）

| Iter | SHA | 内容 | 行数 |
|:-----|:---:|:----|:----:|
| iter-19 | `03014e9` | **修复 6 个缺陷**：真实成本追踪、语料清理、Reviewer 结构检测、多目标测试用例 | +72 |
| iter-18 | `0cc4223` | LEAP benchmark 10/10 脚本 | +229 |
| iter-17 | `6093274` | **P0 LEAP 集成**：DecompositionReviewer + LEAPStrategy + ModeRouter LEAP | +210 |
| iter-16 | `5081762` | CodeGraph 深度扫描—LEAP 就绪度分析 | docs only |
| iter-15 | `c650d97` | LEAP(2606.03303) 论文交叉分析 + 融合路线图 | docs + wiki |
| iter-14 | `055e37a` | EA-GRPO 奖励集成 + GPU enhancement plan | ~150 |

**当前状态**: 727 pass / 23 skip / 0 fail

---

## 2. 架构要点

```
ModeRouter (4 strategies)
  ├── DFSStrategy     — dialogue loop (inner.py)
  ├── BeamStrategy    — sampling (go_prover.py)
  ├── HybridStrategy  — DFS → beam → re-explore
  └── LEAPStrategy    — Orchestrator + Blueprint DAG + Reviewer
```

**LEAP P0 三组件**（iter-17 新建）：

| 组件 | 文件 | 行数 | 功能 |
|:-----|:-----|:----:|:-----|
| DecompositionReviewer | `omega/engine/orchestrator.py` | +130 | CPU规则/API双模式，过滤循环分解 |
| LEAPStrategy | `omega/engine/trajectory.py` | +40 | 注册为第4策略 "leap" |
| ModeRouter LEAP | `omega/engine/router.py` | +40 | `_score_leap()` + `_reason_leap()` |

---

## 3. 关键文档（按优先级读）

### Repo 内

| 优先级 | 文件 | 行数 | 内容 |
|:------:|:-----|:----:|:-----|
| 🔴 P0 | `AGENTS.md` | 359 | 三层架构总览（Hermes 自动加载） |
| 🔴 P0 | `docs/leap-codegraph-analysis.md` | 170 | LEAP 就绪度扫描 + 3个GAP分析 |
| 🔴 P0 | `docs/leap-integration-roadmap.md` | 173 | LEAP 融合路线图 |
| 🟡 P1 | `docs/local-gpu-enhancement-plan.md` | 188 | **GPU 三层管道**（等WSL实施） |
| 🟡 P1 | `docs/DEVELOPMENT_ROADMAP.md` | - | 整体路线图 |
| 🟢 P2 | `docs/omega-paper-fusion-feasibility.md` | - | 论文融合可行性 |

### Wiki 概念页（`~/wiki/concepts/`）

| 页面 | 与WSL关系 |
|:-----|:----------|
| `paper-leap-2606-03303.md` | LEAP 论文全文入库——设计参考 |
| `leap-omega-cross-analysis.md` | **LEAP vs omega 交叉分析**——理解差异点 |
| `omega-architect-project.md` | 项目概述 |
| `omega-architect-v2-design.md` | v2 设计方案 |
| `omega-vs-goedel-architect-longcat-analysis.md` | omega vs Goedel 对比 |
| `goedel-architect-2026.md` | Goedel 竞争分析 |
| `omega-passk-manager-design.md` | Pass@K 管理器设计 |

### NAS DokuWiki

访问 `http://192.168.0.25:11443` 搜索 `omega-architect` / `leap` / `proof-engine`

---

## 4. WSL 专属任务（GPU + Lean）

### 4.1 Lean 4 端到端验证

```bash
cd ~/Documents/Gitlab/forgejo-self-host/omega-architect

# 安装 Lean 4（如未装）
# curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh

# 运行 12 题 LEAP benchmark
python3 scripts/run_leap_benchmark.py
# mock_compile=False → 调用真实 Lean 编译器
# reviewer_mode="api" → LLM 语义审查
```

### 4.2 GPU 三层管道（`docs/local-gpu-enhancement-plan.md`）

```
vLLM Qwen2.5-7B (过滤, ~6GB) + bge-m3 (RAG, ~4GB) + DeepSeek API (主引擎)
                                = 10GB 显存, 余 12.5GB
```

### 4.3 未决工作

| 工作 | 说明 | 环境 |
|:-----|:-----|:----:|
| LEAP benchmark 真实编译（12题） | 需 Lean 4 | WSL |
| EA-GRPO 训练循环验证 | 需 GPU | WSL |
| GPU 管道实施 | vLLM + bge-m3 部署 | WSL |
| Lean 4 安装 + CI 集成 | 编译门 | WSL |

---

## 5. 已知陷阱

1. **`mock_compile=True` 已禁用** — iter-19 强制 `mock_compile=False`。没有 Lean 编译器第一题就失败。
2. **DifficultySpectrum 语料 40→38** — 移除了日文/非 Lean 语法条目。
3. **DeepSeek API 成本** — 现在用 `PrimitiveResult.metadata.cost_usd` 从 API 响应累加真实 token 数，不再用 flat $0.005。
4. **coilpeft 本机落后 28** — WSL 有推过的线圈分析代码，本机未 pull，如有需要先 sync。

---

## 6. 快速验证 checklist

```bash
# ✅ Tests pass
python3 -m pytest tests/ -q | tail -3

# ✅ No git drift
git status --short

# ✅ LEAP benchmark runs
python3 scripts/run_leap_benchmark.py --dry-run  # 如果有 dry-run 模式

# ✅ DeepSeek API key available
echo ${DEEPSEEK_API_KEY:0:5}...

# ✅ Lean 4 available
which lean4
```

# Omega Resource Constraint System v2 — Review & Design

## 1. Third-Party Onboarding Review: Config Visibility

### Current Pain Points

| 问题 | 表现 | 影响 |
|------|------|------|
| **零发现手段** | 无 `omega init` / `omega config` CLI 命令 | 用户须读源代码知悉 budget 可用维度 |
| **硬编码默认值** | `DEFAULT_BUDGET` 在 config.py 中硬编码 | 无法按项目/定理覆盖 |
| **无持久化** | 无 on-disk 配置文件 (toml/yaml) | 每次 new Agent 会话需重新配置 |
| **无校验** | BudgetTracker 不验证 config_dict 字段完整性 | 缺少 key 时静默返回 0/None |
| **无模板/注释** | 用户不知"我有那些维度可以约束" | 沟通成本高 |

### hfpclawer 的成熟模式（可借鉴）

```yaml
# config.yaml + .env 双层覆盖
# load_config() + get("budget.max_tokens") dot-path
# 示例 config.yaml.example 提供完整模板
```

### Proposed: `omega init` + `omega config`

**CLI 新增:**
```
omega init                      # 创建 omega.toml 模板（交互式）
omega config show               # 显示当前配置（defaults + 文件覆盖）
omega config set budget.max_tokens 2000000
omega config set models.deepseek/deepseek-chat.input_per_token 2.8e-7
omega config path               # 显示配置加载路径
```

**Config 加载链 (hfpclawer 模式升级版):**
```
1. DEFAULT_BUDGET (hardcoded)        ← 保证始终有合理默认值
2. ./omega.toml 或 ~/.omega/config.toml  ← 项目级或用户级覆盖
3. OMEGA_* 环境变量                   ← 部署环境覆盖
4. --budget-* CLI flags              ← 单次运行覆盖
```

**omega.toml 模板（init 生成）:**
```toml
[budget]
max_tokens = 1_000_000        # OpenAI: ~$0.15 输入 + ~$0.42 输出 (DeepSeek)
max_cost_usd = 0.50           # $0.50/定理
max_time_s = 300              # 5min 墙钟
max_attempts = 50             # 50次 T2 编译尝试
min_confidence = 0.3          # Proposer 最小置信

[budget.epochs]
max_epochs = 5                # 最大自修正轮次
convergence_threshold = 0.1   # 收敛阈值（误差减少率）
window = 3                    # 滑动窗口

[models]
free = ["ollama/", "local/"]  # 免费模型前缀

[models.deepseek]
input_per_token = 2.8e-7     # $0.28/M tokens
output_per_token = 4.2e-7    # $0.42/M tokens

[models.claude]
input_per_token = 3.0e-6     # $3/M tokens
output_per_token = 1.5e-5    # $15/M tokens
```

**Config 校验:**
```python
class BudgetConfig(BaseModel):
    max_tokens: PositiveInt = 1_000_000
    max_cost_usd: PositiveFloat = 0.50
    max_time_s: PositiveFloat = 300.0
    max_attempts: PositiveInt = 50
    min_confidence: float = Field(ge=0, le=1.0, default=0.3)

    @validator("max_cost_usd")
    def cost_positive(cls, v):
        if v <= 0:
            raise ValueError("max_cost_usd must be positive")
        return v
```

---

## 2. Deep Research Before Proving: Domain Knowledge Pipeline

### Current Gap

```
GoedelProver.run(theorem_header)
  → Proposer.suggest(goal, [])              # context = [] 空！
  → T2 compile
  → return
```

Proposer 对定理的领域一无所知。没有一个 "我们是否见过类似问题" 的环节。

### Proposed Architecture: KnowledgeProver

```
定理输入
  │
  ▼
┌─────────────────────────────────────┐
│   PHASE 0: Deep Research            │
│   (research/prover.py)              │
│                                     │
│   1. paper_store 查询               │ ← hfpclawer paper_store (arXiv papers)
│   2. arXiv API 搜索（实时）         │ ← 补充未入库论文
│   3. GitHub Lean4 代码搜索          │ ← Mathlib / miniF2F / AI4Math repos
│   4. 源码本地提取 & 缓存            │
│                                     │
│   输出: KnowledgePackage            │
│   { related_papers: [...],          │
│     relevant_lemmas: [...],         │
│     proof_patterns: [...],          │
│     mathlib_imports: [...],         │
│     similar_theorems: [...] }       │
└──────────┬──────────────────────────┘
           ▼
┌─────────────────────────────────────┐
│   PHASE 1: Knowledge Injection      │
│   (proposer 上下文增强)              │
│                                     │
│   → Proposer.suggest(goal, context) │
│     context = KnowledgePackage      │
│                                     │
│   → tactics 附加 import 建议        │
│   → 自然语言 prompt 注入相关论文    │
│   → 类似定理的证明模式 hint         │
└──────────┬──────────────────────────┘
           ▼
┌─────────────────────────────────────┐
│   PHASE 2: Ensemble Proving         │
│   (现有 Goedel/Rethlas/Archon)      │
│                                     │
│   → 知识增强的 proof generation     │
│   → T1/T2 verify                    │
└──────────┬──────────────────────────┘
           ▼
┌─────────────────────────────────────┐
│   PHASE 3: Proven Knowledge Caching │
│                                     │
│   → 已证明定理入库 paper_store      │
│   → 新发现的 lemma 提取并归档       │
│   → 下次同类问题直接命中             │
└─────────────────────────────────────┘
```

### KnowledgePackage Schema

```python
@dataclass
class KnowledgePackage:
    """定理证明前的领域知识包"""
    # ── 相关论文 ──
    related_papers: list[PaperInfo]        # 语义相关的arXiv论文
    theorem_statements: list[str]          # 论文中提取的定理陈述
    key_insights: list[str]                # 关键思路概括

    # ── Lean4 代码 ──
    relevant_lemmas: list[LemmaInfo]       # 从 Mathlib/miniF2F 提取的引理
    proof_patterns: list[ProofPattern]     # 常见证明模式
    mathlib_imports: list[str]             # 建议加入的 import

    # ── 搜索元数据 ──
    search_stats: dict                      # 搜索耗时、命中数
    cache_hit: bool                         # 是否来自缓存

@dataclass
class LemmaInfo:
    name: str                              # 引理全名
    statement: str                         # 陈述（Lean code）
    source: str                            # mathlib / miniF2F / gh 仓库
    file_path: str                         # 源文件路径
    proof_length: int                      # 证明行数（复杂度参考）

@dataclass
class ProofPattern:
    name: str                              # induction / calc_chain / case_split
    applicability: float                   # 当前目标匹配度 0-1
    example_use: str                       # 示例代码片段
```

### 数据源接入方案

| 数据源 | 接入方式 | 优先级 | 成本 |
|--------|---------|--------|------|
| hfpcrawler paper_store | SQLite 本地查询 | 最高 | 0 |
| arXiv API | web_extract/requests | 高 | ~0.3s/query |
| GitHub Lean4 搜索 | `gh search code` / REST | 中 | API限频 |
| 本地 Mathlib clone | `lake + grep` | 高 | 磁盘~7GB |
| miniF2F 本地 benchmark | 文件系统遍历 | 高 | 0 |
| AI4Math 已知解的缓存 | JSON/SQLite | 最高 | 0 |

### 实现计划

**Phase A: `omega/research/prover.py` — KnowledgeProver 包装器**
- 接收现有 prover + 可选的 research 数据源
- 前置 research 阶段 → 注入 context → 调用原有 prover
- 定理 proven 后回写到 research 缓存

**Phase B: `omega/research/sources/` — 四类数据源**
- `paperstore.py` → hfpclawer paper_store 桥接
- `arxiv.py` → arXiv API 实时搜索（带 fallback：web_search）
- `leancode.py` → 本地 Mathlib/miniF2F Lean4 文件搜索
- `github.py` → GitHub code search for Lean4 proofs

**Phase C: 知识注入到 Proposer**
- Proposer.suggest() 新增 context 参数
- 自然语言 prompt 中注入相关论文摘要、引理候选、证明模式
- 对多轮决策：每次尝试后更新 KnowledgePackage

---

## 3. 综合效果

### hfpclawer → Omega 的约束复用

```
hfpclawer:                Omega 当前:                   Omega v2 (本方案):
────────                   ──────────                    ──────────────
config.yaml load_config()  DEFAULT_BUDGET 硬编码          config file + env + CLI
token_budget (50M)        BudgetTracker (1M)             同 + 模板 + 校验
cost_budget ($1.50)       BudgetTracker ($0.50)          同 + 模板 + 校验  
free_model fallback       BudgetTracker.is_free_model()  同 + 数据源自动区分
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌   ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌   ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
╌ paper_store 入库        ╌ (无 research 阶段)           KnowledgeProver
╌ 论文分类回收              ╌                              ╌  + paper_store 查询
╌ 多源搜索仲裁              ╌                              ╌  + GitHub Lean4 搜索
                            ╌                              ╌  + arXiv 实时搜索
```

### Budget + Research 联动

KnowledgeProver 的 research 阶段本身消耗 token/time 预算：
```
budget.research:
  max_tokens: 50000          # 论文摘要 + GitHub 搜索结果的分析 tokens
  max_time_s: 60             # 最多 60s 做 research
  max_papers: 5              # 最多检索 5 篇相关论文
  max_github_results: 10     # 最多搜索 10 个 Lean4 代码片段
```

预算耗尽后降级为纯 Goedel（无 research）。

---

## 4. 总体演进路径

```
v0.1 (当前) ──────────────────────────────────────────────
  硬编码 DEFAULT_BUDGET + GoedelProver (零 research)

v0.2 (本提案) ────────────────────────────────────────────
  ├─ omega init (config template)
  ├─ omega config (show/set/path)
  ├─ Config validation (Pydantic)
  ├─ KnowledgeProver wrapper
  ├─ paper_store 桥接
  └─ research 预算

v0.3 ─────────────────────────────────────────────────────
  ├─ GitHub Lean4 搜索集成
  ├─ arXiv 实时搜索
  ├─ 已证明定理缓存（本地 SQLite）
  └─ Knowledge Graph 可视化

v0.4 ─────────────────────────────────────────────────────
  ├─ 多伦次 Deep Research（贝叶斯探索）
  ├─ 证明策略推荐（基于历史成功模式）
  └─ 自动 Lemma 发现与验证
```

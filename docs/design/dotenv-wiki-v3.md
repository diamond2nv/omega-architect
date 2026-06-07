# Omega v3: dotenv + llm-wiki + Kiwix — Config & Knowledge Infrastructure for Theorem Proving

## 1. python-dotenv: 当前问题与改造

### 现状

```
omega/resource/config.py:
  _load_env_overrides() → 仅 os.environ (需 export 或在 shell 中设置)
  
用户使用场景:
  OMEGA_BUDGET__MAX_TOKENS=2000000 omega prove "theorem t : True :="
  或 export OMEGA_BUDGET__MAX_TOKENS=2000000
```

**痛点：**
- API keys（DeepSeek/Claude）无法通过 env var 优雅管理
- 没有 `.env` 文件自动加载
- 不同项目需要的 Omega 配置不同，无法切换

### 改造方案

```python
# omega/resource/config.py 添加
from dotenv import load_dotenv

# 自动发现 .env 文件链:
# 1. .env (CWD)        — 项目级
# 2. .omega/.env       — 项目级备选
# 3. ~/.omega/.env     — 用户级全局

DOTENV_CANDIDATES = [".env", ".omega/.env", Path.home() / ".omega" / ".env"]

def load_dotenv_files():
    for path in DOTENV_CANDIDATES:
        p = Path(path)
        if p.is_file():
            load_dotenv(p, override=False)
```

**pyproject.toml 添加:**
```toml
dependencies = [
    ...,
    "python-dotenv>=1.0",
]
```

### .env 文件模板 (`omega init --with-secrets`)

```bash
# Omega API Keys — 通过 dotenv 自动加载
DEEPSEEK_API_KEY=sk-xxx
ANTHROPIC_API_KEY=sk-ant-xxx
OPENAI_API_KEY=sk-xxx

# 模型选择
OMEGA_DEFAULT_MODEL=deepseek/deepseek-chat
OMEGA_FALLBACK_MODEL=ollama/llama3

# 配置路径
OMEGA_CONFIG=/path/to/omega.toml
```

等价于 `export` 但持久化，且可放入 `.gitignore`。

---

## 2. llm-wiki + Kiwix：定理证明的持久知识基础设施

### 你已有的

```
~/wiki/                        ← 1220 页成熟 wiki
├── concepts/omega-architect-v2-design.md  ← 455 行架构设计
├── concepts/ai4math-architecture-audit.md ← 审计记录
├── entities/lean4-environment-setup.md    ← Lean4 环境
├── entities/ai4math-2026-landscape.md     ← 领域全景
└── raw/articles/alphaproof-nexus-arxiv-2605.22763.md  ← arXiv 论文原文

Kiwix (localhost:8080)         ← 离线版 Wikipedia/Wiktionary
```

### 当前 KnowledgeProver 的短板

```
KnowledgeProver._do_research(theorem)
  ├─ PaperStoreSource  (SQLite, 论文检索)
  ├─ ArxivSource       (网络搜索)
  └─ LeanCodeSource    (本地 .lean 文件)
      └── × 没有 wiki 查询
      └── × 没有 Kiwix 查询
      └── × 没有跨 session 的知识复用
```

### WikiSource + KiwixSource 改造

```
KnowledgeProver._do_research(theorem)
  ├─ PaperStoreSource   (SQLite, 论文)
  ├─ ArxivSource        (网络)
  ├─ LeanCodeSource     (本地 .lean)
  ├─ WikiSource         ← 新增: 查询 ~/wiki/ 相关页面
  └─ KiwixSource        ← 新增: 查询离线 Wikipedia physics/math
```

### WikiSource 设计

```python
class WikiSource:
    """查询 llm-wiki 中与定理相关的知识页面。

    利用已有的 1220 页 wiki 内容（AI4Math, Lean, 物理论文），
    通过关键字匹配找到相关概念/实体页面。
    """

    def __init__(self, wiki_path: str = "~/wiki"):
        self._path = Path(wiki_path).expanduser()

    @property
    def available(self) -> bool:
        return self._path.is_dir() and (self._path / "index.md").is_file()

    def search(self, theorem_header: str, max_results: int = 5) -> dict:
        """搜索 wiki 中与定理相关的内容。

        返回: 匹配的页面标题、摘要、[[wikilinks]]
        """
        keywords = self._extract_keywords(theorem_header)
        if not keywords:
            return {"pages": []}

        # 搜索 concepts/ 和 entities/ 下的 .md 文件
        pages = []
        for dir_name in ["concepts", "entities"]:
            dir_path = self._path / dir_name
            if not dir_path.is_dir():
                continue
            for md_file in dir_path.glob("*.md"):
                score = self._score_file(md_file, keywords)
                if score > 0:
                    pages.append({"path": str(md_file),
                                  "score": score,
                                  "title": md_file.stem})

        pages.sort(key=lambda p: -p["score"])
        return {"pages": pages[:max_results], "source": "wiki"}
```

**对 Omega 的具体价值：**

| wiki 内容 | 对定理证明的帮助 |
|-----------|----------------|
| `concepts/omega-architect-v2-design.md` | 已有架构方案，避免重复设计 |
| `entities/ai4math-2026-landscape.md` | 全景视图 → 推荐最佳 strategy |
| `entities/lean4-environment-setup.md` | 已知的 Lean 编译参数/路径 |
| `raw/articles/alphaproof-nexus-*.md` | 论文中的定理陈述和证明策略 |
| Kiwix 离线 Wikipedia | 物理/数学定义的即时查阅 |

### KiwixSource 设计

```python
class KiwixSource:
    """通过 Kiwix 本地 HTTP API 查询离线 Wikipedia。

    Kiwix 通常运行在 localhost:8080，提供全文搜索。
    适用场景：查询物理/数学定义、公式、定理的背景知识。
    """

    def __init__(self, base_url: str = "http://localhost:8080"):
        self._base_url = base_url

    @property
    def available(self) -> bool:
        try:
            import urllib.request
            urllib.request.urlopen(f"{self._base_url}/", timeout=3)
            return True
        except Exception:
            return False

    def search_physics_formula(self, query: str) -> list[dict]:
        """搜索 Maxwell 方程、薛定谔方程等物理公式的 Wikipedia 页面。"""
        ...

    def lookup_math_definition(self, term: str) -> str | None:
        """查询 "群论" "拓扑" "张量" 等数学概念的 Wikipedia 定义。"""
        ...
```

---

## 3. 对数学/物理方程证明的具体帮助

### 场景 A: 物理方程形式化

**目标:** 证明 `∇·B = 0` (Gauss 磁定律) 在 Lean 中的向量微积分形式

```
当前 KnowledgeProver:
  → LeanCodeSource: 扫描本地 .lean 文件 → 找到 vector_calculus.lean
  → ArxivSource:    arXiv 搜索 → 找不到相关论文

加入 WikiSource + KiwixSource:
  → WikiSource:     ~/wiki/concepts/ → 找到 maxwell-equations.md
                    → 包含 ∇·B 的 Lean 编码方案 + 之前试过的 tactic
  → KiwixSource:    Wikipedia → Gauss's law for magnetism →
                    → 提供物理背景和数学公式的精确表述
                    → LLM 注入后更准确地生成 Lean 代码
```

### 场景 B: 已知定理的复用

**目标:** 证明 `sin²θ + cos²θ = 1`

```
KnowledgeProver (v2):
  → 从头开始尝试 prove，需要多次 T2 编译

加入 WikiSource:
  → WikiSource: 搜索 trig_identities → 找到之前 proven 的版本
  → 直接注入完整证明代码
  → T2 一次通过
```

### 场景 C: 跨 session 知识累积

```
Session 1: 证明 "theorem add_comm (a b : Nat) : a + b = b + a"
  → KnowledgeProver 成功 → 证明写入 ~/wiki/entities/lean-proven-lemmas.md
  → 同时写入 ~/.omega/research_cache/proved_*.json

Session 2: 证明 "theorem add_assoc (a b c : Nat) : (a + b) + c = a + (b + c)"
  → KnowledgeProver research 阶段:
    1. WikiSource 命中 lean-proven-lemmas.md → add_comm 证明可见
    2. 注入 "已知的 add_comm 使用了 induction + rfl 模式"
    3. Proposer 建议 "参考 add_comm 的模式，尝试 induction on a"
  → 更快的收敛（而不是从零开始）
```

### 对比: 无 wiki vs 有 wiki

| 指标 | 无 wiki (v2) | 有 wiki + Kiwix (v3) |
|------|------------|-------------------|
| 首次证明时间 | 需 3-5 轮 T2 编译 | 2-3 轮（复用已知模式） |
| 跨定理知识迁移 | 无（每次独立） | 有（wiki 持久化） |
| 物理/数学公式背景 | LLM 自己的知识 | Kiwix Wikipedia 精确引用 |
| 已证明定理复用 | 仅 cache（易过期） | wiki 永久存档，可交叉引用 |
| 评审说服力 | "模型盲猜" | "引用已 proved 的 lemma + Wikipedia 定义" |

---

## 4. 实现计划

### Phase 1: dotenv 集成（~30 行）

```python
# omega/resource/config.py
# 在 load_config() 开头加入
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())          # CWD/.env
load_dotenv(Path.home() / ".omega" / ".env")  # ~/.omega/.env
```

### Phase 2: WikiSource（~80 行）

```python
# omega/research/sources/wiki.py
class WikiSource:
    def __init__(self, wiki_path="~/wiki"): ...
    def search(self, theorem_header) -> dict: ...
    def read_page(self, page_name) -> str: ...
```

### Phase 3: KiwixSource（~60 行）

```python
# omega/research/sources/kiwix.py
class KiwixSource:
    def __init__(self, base_url="http://localhost:8080"): ...
    def search(self, query) -> list[dict]: ...
    def get_page(self, zim_id, article_path) -> str: ...
```

### Phase 4: KnowledgeProver 集成

```python
# omega/research/prover.py
class KnowledgeProver:
    def __init__(self, ..., use_wiki=True, use_kiwix=True):
        self._wiki = WikiSource() if use_wiki else None
        self._kiwix = KiwixSource() if use_kiwix else None

    def _do_research(self, theorem):
        ...
        if self._wiki:
            wiki_result = self._wiki.search(theorem_header)
            # 合并到 KnowledgePackage
```

# LEAP Integration Roadmap for Omega-Architect

> **关联**: paper-leap-2606-03303 (arXiv), wiki/concepts/leap-omega-cross-analysis.md
> **状态**: 📋 计划 — P0 可立即启动（CPU-only 可实施）
> **核心验证**: LEAP (Google DeepMind 2026) 证明通用 LLM + Agentic Framework = SOTA 形式定理证明

---

## LEAP 三大核心可迁移模式

### 1️⃣ AND-OR DAG 分级记忆

**LEAP 做法**:
- DAG 的 OR 节点 = 开放目标/引理
- AND 节点 = 分解方案（所有子目标都证明则父目标也证明）
- 共享引理跨分支复用，避免指数爆炸

**Omega 现状**: 线性 DFS + Beam + Hybrid，无图结构

**实施**: `omega/engine/dag.py`

```python
# ── 核心数据结构 ──
@dataclass
class DAGNode:
    """Proof DAG node."""
    id: str
    theorem_stmt: str
    node_type: Literal["goal", "lemma", "decomposition", "definition"]
    children: list[str] = field(default_factory=list)  # subgoal IDs
    parents: list[str] = field(default_factory=list)
    proof_cache: str | None = None  # cached proof if proved
    status: str = "open"  # open | proving | proved | failed

@dataclass
class ProofDAG:
    """AND-OR proof DAG with hierarchical memoization."""
    nodes: dict[str, DAGNode] = field(default_factory=dict)
    root_id: str = ""

    def add_goal(self, stmt: str, node_type: str) -> str: ...
    def add_decomposition(self, parent_id: str, subgoals: list[str]) -> bool: ...
    def get_open_goals(self) -> list[DAGNode]: ...
    def mark_proved(self, node_id: str, proof: str): ...
    def is_acyclic(self, parent_id: str, child_id: str) -> bool: ...
    def shareable_lemmas(self, context: str) -> list[DAGNode]: ...
```

**验证**: 单定理多分支搜索时，引理复用率 >30%（LEAP 复现目标）

---

### 2️⃣ 交错了非形式化→形式化规划

**LEAP 做法**:
1. 直接证明（informal proof → Lean code → compile）
2. 如果失败 → Blueprint（非形式化分解 + Lean proof sketch）
3. Sketch 中仅新引理允许 `sorry`，主证明体必须 `sorry`-free
4. Lean 验证 sketch → 通过后添加为 AND 节点 → 递归证明子目标

**Omega 现状**: Rethlas 已有 Blueprint 但未独立为全局抽象

**实施**: `omega/engine/blueprint.py`

```python
@dataclass
class Blueprint:
    """Informal proof plan with formal sketch."""
    goal: str
    informal_steps: list[str]  # natural language reasoning chain
    formal_sketch: str          # Lean code with sorry for new lemmas
    new_lemmas: list[str]       # proposed lemma statements
    dependencies: list[Blueprint] | None = None

def generate_blueprint(goal: str, llm) -> Blueprint: ...
def validate_sketch(sketch: str, compiler) -> bool: ...
def decompose(goal: str, llm, compiler) -> Blueprint: ...
```

---

### 3️⃣ LLM Reviewer 作为搜索过滤器

**LEAP 做法**:
- 每次分解后，LLM 评审子目标是否：
  - 与父目标相关
  - 确实简化了问题
  - 提供了可行路径
- 不通过 → backtrack，用其他策略

**Omega 现状**: 无分解质量过滤器

**实施**: `omega/plan/reviewer.py`

```python
class DecompositionReviewer:
    """LLM-as-reviewer for decomposition quality.

    CPU mode: rule-based (goal length, keyword overlap, type match).
    API mode: DeepSeek-v4-flash (low cost, ~0.0005$/review).
    GPU mode: Local vLLM Qwen 7B (0 cost).
    """

    def review(
        self,
        parent_goal: str,
        subgoals: list[dict],
    ) -> ReviewResult:
        """:returns ReviewResult with accept/reject + reason."""
```

---

## P0 实施计划（CPU-only 可执行）

| # | 模块 | 文件 | 行数 | 依赖 |
|:-:|:-----|:-----|:----:|:-----|
| 1 | DAG 数据结构 | `omega/engine/dag.py` | ~200 | 无 |
| 2 | Blueprint 抽象 | `omega/engine/blueprint.py` | ~150 | 无 |
| 3 | Decomposition Reviewer | `omega/plan/reviewer.py` | ~150 | DAG |
| 4 | LEAP Agent pipeline | `omega/plan/leap_agent.py` | ~200 | DAG + Blueprint |
| 5 | Mode Router v2 | `omega/plan/mode_router.py` | ~200 | 以上全部 |

**总预计**: ~900 行新增代码，CPU-only 可开发+测试（仅 Reviewer 需 API）

**测试**: 每个模块 10-15 个单元测试 = ~60 测试，全部 mock compiler

---

## P1 计划（需 Lean Toolchain）

| # | 模块 | 文件 | 行数 | 依赖 |
|:-:|:-----|:-----|:----:|:-----|
| 6 | Lean-IMO-Bench 评测 | `omega/benchmark/lean_imo.py` | ~100 | hfpclawer |
| 7 | Lemma 缓存系统 | `omega/engine/lemma_cache.py` | ~200 | DAG + Embedding |
| 8 | MiniF2F benchmark upgrade | 优化 pipeline | ~100 | Lean toolchain |

---

## P2 计划（受益 GPU）

| # | 模块 | 文件 | 行数 | 依赖 |
|:-:|:-----|:-----|:----:|:-----|
| 9 | GPU 加速 DAG 并行展开 | `omega/gpu_layer/dag_scheduler.py` | ~200 | GPU + vLLM |
| 10 | EA-GRPO × LEAP 奖励融合 | 扩展 `compute_compile_reward` | ~50 | GPU |
| 11 | 跨论文 lemma 检索 | hfpclawer × lemma_cache 集成 | ~200 | HFpClawer |

---

## 与现有系统对接

```
现有系统                     LEAP 扩展
─────────                   ──────────
omega/loop/inner.py  ──────→ Direct formalization 步骤
omega/prover/       ──────→ Solver Agent 后端（Goedel/Rethlas/Archon）
omega/engine/hybrid.py ───→ 保留为直接证明阶段的后备
omega/plan/         ──new──→ LEAP Agent + Mode Router
omega/search/dec.py ──────→ EA-GRPO reward 合成
omega/resource/budget.py ─→ BudgetTracker 控制 LEAP rollout 成本
```

---

## 预期结果

| 指标 | 当前 | P0 | P1 | P2 |
|:-----|:----:|:--:|:--:|:--:|
| MiniF2F formal rate | ~40% | ~55% | ~65% | ~70% |
| Lean-IMO-Bench | 无 | 可以跑 | ~45% | ~55% |
| 复杂定理 (3+ steps) | ~20% | ~40% | ~50% | ~60% |
| Lemma 复用率 | 0% | 20% | 35% | 50% |
| Putnam 2025 | 0 | 3-4/12 | 6-8/12 | 10-12/12 |

# Ω-Archtect AGENTS.md — Three-Layer Unified Architecture

## Design Principles

1. **分层不分裂** — 每一层调用下一层，不是替代
2. **模式不取代** — 不同执行路径统一为"运行模式"，由路由层自动选择
3. **资源统一** — 所有模式共享 BudgetTracker + ConvergenceTracker + ModelRegistry
4. **入口统一** — 一个 CLI 入口 `omega prove`，模式选择对用户透明

## Repo Standards

| PEP | Rule | How |
|-----|------|-----|
| 621 | **Version source** | `pyproject.toml` only (当前 `0.2.0`); `__init__.py` reads via `importlib.metadata`+`tomllib` |
| 660 | **Editable install** | `pip install -e .` works (has `[build-system]`) |
| 8 | **Code style** | ruff (100 chars, double quotes); 100% English in .py |
| — | **.gitignore** | Covers: `__pycache__/ *.egg-info/ dist/ build/ .venv/ .env` |
| — | **Version mgmt** | `bash scripts/release.sh VERSION --push`; alignment=hotfix, no force tag |

> Templates: `~/.hermes/skills/software-development/version-management/`

## Versioning Convention

- **唯一版本源**: `pyproject.toml` 中 `[project].version` 为项目的唯一真实版本号
- **Git commit 版本**: 所有 git commit 中的版本号必须从 `pyproject.toml version` 派生。例如 `pyproject.toml` 中 `version = "0.2.0"`，则 commit 版本为 `v0.2.0`
- **不带 `v` 前缀的版本标签**: git tag 统一使用 `v<version>` 格式（如 `v0.2.0`），与 `pyproject.toml` 一一对应
- **重大变更时**：先更新 `pyproject.toml` 中的 `version`，再以此为准撰写 commit message 和创建 tag
- **工具辅助**：每次 `git commit` 前，用 `grep '^version' pyproject.toml | head -1` 确认当前版本

## Public-Release Sanitization (MANDATORY)

> ⛔ This repo has a **public origin** (GitHub `diamond2nv/omega-architect`, MIT license).
> Anything committed to `main` may become public. The NAS remote (`origin`, Forgejo) is
> private — push sensitive-only changes there, never to the public GitHub remote.

### What must NEVER appear in tracked files

| Category | Rule | Example placeholder |
|----------|------|---------------------|
| Private LAN IPs | `192.168.0.x`, `10.x`, `172.16-31.x` | `<nas-host>` / `localhost` |
| Machine home paths | `/home/<real-user>/...` | `os.path.expanduser("~/...")` / `<project-dir>` |
| Internal machine codenames | HUAWEI / Speaker / WSL hostnames in public docs | generic "LAN peers" |
| Personal emails | real user emails in docs/examples | `dev@example.com` |
| Internal handover docs | WSL-HERMES-HANDOVER-style operational manuals | keep out of public repo |
| API keys / secrets | `sk-...`, real tokens | env vars only (`.env` is gitignored) |

### Rules

1. **Env-ize machine-specific defaults**: `OLLAMA_HOST`, `VLLM_MODEL_PATH`,
   `LEAN_LSP_MCP`, `VLLM_PYTHON` — read from env with `os.path.expanduser("~/...")`
   fallback. Never hard-code `/home/<user>/...` or LAN IPs in code.
2. **Default parameters**: use `None` + expanduser, or `shutil.which()` — never a
   literal machine path.
3. **Commit messages are public too**: neutral wording, no real names / IPs / emails.
4. **Before `git push` to public remote**: run
   `git ls-files | xargs grep -nE "192\.168\.|/home/<real-user>|HUAWEI|Speaker"` and
   confirm zero hits. Also re-scan for `172.26.` / `172.2[0-9].` WSL gateway ranges.
5. **No archives**: `*.zip` / `*.tar.gz` of the repo must not be tracked (gitignored).
6. **Credits**: any fused/inspired open-source code is credited in README
   Acknowledgements (never omit attribution — MIT/Apache obligations).
7. **Internal-only changes**: push to NAS Forgejo (`origin`) only; public release is a
   separate, deliberate step (GitHub remote, reviewed).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI / API 统一入口                                                  │
│  omega prove / omega bench / omega config                           │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────────┐
│  Layer 3: Orchestration (多 Agent 状态机)  📋 Planned                │
│  ─────────────────────────────────────────────                       │
│  适用: hard 定理、需分解的复杂目标                                    │
│  Orchestrator (delegate_task):                                      │
│    analyze_query → generate_blueprint → prove_lemmas →               │
│    refine_blueprint → synthesize_result                             │
│  8 原语: apply_lemma / rewrite_goal / induction /                   │
│          case_split / calc_chain / search_lemma /                    │
│          extract_proof / fallback_decompose                         │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  调用
┌───────────────────────────▼─────────────────────────────────────────┐
│  Layer 2: Proof Engine（证明引擎）                                    │
│  ──────────────────────────────────                                  │
│  Mode A: Dialogue ✅            Mode B: Sampling ✅                  │
│  (omega/loop/inner.py)         (omega/prover/go_prover.py)          │
│  Mode C: Hybrid ✅             Mode Router 📋 Planned               │
│  (omega/engine/hybrid.py)
│                                                                     │
│  共享基础设施:                                                       │
│  ├── P0 Search Aggregator ✅ (omega/search/aggregator.py)            │
│  ├── P1 Error Classifier ✅ (omega/classifier/)                      │
│  ├── CompileGate ✅ (omega/loop/compile_gate.py)                     │
│  └── ErrorMemory ✅ (omega/loop/error_memory.py)                     │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  调用
┌───────────────────────────▼─────────────────────────────────────────┐
│  Layer 1: Hardware & Resource（统一资源抽象） ✅                      │
│  ─────────────────────────────────────────                            │
│  ┌──── Remote API ──────────┐  ┌──── Local GPU ──────────┐          │
│  │ DeepSeek API (flash/pro) │  │ vLLM (Goedel-Prover-V2) │          │
│  │ API key → 自动连接       │  │ Ollama / Transformers   │          │
│  └──────────────────────────┘  └─────────────────────────┘          │
│                                                                     │
│  Cross-cutting:                                                     │
│  ├── BudgetTracker ✅ (omega/resource/budget.py)                     │
│  ├── ConvergenceTracker ✅ (omega/resource/tracker.py)               │
│  ├── ModelRegistry ✅ (omega/resource/model_registry.py)            │
│  └── GPU Layer ✅ (omega/gpu_layer/)                                │
└─────────────────────────────────────────────────────────────────────┘
```

## Layer 1: Hardware & Resource Abstraction ✅

### GPU Layer (`omega/gpu_layer/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `detector.py` | GPU/VRAM/CUDA/Ollama/vLLM 检测 | ✅ built |
| `backends.py` | 三后端统一接口 (VLLM/Ollama/Transformers) | ✅ built |
| `scheduler.py` | 全局调度器 GPUScheduler（单例） | ✅ built |

**Auto-routing priority**: vLLM running → GPU available → Ollama → Transformers

```bash
gpu-cli status           # 检测可用硬件
gpu-cli start goadel     # 启动 vLLM server
gpu-cli generate ...     # 直接生成
```

**Goedel-Prover-V2 本地推理**:
- Repo: https://github.com/Goedel-LM/Goedel-Prover-V2
- vLLM server on :8001, model `Goedel-Prover-V2-8B`
- WSL params: `gpu_memory_utilization=0.85, max_model_len=4096, dtype=bfloat16, enforce_eager=True`
- ⚠ Max model length 4096 (vs official 40960) due to WSL 24GB RAM limit
- Cache path: `~/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B/` (24GB weights)

### Model Registry (`omega/resource/model_registry.py`)

Single source of truth for all model pricing, capabilities, and routing.

```python
from omega.resource.model_registry import ModelRegistry
registry = ModelRegistry()
info = registry.get_model("deepseek-v4-flash")
# info.model, info.pricing (input/output per token), info.max_context
```

### Budget Tracker (`omega/resource/budget.py`)

Four-dimensional budget tracking: tokens / cost (USD) / time (seconds) / attempts.

```python
from omega.resource.budget import BudgetTracker
bt = BudgetTracker(max_cost=0.50, max_time=300, max_attempts=50)
bt.check()        # raises BudgetExhausted if exceeded
bt.consume(tokens=100, cost=0.001, time=2.5)
bt.summary()      # returns usage dict
```

### Convergence Tracker (`omega/resource/tracker.py`)

Epoch-based convergence monitoring. Tracks error count across rounds and detects stuck/diverging states.

```python
from omega.resource.tracker import ConvergenceTracker
ct = ConvergenceTracker(window=5, threshold=0.05)
ct.record_epoch(n_errors=3, proof_length=120, elapsed_s=10.5, errors=[...])
ct.is_stuck()     # True if errors not decreasing over window
ct.is_diverging() # True if errors consistently increasing
```

## Layer 2: Multi-Path Trajectory Exploration

Frames theorem proving as a **search over proof trajectories**,
inspired by Tree-of-Thoughts (ToT), AlphaZero/MCTS, and beam search.

### Core Abstractions

| Concept | Type | Description |
|---------|------|-------------|
| **ProofState** | `dataclass` | theorem + code + errors + goals + depth + value |
| **ProofAction** | `dataclass` | type + content (tactic/code) + confidence |
| **Trajectory** | `dataclass` | ordered steps + success + elapsed + budget |
| **SearchStrategy** | `ABC` | `run(theorem) → Trajectory` |

```python
from omega.engine import get_strategy, list_strategies
from omega.engine.trajectory import ProofState, ProofAction, Trajectory
```

### Search Strategies

| Strategy | Algorithm | Module | When to use |
|----------|-----------|--------|-------------|
| **DFS** | Dialogue | `omega/loop/inner_loop` | Easy/medium theorems, single trajectory |
| **Beam** | Sampling | `omega/prover/go_prover` | Multiple valid approaches, parallel candidates |
| **Hybrid** | Multi-Path | `omega/engine/hybrid` | Hard theorems, DFS first → beam on stuck |

---

### DFS (Dialogue) — Single Trajectory 🎯 ✅

Single deep path with error-driven backtracking.

```
State → LLM generates action → compile → if error: refine → repeat
```

- **Algorithm**: Depth-First Search
- **Good for**: 3-20 round proofs, linear token cost
- **Risk**: Local optima, repetitive error loops
- **Stuck detection**: `ConvergenceTracker` — dead loop / diverging / no progress

```python
from omega.loop import inner_loop, InnerLoopConfig
result = inner_loop("theorem t : 1 + 1 = 2 := by", config=InnerLoopConfig(max_rounds=50))
```

**Three-layer error feedback (P1)** ✅ — `omega/classifier/`

```
error → Layer 1 (regex, <1ms) → Layer 2 (NLP 4-algo) → Layer 3 (LLM Judge, cached)
```

---

### Beam (Sampling) — Parallel Trajectories ✅

Generates K independent trajectories, scores via compile, keeps top candidates.

```
Generate N candidates → compile all → correct failures → return first success
```

| Module | Purpose |
|--------|---------|
| `go_prover.py` | Goedel-style parallel sampling + 2-round correction |
| `re_prover.py` | Rethlas — blueprint decomposition + recursive sub-goal |
| `ar_prover.py` | Archon — multi-strategy integration + ProgressCritic |
| `ensemble.py` | Run three prover variants, elect best by ensemble voting |

**Search Aggregator (P0)** ✅ — `omega/search/aggregator.py`

```python
from omega.search.aggregator import SearchAggregator
agg = SearchAggregator()
results = agg.search("commutativity of addition on ℕ")
```

---

### Hybrid (Multi-Path Trajectory Exploration) ✅

Two-phase search: **DFS first, Beam on stuck**.

```
Phase 1: DFS trajectory (Dialogue, up to 20 rounds)
  ├── success → ✅ done (zero overhead for easy theorems)
  └── stuck → enter Phase 2

Phase 2: Beam search (Sampling, 6-8 candidates, second opinion)
  ├── success → ✅ done
  └── fail → extract best candidate

Phase 3: Second DFS trajectory (with beam candidate + errors as context)
  ├── success → ✅ done
  └── fail → return best result
```

**Why this order:** Controlled experiments showed the old "Sampling → Dialogue" order was pure overhead — Phase 1 Sampling never found a direct proof, all successes came from Dialogue. v2 flips to DFS first: easy theorems pass in 3-5 rounds with zero overhead, while hard theorems still get multi-path diversity when stuck.

```python
from omega.engine import run_hybrid_v2, HybridV2Config
from omega.engine import get_strategy

# Low-level API
result = run_hybrid_v2("theorem t : 1+1=2 := by", HybridV2Config(phase1_rounds=15))

# High-level API
strategy = get_strategy("hybrid")
trajectory = strategy.run("theorem t : 1+1=2 := by")
# trajectory.success, trajectory.proof, trajectory.steps
```

---

### MCTS / UCB1 — Built, Not Wired 🔴

`omega/search/tree.py` implements a proof search tree with MCTS-style selection
(`select_best("ucb1")`, `_select_ucb1(c=1.4)`, plus `best_value` / `most_visits` /
`deepest_unexplored`), but **no prover calls it**: `prover/{ar,re,go}_prover.py`
import only `GoalState`, and `omega/engine/trajectory.py` ships `DFSStrategy`
alone — i.e. *tree data structure exists, tree search does not*.

- **Feasibility is layer-dependent**: token-level search → MCTS not viable
  (see `docs/plan/omega-policy-learning-plan.md` §533); **tactic/lemma level**
  (discrete, enumerable) → viable, and that is exactly what `ProofTree` models;
  continuous hyper-parameters → Optuna/BO (see `docs/technical-review-optuna-hpo-2026-09.md`).
- **Wiring plan** (3 steps: `MCTSStrategy` on the existing `SearchStrategy` ABC →
  cheap value from `CompileGate`/`ErrorClassifier` instead of LLM rollouts →
  `ProofErrorMemory` as search prior): `docs/omega-mcts-wiring-plan-2026-09.md`
- Background review: `docs/technical-review-mcts-hybrida-2026-09.md`

### Mode Router 📋 Planned

```python
class ModeRouter:
    """Select optimal search strategy based on theorem difficulty + resources + history."""
    def select(self, theorem, context) -> SearchStrategy:
        # DFS for easy + API available
        # Beam for easy + local GPU
        # Hybrid for previously failed theorems
```

## Layer 3: Multi-Agent Orchestration 📋 Planned

Entry: `analyze_query` → states: `generate_blueprint → prove_lemmas → refine_blueprint → synthesize_result`

Each state is a (`role`, `goal`, `toolsets`) triple dispatched via `delegate_task`.

**8 Primitives**:
| # | Primitive | Trigger | Action |
|---|-----------|---------|--------|
| 1 | `apply_lemma` | `apply` keywords | Apply existing lemma from Mathlib |
| 2 | `rewrite_goal` | `rw` / `simp` | Rewrite target using known identities |
| 3 | `induction` | `∀ n:ℕ` or recursive structure | Structural induction |
| 4 | `case_split` | `h : A ∨ B` or `if ...` | Case analysis |
| 5 | `calc_chain` | equality chain | `calc a = b := ...` |
| 6 | `search_lemma` | unknown identity | leansearch / loogle query |
| 7 | `extract_proof` | previous similar problem | Adapt known proof structure |
| 8 | `fallback_decompose` | complex goal | Delegate to decompose_task |

## CLI Reference

```bash
omega prove "<theorem>"            # auto-mode (Router decides)
omega prove "<theorem>" --mode dialogue   # force Mode A
omega prove "<theorem>" --mode sampling --num-samples 8
omega config show                  # show budget + lean toolchain
omega config show --lean           # lean toolchain only
omega init --force                 # re-detect lean + mathlib
gpu-cli status                     # GPU hardware status
```

## Lean Compilation Setup

- **Compile channel**: `lake env lean --stdin` (lean-paper-plane project + Mathlib)
- **Mathlib cache**: 6.7GB / ~8109 oleans
- **Compile time**: ~2.5s/theorem
- **Error categories**: 13 classes (PLAN/CODE/SEARCH/OTHER/NO_ERROR etc.)
- **Diagnostics**: Full structured errors with line numbers

```python
from omega.loop.compile_gate import CompileGate
gate = CompileGate()
result = gate.compile("import Mathlib\ntheorem t : 1=1 := rfl")
# result.success, result.errors, result.error_class, result.line
```

See `omega/resource/lean_config.py` for auto-discovery of Lean toolchain.

## Test Suite

```bash
pytest tests/      # 150/150 passed
```

Tests cover: CompileGate (13 error classes), error classifier, convergence tracker, budget tracker, prover modules (Goedel/Rethlas/Archon), search aggregator, three-layer classifier.

## Key Design Decisions

1. **Loop Engineering > Prompt Engineering** — Inner loop (compile → fix → repeat) outperforms single-shot prompting
2. **Compile is a local gate** — NOT a tool_call; synchronous, no API cost, ~2.5s latency
3. **MCP integration** — Lean LSP tools (loogle, leansearch, goal inspection) through MCP protocol
4. **Search limit** — max 2 search tool_calls per round to prevent infinite search loops
5. **ErrorMemory** — Cross-theorem error→fix learning in JSONL, Jaccard similarity fallback
6. **P0/P1 separation** — Search aggregator and error classifier are shared Layer 2 infra, not Mode-specific

## Licensing

- Ω-Architect: Apache 2.0
- Goedel-Prover-V2: Apache 2.0
- Rethlas: Apache 2.0
- Archon: Apache 2.0
- Mathlib: Apache 2.0
- aesop: MIT

## Development Progress

| Feature | Status | Notes |
|---------|--------|-------|
| Layer 1 (GPU/Resource) | ✅ built | gpu_layer/, resource/ |
|  Mode A (Dialogue) | ✅ built | omega/loop/ |
|  Mode B (Sampling) | ✅ built | omega/prover/ |
|  **Mode C (Hybrid)** | **✅ built** | **omega/engine/hybrid.py** |
|  P0 Search Aggregator | ✅ built | omega/search/aggregator.py |
| P1 Three-Layer Classifier | ✅ built | omega/classifier/ |
| P2 Convergence Detection | ✅ built | Fixed stuck/diverging detection |
| P3 Budget Tracking | ✅ built | tool_call-aware counting |
| EA-GRPO Reward | ✅ built | omega/search/dec.py, integrated in LUFFY |
|  Mode Router | 📋 planned | |
| Layer 3 (Orchestration) | 📋 planned | |
| CLI unification | 📋 planned | |
| LEAP-Style Multi-Agent (P0) | 📋 planned | See docs/leap-integration-roadmap.md |
| AND-OR DAG State Machine (P0) | 📋 planned | Core LEAP-style data structure |
| LLM Reviewer (P0) | 📋 planned | Decomposition quality filter |
| Blueprint Abstract Layer (P0) | 📋 planned | Extract from Rethlas |
| Lean-IMO-Bench Eval (P1) | 📋 planned | `omega bench lean-imo` |
| Lemma Cache (P1) | 📋 planned | Cross-theorem reuse |

### LEAP Integration Roadmap

See `docs/leap-integration-roadmap.md` for the full cross-analysis and P0-P2 fusion plan.

Key insight from LEAP (arXiv 2606.03303, Google DeepMind 2026):
- General LLM + Agentic Framework = SOTA (Putnam 12/12, Lean-IMO-Bench 70%)
- Three patterns to adopt: AND-OR DAG memoization, interleaved informal→formal planning, LLM-as-reviewer
- Omega unique advantage: EA-GRPO reward (edit-distance-aware) + DeepSeek API (100x cheaper than Gemini)

See [`docs/plan/architecture/unified-architecture-v1.md`](docs/plan/architecture/unified-architecture-v1.md) for full design.

## Cache Path Gotchas

- Goedel model weights: `~/.cache/huggingface/hub/models--Goedel-LM--Goedel-Prover-V2-8B/` (24GB)
- HF_HOME=/mnt/d/home/.cache/ — that path has ONLY config/tokenizer (16MB), NOT weights
- Always use `~/.cache/huggingface/hub/...Goedel-Prover-V2-8B...` for vLLM serving
- WSL vLLM lives in `miniconda3` env, NOT in Hermes venv — hybrid strategies need explicit python path

---

## Design Patterns

### Compiler World Model (Era of Experience §4)

**Core idea**: Each layer of a compiler models the physical reality of its abstraction level.
In omega-architect, the **Lean 4 compiler** serves as the mathematical world model — every `compile()` call queries the "physics" of Lean's dependent type theory.

| OSI Layer | Compiler | omega-architect Mapping |
|:---------:|:---------|:------------------------|
| 6 — Language | Lean 4 (`lake env lean --stdin`) | `CompileGate` (omega/loop/compile_gate.py) |
| 5 — IR | Lean internal core/elaborator | Elaboration + type inference |
| 4 — Assembly | Kernel type-checking | `#check` / `#reduce` diagnostics |
| 3 — Linking | Mathlib olean cache | 6.7GB / ~8109 oleans |
| 2 — OS | `lake` package manager | `omega/resource/lean_config.py` |
| 1 — Silicon | CPU + GPU | `omega/gpu_layer/` |

**How this pattern manifests in the repo**:

1. **Compile-as-execution** — Unlike most ML loops where the model samples text and scoring is a separate LLM call, omega-architect's inner loop compiles Lean code through the full toolchain. The compiler *is* the world model: it faithfully simulates whether a tactic string actually proves the theorem.
2. **Layer-7 (domain) interface** — The three search strategies (DFS/Beam/Hybrid) and the Layer-3 Orchestrator sit at the domain layer, generating Lean programs. The compiler at Layer 6 executes them and returns typed errors.
3. **Cross-layer fidelity** — Error messages from the Kernel (Layer 4) propagate back up to the LLM (Layer 7) via `CompileGate`, forming a tight compile–error–fix loop.
4. **Cache as physical layer** — `Mathlib` olean files (~6.7GB) act as a pre-compiled physical substrate, analogous to a linked binary at Layer 3.

**Relation to Grounded Reward**: The compiler world model provides the natural grounded reward signal — pass/fail from `CompileGate` is the physical measurement that anchors the NN-driven EA-GRPO policy (see below).

See: `compiler-world-model-design` skill for the full seven-layer OSI-style design pattern.

---

### Grounded Reward (Era of Experience §3)

**Core idea**: Reward functions should be anchored in objective signals (physics / simulation / formal verification), not just human labels. A **two-layer architecture** — NN policy + grounded verifier — prevents reward hacking and scales to hard problems.

**How this pattern manifests in the repo**:

| Layer | omega-architect Component | Description |
|:------|:--------------------------|:------------|
| **Top (NN)** | `EA-GRPO` → `omega/search/dec.py` | Edit-distance-aware policy gradient. DeepSeek V4 / Goedel-Prover-V2 as the policy network. Learns which tactic sequences yield successful proofs. |
| **Bottom (Grounded)** | `CompileGate` → `omega/loop/compile_gate.py` | The Lean 4 kernel's pass/fail verdict. ~2.5s per compile, zero API cost, deterministic. This is the physical "ground truth" of the theorem-proving domain. |

**Bidirectional calibration loop**:

```
        EA-GRPO (NN policy) — samples tactic sequences
               │
               ▼
        CompileGate — Lean kernel type-checks the proof
               │
               ▼
        pass/fail ← grounded reward signal (binary + error class)
               │
               ▼
        ConvergenceTracker — detects stuck/diverging trajectories
               │
               ▼
        EA-GRPO update — policy adjusted by edit-distance + compile result
```

**Key design decisions rooted in this pattern**:

1. **Compile is NOT a tool call** — It's a synchronous local gate (~2.5s). This makes the grounded signal *cheaper and faster* than any alternative (LLM-as-judge, human review), satisfying the "trusted data first" rule.
2. **Three-layer error classifier** — When the compiler rejects a proof (grounded = fail), the classifier (regex → NLP → LLM Judge) extracts structured error semantics, feeding the NN policy with *why* it failed, not just *that* it failed.
3. **ErrorMemory** — Cross-theorem error→fix pairs in JSONL with Jaccard fallback. This is a form of *offline grounded experience replay* — the NN learns from past compiler rejections without re-compiling.
4. **Hybrid strategy design** — DFS first for easy theorems (zero overhead), beam on stuck (exploration diversity). This mirrors the grounded-reward pattern's "trusted path first, NN exploration second" philosophy.

**Why this matters**: Without the Lean compiler as a grounded signal, omega-architect would need either (a) an expensive LLM-as-judge for every proposed tactic, or (b) human annotation of proof quality — both of which are the scalability bottlenecks that the Grounded Reward pattern specifically addresses.

See: `grounded-reward-design` skill for the full two-layer design pattern, and note its companion `dead-end-registry-design` for how failed compiler queries are stored as structured negative experience.

---

### Cross-Pattern Interaction: Compiler World Model → Grounded Reward

The two patterns form a **virtuous cycle** in omega-architect:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Compiler World Model (the environment)                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐   │
│  │ Lean 4 Code  │ →  │ CompileGate  │ →  │ pass/fail + errors   │   │
│  │ (Domain L7)  │    │ (Kernel L4)  │    │ (Grounded signal)    │   │
│  └──────────────┘    └──────────────┘    └───────────┬──────────┘   │
│                                                       │              │
│  Grounded Reward (the learning signal)                │              │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────▼──────────┐   │
│  │ EA-GRPO      │ ←  │ Trajectory   │ ←  │ Error Memory +       │   │
│  │ (NN Policy)  │    │ Scoring      │    │ Convergence Tracker  │   │
│  └──────────────┘    └──────────────┘    └──────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

- The **compiler world model** generates the signal (pass/fail)
- The **grounded reward** framework uses that signal to train the policy
- Policy improvements produce better Lean code, *closing the loop*

This pairing is what enables omega-architect's central claim: *a loop-engineering approach that outperforms single-shot prompting at 1/100th the API cost* — the compiler is free to query, and its binary signal is maximally informative for the grounded reward layer.

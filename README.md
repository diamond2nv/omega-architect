# Ω-Architect

[![License](https://img.shields.io/github/license/diamond2nv/omega-architect)](https://github.com/diamond2nv/omega-architect/blob/master/LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://github.com/diamond2nv/omega-architect/blob/master/pyproject.toml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/diamond2nv/omega-architect)

**Open Formal Theorem Proving for Physics, Optics, and Quantum Systems**

> ⚠️ **Alpha: Core functionality works. APIs may change as we stabilize the feature set.**

Ω-Architect is a **heavyweight harness** — an open-source formal theorem proving framework
that combines multiple proof engines, an orchestration layer, and hardware-aware resource
management into one unified system for:

- **Quantum optics**: Whispering-gallery-mode resonators, Brillouin scattering, optical frequency combs, optical clocks
- **Physics**: Quantum electrodynamics (QED), quantum optics, condensed matter
- **Engineering verification**: NV center sensing, steel rail crack detection
- **Competition math**: IMO, USAMO, Putnam levels

It is the product of years of iterative agent-driven development: a three-layer unified
architecture (hardware abstraction → proof engines → orchestration) with a hybrid
LLM + Lean-4 verification pipeline, MCTS-style trajectory search, error memory, and
self-contained verification gates.

## License

MIT — see [LICENSE](LICENSE).

## Architecture

```
User Query
    │
    ▼
[Orchestrator] ── state machine (AGENTS.md-driven)
    │
    ├── Skill Selector  (8 primitives)
    ├── Decomposer      (divide into sub-goals)
    │
    ├── T1 Verifier     (LLM, ~5s) ─ fast check
    ├── T2 Verifier     (Lean, ~30s) ─ compiler verify
    │
    └── Search Layer    (leansearch, loogle, Mathlib)
```

Three-layer design:

1. **Layer 1 — Hardware & Resource**: GPU detection, unified backends (vLLM / Ollama /
   Transformers), BudgetTracker, ConvergenceTracker, ModelRegistry. Model routing is
   configurable via environment (`OLLAMA_HOST`, `VLLM_MODEL_PATH`, ...).
2. **Layer 2 — Proof Engines**: Dialogue / Sampling / Hybrid modes over a shared
   **MCTS-style search tree** (`omega/search/tree.py` — UCB1 node selection for
   exploration-exploitation balance), running trajectory search over proofs with
   error-driven backtracking and error memory. Engine design is inspired by
   Tree-of-Thoughts, AlphaZero/MCTS, and beam search.
3. **Layer 3 — Orchestration** *(planned)*: multi-agent state machine for hard theorems.

## Verification Gates

Self-contained pytest gates (B10/B11/C01/C02/F01/F02) validate core invariants without
external dependencies — register markers in `pyproject.toml`, plain asserts, no gate lib.

## Quick Start

**Python ≥ 3.11.** Install the CLI the way that matches how you work — `uv` is recommended,
because the tool then lives in its own environment:

```bash
# 1) From the public repo, pinned to a tag (reproducible)
uv tool install "omega-architect @ git+https://github.com/diamond2nv/omega-architect@v0.2.2"

# 2) From the wheel attached to the GitHub release
uv tool install "https://github.com/diamond2nv/omega-architect/releases/download/v0.2.2/omega_architect-0.2.2-py3-none-any.whl"

# 3) From a checkout (development)
git clone https://github.com/diamond2nv/omega-architect && cd omega-architect
uv pip install -e ".[all]"        # or: pip install -e .
```

```bash
omega init                                                 # writes omega.toml
omega status                                               # model router health + history
omega prove 'theorem add_zero (n : ℕ) : n + 0 = n := by'    # generate → compile → verify
omega bench                                                # theorem benchmark (T2 pass rate)
```

**Prerequisites and cost**

- **Lean 4 toolchain** — required for compiler-backed verification (T2 / CompileGate):
  install [elan](https://github.com/leanprover/elan) and a Lean 4 release, keep `lean` on
  `PATH`. `omega init` discovers it; `omega init --skip-lean` configures without discovery,
  and `scripts/mcts_lean_smoke.py --generator fixed` exercises the Lean path without an LLM.
- **LLM backend** — proof generation needs one, and it is the main cost driver: a remote API
  key (default model `deepseek/deepseek-v4-flash`) or a local Ollama / vLLM model via
  `--model`. `--samples`, `--rounds`, `--timeout` and the budget section of `omega.toml`
  bound each run. A **local GPU backend exists to cut API token spend** — the size of that
  saving depends on model strength and the workload, so measure it (same theorem set, both
  backends) and tune, rather than assuming a fixed ratio.
- Runs are **not free**: every `prove` spends LLM tokens plus Lean compile time.

## Acknowledgements

Ω-Architect is an integrated fusion of many excellent open-source projects and ideas.
We are deeply grateful to:

| Project / Work | Contribution |
|---|---|
| [Lean 4](https://github.com/leanprover/lean4) + [Mathlib](https://github.com/leanprover-community/mathlib4) | The formal verification foundation (T2 verifier, lemmas) |
| [Goedel-Prover-V2](https://github.com/Goedel-LM/Goedel-Prover-V2) | Local 8B proof model (vLLM backend) |
| [lean-lsp-mcp](https://github.com/leanprover/lean-lsp-mcp) | Lean Language Server ↔ MCP bridge |
| [langchain-ollama](https://github.com/langchain-ai/langchain) / [Langfuse](https://langfuse.com) | LLM interface & tracing |
| [vLLM](https://github.com/vllm-project/vllm) / [Ollama](https://ollama.com) | Local inference backends |
| [DeepSeek](https://www.deepseek.com) | Remote API backend |
| [Tree-of-Thoughts](https://github.com/princeton-nlp/tree-of-thought-llm) / AlphaZero / beam search | Trajectory exploration inspiration |
| Python scientific ecosystem | numpy, pytest, httpx, etc. |

If we missed your project, please open an issue — we want to credit every building block.

## Repo Structure

```
omega/            Main package
├── engine/       Trajectory engine over proofs (ToT / AlphaZero-MCTS / beam inspired)
├── search/       Proof search trees (MCTS-style UCB1 selection) + multi-source aggregator
├── loop/         Verification loop: dialogue-mode proving, compile gates, error memory
├── prover/       Sampling-based proof generation (local / remote model backends)
├── classifier/   Error classification for proof attempts
├── resource/     BudgetTracker · ConvergenceTracker · ModelRegistry
├── gpu_layer/    GPU detection & unified backends (vLLM / Ollama / Transformers)
├── learn/        Policy learning: trainable proof strategies (LLM / router policies, RL)
└── cli/          `omega prove` / `omega bench` / `omega config` entry points
omega-core/       Core registry & shared infrastructure
omega-plugin/     Plugin interface
scripts/          Operational scripts
benchmarks/       Benchmarks
tests/            Self-contained verification gates
docs/             Design & planning documents
```

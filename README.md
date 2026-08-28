# Ω-Architect

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
LLM + Lean-4 verification pipeline, trajectory search, error memory, and self-contained
verification gates.

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
2. **Layer 2 — Proof Engines**: DFS (dialogue), Beam (sampling), Hybrid — trajectory
   search over proofs with error-driven backtracking and error memory.
3. **Layer 3 — Orchestration** *(planned)*: multi-agent state machine for hard theorems.

## Verification Gates

Self-contained pytest gates (B10/B11/C01/C02/F01/F02) validate core invariants without
external dependencies — register markers in `pyproject.toml`, plain asserts, no gate lib.

## Quick Start

```bash
pip install -e .
omega prove "∀ x : ℝ, x^2 ≥ 0"
omega bench
omega config --show
```

Requires a Lean 4 toolchain (`~/.elan`) for compiler-backed verification; LLM backends
are optional (DeepSeek API / local Ollama / vLLM).

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
omega/            Main package (proof engines, search, resource)
omega-core/       Core registry & shared infrastructure
omega-plugin/     Plugin interface
scripts/          Operational scripts
benchmarks/       Benchmarks
tests/            Self-contained verification gates
docs/             Design & planning documents
```

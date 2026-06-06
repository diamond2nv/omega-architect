# License Compliance

## Overview

Ω-Architect is [Apache 2.0](../../LICENSE). This document tracks all third-party code influences,
design inspirations, and runtime dependencies to ensure full license compliance.

All code in this repository is original, written from scratch in Ω-Architect's own Python style.
No source files from third-party repositories have been copied verbatim. Design patterns and
algorithmic approaches are inspired by academic papers and existing open-source projects, all
under Apache 2.0 or compatible licenses.

---

## Third-Party Code & Design Inspiration

### Goedel-Prover-V2

- **Source**: https://github.com/Goedel-LM/Goedel-Prover-V2
- **License**: Apache 2.0
- **Paper**: [Goedel-Prover-V2: Parallel Sampling + Self-Correction](https://arxiv.org/abs/2606.06468)
- **Files in Ω**:
  - `omega/search/proposer.py` — `Proposer` class with `strategy="goedel"` that generates N independent tactic attempts via parallel LLM sampling
  - `omega/search/tree.py` — `ProofTree`, `SearchNode` with `expanded_by` tracking to record which strategy expanded each node
- **Design inspiration**:
  - Parallel sampling: generate multiple independent tactic proposals from the same goal state
  - Self-correction: failed tactic branches are tracked and pruned; alternative paths are explored
  - Search tree structure with node status tracking (UNEXPLORED → IN_PROGRESS → VERIFIED / FAILED / PRUNED)
  - MCTS-style node selection (UCB1) for exploration-exploitation balance
- **What was replicated**:
  - The strategy of generating `num_samples` independent tactic suggestions per goal
  - The search tree abstraction tracking node status, parent/child relationships, and proof paths
  - The concept of T2 verification as the authoritative gate on proof correctness
- **What differs**:
  - Goedel-Prover-V2 uses a dedicated Goedel model for tactic generation; Ω uses a pluggable LLM via `generate_fn` callback (default: Hermes Agent via `delegate_task`)
  - Goedel-Prover-V2 uses `lake exe repl` for Lean REPL interaction; Ω's T2 uses `lake env lean --stdin` (see `omega/verify/t2_real.py`)
  - The proposer interface is abstracted as a `TacticGenerator` callback type, making it pluggable without code changes
  - No Goedel model weights, checkpoints, or inference code are included
  - The search tree is pure Python with no Goedel-specific serialization format
- **Compliance note**: All design patterns are reimplemented from scratch based on the paper's descriptions. No source code was copied.

---

### Rethlas

- **Source**: https://github.com/PKU-FrenzyMath/Rethlas (PKU FrenzyMath)
- **License**: Apache 2.0
- **Files in Ω**:
  - `omega/search/proposer.py` — `Proposer` class with `strategy="rethlas"` for blueprint-based decomposition
- **Design inspiration**:
  - Blueprint-based decomposition: break complex goals into structured subgoals before generating tactics
  - Fewer but higher-quality suggestions per goal (`num_samples // 2` vs Goedel's full sampling)
  - Hierarchical proof structure with goal decomposition as a separate phase
- **What was replicated**:
  - The concept of a "blueprint" strategy that produces structured, lower-volume tactic suggestions
  - The integration of goal decomposition as a proposer-level concern
- **What differs**:
  - Rethlas uses Codex CLI for LLM interaction — Ω does not use Codex CLI at all
  - Ω's Rethlas strategy is simply a parameter change to `make_llm_proposer` (fewer samples), not a separate codebase
  - No Rethlas-specific retrieval or embedding infrastructure is included
  - Blueprint generation is done via the same LLM `generate_fn` as Goedel/Archon strategies
  - No Codex, no OpenAI-specific APIs, no repository-level context indexing
- **Compliance note**: The Rethlas strategy is implemented as a ~5-line parameter change in the `Proposer._build_generator` method. All code is original.

---

### Archon

- **Source**: https://github.com/PKU-FrenzyMath/Archon (PKU FrenzyMath)
- **License**: Apache 2.0
- **Files in Ω**:
  - `omega/search/proposer.py` — `Proposer` class with `strategy="archon"` for draft-first proof generation
- **Design inspiration**:
  - Multi-strategy ensemble: combine multiple proof strategies in one search
  - Draft-first: generate a full proof draft before refinement
  - Progress monitoring: track search statistics across strategies (see `ProofTree.stats()`)
- **What was replicated**:
  - The concept of composable proof strategies selected at runtime
  - The draft-first approach where a complete proof is attempted before decomposing
- **What differs**:
  - Archon uses Codex CLI — Ω does not use Codex CLI
  - Ω's Archon strategy is a single parameter (`strategy="archon"`) in the `Proposer` class
  - No ensemble orchestration layer or inter-strategy communication protocol
  - Progress monitoring is done via `ProofTree.stats()` and `SearchNode` metadata, not via a separate Archon monitoring system
- **Compliance note**: The Archon strategy is implemented as a strategy parameter selection. All code is original.

---

### lean-paper-plane (T2 Real Compile Target)

- **Source**: https://github.com/.../lean-paper-plane
- **Impact on Ω**: The project at `~/lean-paper-plane` is used as a **compile-time environment** with a cached Mathlib build (~7.1 GB, 8108 .olean files). Ω's T2 module invokes `lake env lean --stdin` inside this project to compile user theorems against Mathlib.
- **Files in Ω referencing it**:
  - `omega/verify/t2_real.py` — `LEAN_PAPER_PLANE = Path.home() / "lean-paper-plane"` (default path), `real_compile_callback()` runs `lake env lean --stdin` in that directory
- **Dependencies used via lean-paper-plane**:

| Dependency | License | Purpose | Note |
|-----------|---------|---------|------|
| Mathlib | Apache 2.0 | Full math library coverage | Ω theorems compile against this via `lake env lean --stdin` |
| aesop | MIT | Proof automation tactic | Available in the lean-paper-plane environment |
| Lean 4 (stdlib) | Apache 2.0 | Core Lean compiler and standard library | Required by all Lean compilation |
| Qq | Apache 2.0 | Quotation/anti-quotation for tactics | Available as transitive dependency |
| Std | Apache 2.0 | Lean standard library extensions | Available as transitive dependency |

- **Compliance note**: Ω does **not** distribute or vendor lean-paper-plane, Mathlib, or any of its dependencies. Users must set up the lean-paper-plane project independently (see `omega/verify/t2_real.py` documentation). The license references above are for informational purposes to document the compilation environment.

---

## Original Code (Ω-Architect)

All Python source files under `omega/` are original works, written from scratch for Ω-Architect.
The following files constitute Ω-Architect's own codebase, licensed under Apache 2.0:

| File | Description | Lines |
|------|-------------|-------|
| `omega/__init__.py` | Package init, version | 3 |
| `omega/agent/__init__.py` | Agent package init | 4 |
| `omega/agent/message_log.py` | Append-only slice cache with prefix-cache boundaries | 148 |
| `omega/agent/orchestrator.py` | Deterministic state machine (AGENTS.md) | 206 |
| `omega/skills/__init__.py` | 8 skill primitives enum + SkillSelection dataclass | 25 |
| `omega/search/__init__.py` | Search package init, re-exports | 20 |
| `omega/search/proposer.py` | Pluggable tactic proposer (Goedel/Rethlas/Archon strategies) | 313 |
| `omega/search/tree.py` | Proof search tree with MCTS-style selection | 310 |
| `omega/verify/__init__.py` | Verification package init, re-exports | 59 |
| `omega/verify/t1_llm.py` | T1 fast structural verifier (pattern + optional LLM) | 303 |
| `omega/verify/t2_lean.py` | T2 Lean compiler verifier interface | 229 |
| `omega/verify/t2_mcp.py` | T2 MCP compile callback + online runner | 214 |
| `omega/verify/t2_real.py` | Real `lake env lean --stdin` compile backend | 198 |

### Design Patterns (not derivative of third-party code)

- **State machine orchestration** (AGENTS.md): Inspired by agent delegation patterns common in LLM agent frameworks, but implemented as a deterministic Python state machine from scratch. The 8 skill primitives are original to Ω-Architect.
- **MessageLog with prefix-cache**: An original design for append-only message storage with inline cache boundaries, enabling efficient sub-goal reuse. Not derived from any third-party project.
- **T1 structural verifier**: Regex-based Lean code validation. All check functions (`_check_unclosed_blocks`, `_check_dangling_sorry`, `_check_missing_proof_body`, `_check_import_existence`, `_check_admit`) are original.
- **T2 callback architecture**: The `CompileFn` callback pattern that decouples verification logic from the actual compiler invocation is an original design enabling testability without MCP dependencies.
- **T2 real compile**: Uses `lake env lean --stdin` (subprocess) instead of Goedel-Prover-V2's `lake exe repl` approach. The diagnostic parser (`parse_lean_diagnostics`) is original.

### Not Used / Explicitly Omitted

The following patterns and tools from third-party projects are **not used** in Ω-Architect:

| Feature | Used In | Status in Ω |
|---------|---------|-------------|
| Codex CLI | Rethlas, Archon | **Not used** — No Codex, no CLI agent invocation |
| `lake exe repl` | Goedel-Prover-V2 | **Not used** — Ω uses `lake env lean --stdin` |
| Goedel model weights | Goedel-Prover-V2 | **Not used** — No model files or checkpoints |
| Retrieval-augmented generation (RAG) | Rethlas | **Not used** — No embedding/retrieval infrastructure |
| Ensemble orchestration | Archon | **Not used** — Strategies are independent parameters |

---

## Python Runtime Dependencies

From `pyproject.toml`:

| Dependency | License | Purpose |
|-----------|---------|---------|
| `pydantic>=2.0` | MIT | Data validation |
| `numpy>=1.24` | BSD-3-Clause | Numerical computation |
| `scipy>=1.10` | BSD-3-Clause | Scientific computing |

Optional dev dependencies:

| Dependency | License | Purpose |
|-----------|---------|---------|
| `pytest>=7.4` | MIT | Testing framework |
| `ruff>=0.1` | MIT | Python linter |
| `pyright>=1.1` | MIT | Python type checker |
| `sympy>=1.12` | BSD-3-Clause | Symbolic mathematics |
| `matplotlib>=3.7` | BSD-3-Clause | Plotting |

All Python runtime dependencies are under permissive licenses compatible with Apache 2.0.

---

## Third-Party Licenses Summary

| Project | License | Compatibility with Apache 2.0 |
|---------|---------|------------------------------|
| Goedel-Prover-V2 | Apache 2.0 | ✅ Compatible |
| Rethlas | Apache 2.0 | ✅ Compatible |
| Archon | Apache 2.0 | ✅ Compatible |
| Mathlib | Apache 2.0 | ✅ Compatible |
| aesop | MIT | ✅ Compatible |
| Lean 4 stdlib | Apache 2.0 | ✅ Compatible |
| Lean 4 compiler | Apache 2.0 | ✅ Compatible |
| pydantic | MIT | ✅ Compatible |
| numpy | BSD-3-Clause | ✅ Compatible |
| scipy | BSD-3-Clause | ✅ Compatible |

---

## Compliance Checklist

- [x] All third-party licenses are Apache 2.0-compatible
- [x] No GPL/LGPL/AGPL code is used
- [x] All design patterns are reimplemented from scratch — no source code copied
- [x] License file preserved at `omega-architect/LICENSE` (Apache 2.0)
- [x] No third-party model weights or checkpoints are distributed
- [x] No Codex CLI or similar proprietary tools are used
- [x] All Python runtime dependencies are under permissive licenses
- [x] lean-paper-plane/Mathlib is a compile-time environment, not vendored
- [x] Attribution notices for all inspired works are documented above

---

*Last updated: 2026-06-07*

# Ω-Architect

**Open Formal Theorem Proving for Physics, Optics, and Quantum Systems**

Ω-Architect is an open-source (Apache 2.0) formal theorem proving framework designed for:

- **Quantum optics**: Whispering-gallery-mode resonators, Brillouin scattering, optical frequency combs, optical clocks
- **Physics**: Quantum electrodynamics (QED), quantum optics, condensed matter
- **Engineering verification**: NV center sensing, steel rail crack detection
- **Competition math**: IMO, USAMO, Putnam levels

## Architecture

```
User Query
    │
    ▼
[Orchestrator] ── AGENTS.md state machine
    │
    ├── Skill Selector  (8 primitives)
    ├── Decomposer      (divide into sub-goals)
    │
    ├── T1 Verifier     (LLM, ~5s) ─ fast check
    ├── T2 Verifier     (Lean, ~30s) ─ compiler verify
    │
    └── Search Layer    (leansearch, loogle, Mathlib)
```

## Repo Structure

```
omega-architect/
├── AGENTS.md           # Deterministic state machine
├── omega/
│   ├── agent/          # Orchestrator, workers, MessageLog
│   ├── verify/         # T1 (LLM) + T2 (Lean) verifiers
│   ├── skills/         # 8 primitives
│   └── search/         # leansearch, loogle, Mathlib
├── tests/              # pytest + pyright + ruff
└── benchmarks/         # MiniF2F, PutnamBench, etc.
```

## Status

- **P0**: MessageLog + AGENTS.md state machine ✅
- **P1**: T1 Verifier (LLM) 🔄
- **P2**: T2 Verifier (Lean) ⏳
- **P3**: MiniF2F ≥ 80% ⏳

License: Apache 2.0

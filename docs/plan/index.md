---
plan_id: plan-index-v1
title: "Ω-Architect Plan Directory Index"
version: 1.0
date: 2026-06-12
status: active
---

# Plan Index

## Active Plans

| ID | Title | Priority | Status | Link |
|----|-------|:--------:|:------:|:----:|
| **U0** | **Unified Architecture Design** | 🔴 P0 | draft | [architecture/unified-architecture-v1.md](./architecture/unified-architecture-v1.md) |
| P0 | Async Multi-Source Search Aggregator | 🔴 P0 | draft | [search/async-search-aggregator-v1.md](./search/async-search-aggregator-v1.md) |
| P1 | Three-Layer Error Classifier | 🔴 P0 | draft | [classifier/three-layer-classifier-v1.md](./classifier/three-layer-classifier-v1.md) |
| P2 | ConvergenceTracker Fix | 🟡 P1 | pending | — |
| P3 | BudgetTracker Fix | 🟡 P1 | pending | — |
| P4 | Paper Store Ingest Pipeline | 🟢 P2 | pending | — |
| CLI | Third-Party User CLI | — | draft | [cli/third-party-cli-v1.md](./cli/third-party-cli-v1.md) |
| **M0** | **AGENTS.md Refactoring** | 🔴 P0 | pending | — |
|| **F0** | **QED×WGM Formalization Roadmap** (5 设计哲学 → 4 Phase 代数推理) | 🔴 P0 | draft | [qed-wgm-formalization-roadmap-v1.md](./qed-wgm-formalization-roadmap-v1.md) |

## Code Module → Plan Mapping

```
omega/search/lean_search.py     → P0: Async Search Aggregator
omega/search/matlas_cache.py    → P0: Cache Strategy
omega/search/error_classifier.py → P1: Three-Layer Classifier
omega/loop/error_memory.py      → P1: Layer 2 (NLP spectrum) replacement
omega/loop/tracker.py           → P2: ConvergenceTracker fix
omega/resource/budget.py        → P3: BudgetTracker fix
omega/research/sources/         → P4: Paper Store Pipeline
omega/cli/                      → CLI: Third-Party CLI
```

## Key Dependencies

```
P0 (Search) ──depends-on──> omega/resource/config.py
P1 (Classifier) ──depends-on──> omega/search/error_classifier.py
P1 (Classifier) ──depends-on──> omega/loop/errors.py
CLI ──depends-on──> P0, P1, P2, P3
```

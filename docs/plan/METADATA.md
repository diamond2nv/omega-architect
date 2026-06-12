# Ω-Architect Plan Directory — Metadata & Version Tracking

## Directory Structure

```
docs/plan/
├── METADATA.md                  ← THIS FILE: version tracking, provenance
├── index.md                     ← Plan index with component map
│
├── architecture/
│   ├── overview.md              ← System architecture overview
│   ├── component-map.md         ← Module dependency graph
│   └── data-flow.md             ← Pipeline data flow
│
├── search/
│   ├── async-search-aggregator-v1.md    ← P0: Async multi-source search
│   └── cache-strategy.md                ← JSONL cache design
│
├── classifier/
│   ├── three-layer-classifier-v1.md     ← P1: 3-layer error classifier
│   ├── nlp-spectrum.md                  ← NLP algorithm spectrum detail
│   └── llm-judge-prompt.md              ← LLM-as-Judge prompt design
│
├── memory/
│   └── memory-server-analysis.md        ← MCP Memory Server feasibility
│
├── experiments/
│   ├── miniF2F-10-summary.md            ← 10题实验总结
│   └── never-pass-analysis.md           ← 4道难题分析
│
├── cli/
│   └── third-party-cli-v1.md            ← CLI design for 3rd-party users
│
└── error-memory-integration-plan.qmd    ← Legacy plan (迁移中)
```

## Version Tracking

| Plan Doc | Version | Date | Author | Status |
|----------|---------|------|--------|--------|
| async-search-aggregator-v1 | 0.1 | 2026-06-12 | omega | draft |
| three-layer-classifier-v1 | 0.1 | 2026-06-12 | omega | draft |
| third-party-cli-v1 | 0.1 | 2026-06-12 | omega | draft |
| memory-server-analysis | 0.1 | 2026-06-12 | omega | draft |

## Cross-Reference: Code ↔ Plan

| Code Module | Plan Doc | Integration Status |
|-------------|----------|-------------------|
| `omega/search/lean_search.py` | search/async-search-aggregator-v1.md | 📝 being rewritten |
| `omega/search/error_classifier.py` | classifier/three-layer-classifier-v1.md | 📝 being extended |
| `omega/loop/error_memory.py` | memory/memory-server-analysis.md | 🔴 to be replaced |
| `omega/cli/` | cli/third-party-cli-v1.md | 📝 being redesigned |
| `omega/loop/inner.py` | architecture/ | 🟢 stable |
| `omega/prover/` | architecture/ | 🟢 stable |
| `omega/verify/` | architecture/ | 🟢 stable |

## Provenance

Each plan document has frontmatter:

```yaml
---
plan_id: search-async-v1
title: "Async Multi-Source Search Aggregator"
version: 0.1
date: 2026-06-12
author: omega-agent
status: draft
depends_on: [search/lean_search.py, search/matlas_cache.py]
implements: P0
references: [arxiv:2606.09659, github:ultraworkers/claw-code]
---
```

---
plan_id: third-party-cli-v1
title: "Third-Party User CLI Design"
version: 0.1
date: 2026-06-12
author: omega-agent
status: draft
depends_on: [omega/cli/__init__.py, omega/runner.py]
implements: CLI
---

# Third-Party User CLI

## Design Principles

1. **Zero config for basic use**: `omega prove "theorem t: 1+1=2 := by native_decide"` should work out of the box
2. **Progressive complexity**: Config is opt-in, not required
3. **Clear output**: What succeeded, what failed, how much it cost
4. **Minimal dependencies**: Python 3.11+ only, no Ollama/vLLM required for API mode

## Commands

### `omega init` — Project scaffold

```bash
# Interactive scaffold
omega init
# → Creates omega.toml with Lean discovery + budget defaults
# → Creates benchmarks/ directory with MiniF2F test split
# → Creates logs/ directory

# Quick start (assume defaults, just show path)
omega init --quick
```

### `omega prove` — Prove a single theorem

```bash
# Inline theorem (quickest)
omega prove "theorem t (n : ℕ) : n + 0 = n := by"
# → Uses DeepSeek API, auto-manages budget
# → Output: ✅ t proved in 12s ($0.003)

# From file
omega prove theorems/group_theory.lean

# With model selection
omega prove "theorem t : 1+1=2 :=" --model deepseek-v4-flash

# With budget override (target: Lean theorem proving researchers)
omega prove "..." --budget-usd 2.00 --time-min 30
```

### `omega bench` — Run benchmarks

```bash
# Run MiniF2F dev-10 (our 10-problem set)
omega bench minif2f:dev-10
# Output:
#   mathd_numbertheory_3   ✅ (8 rounds, $0.002)
#   induction_12dvd4...    ❌ (14 rounds, $0.008)
#   ...
#   Result: 4/10 (40%)  —  $0.17  —  29.4min

# All of MiniF2F
omega bench minif2f:test --model deepseek-v4-flash --budget-usd 5.00

# Custom benchmark file
omega bench my_problems.json

# Available benchmarks (discovery)
omega bench --list
# minif2f:dev-10 (10 problems)
# minif2f:test   (244 problems)
# putnambench    (672 problems)
# lean-project   (custom project)
```

### `omega search` — Search Mathlib (the new P0 aggregator)

```bash
# Natural language search
omega search "commutativity of addition on ℕ"

# Type signature search
omega search "?a + ?b = ?b + ?a" --mode loogle

# With cache info
omega search "matrix multiplication" --show-cache
# → Source: leansearch.net (cached 5min ago, 3 results)
# → Source: local BM25 (wiki, 2 results)
# → Source: loogle (1 result)
```

### `omega config` — Configuration

```bash
# Show current config
omega config show

# Set budget
omega config set budget.max_cost_usd 2.00

# Set model
omega config set model.default deepseek-v4-pro

# Set API key
omega config set deepseek.api_key "sk-..."
# → Written to ~/.omega/config.toml (not project-local)
```

### `omega doctor` — Health check

```bash
omega doctor
# ✅ Python 3.11.7
# ✅ DeepSeek API: connected (model: deepseek-v4-flash)
# ⚠️  lean-lsp-mCP: not found (install: pip install lean-lsp-mcp)
# ✅ Cache: 47 entries (12MB)
# ✅ Logs: /home/user/.omega/logs/
```

## Output Format

Structured JSON + human-readable summary:

```bash
omega prove "theorem t : 1+1=2 :=" --json
```

```json
{
  "theorem": "theorem t : 1+1=2 :=",
  "status": "proved",
  "model": "deepseek-v4-flash",
  "rounds": 3,
  "cost_usd": 0.0008,
  "time_s": 4.2,
  "compile_ms": 312,
  "proof": "native_decide",
  "search_queries": 0
}
```

## Installation

```bash
# From PyPI (future)
pip install omega-architect

# From source (current)
git clone https://github.com/.../omega-architect.git
cd omega-architect
pip install -e .
```

## Third-Party Workflow

```bash
# 1. Install
pip install omega-architect

# 2. Set API key
echo "DEEPSEEK_API_KEY=sk-..." >> ~/.omega/config.toml

# 3. Prove a theorem
omega prove "theorem add_comm (a b : ℕ) : a + b = b + a :="

# 4. Run benchmark
omega bench minif2f:dev-10

# 5. Review results
cat logs/latest/summary.json
```

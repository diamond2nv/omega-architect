#!/usr/bin/env python3
"""Omega resource management — budget, benchmark, convergence, and allocation.

Unified exports:

**Budget tracking:**
    BudgetConfig       — validated configuration (pydantic-like dataclass)
    BudgetTracker      — dual-tier token/cost/time/attempt budget enforcement
    BudgetTier         — per-tier limits with time-based dynamic tokens (``tok_s``)

**Hardware benchmark:**
    BenchData, BenchResult  — measured tok/s from local ollama models
    load_benchmark, get_tok_s  — auto-detect + cache access

**Convergence:**
    ConvergenceTracker — epoch-level convergence tracking with stuck detection
    EpochSnapshot      — per-epoch metrics dataclass

**Allocation:**
    ModelAllocator     — strategic local/remote model routing

**Config loading:**
    load_config        — load from file + env + CLI with caching
    reset_config_cache — clear global config cache (tests)
    get                — dot-path access into a BudgetConfig

Config loading chain (later overrides earlier):
    1. BudgetConfig() hardcoded defaults
    2. omega.toml / .omega/config.toml / ~/.omega/config.toml
    3. OMEGA_* environment variables
    4. CLI --budget-* overrides
"""

from __future__ import annotations

from omega.resource.allocator import Allocation, ComplexityClass, ModelAllocator
from omega.resource.benchmark import (
    BenchData,
    BenchResult,
    get_tok_s,
    load_benchmark,
    reset_benchmark_cache,
)
from omega.resource.budget import BudgetTracker
from omega.resource.config import (
    DEFAULT_BUDGET,
    BudgetConfig,
    BudgetTier,
    EpochConfig,
    get,
    load_config,
    reset_config_cache,
)
from omega.resource.tracker import ConvergenceTracker, EpochSnapshot

__all__ = [
    "Allocation",
    "BenchData",
    "BenchResult",
    "BudgetConfig",
    "BudgetTier",
    "BudgetTracker",
    "ComplexityClass",
    "ConvergenceTracker",
    "DEFAULT_BUDGET",
    "EpochConfig",
    "EpochSnapshot",
    "ModelAllocator",
    "get",
    "get_tok_s",
    "load_benchmark",
    "load_config",
    "reset_benchmark_cache",
    "reset_config_cache",
]

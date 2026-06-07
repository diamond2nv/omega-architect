"""Omega resource management — budget tracking and convergence monitoring.

Unified exports:
    BudgetConfig (alias for ``config.DEFAULT_BUDGET``)
    get          — dot-path access into default budget
    BudgetTracker — token/cost/time/attempt budget enforcement
    ConvergenceTracker — epoch-level convergence tracking
    EpochSnapshot — per-epoch metrics dataclass
"""

from __future__ import annotations

from omega.resource.budget import BudgetTracker
from omega.resource.config import DEFAULT_BUDGET, get
from omega.resource.tracker import ConvergenceTracker, EpochSnapshot

__all__ = [
    "BudgetConfig",
    "BudgetTracker",
    "ConvergenceTracker",
    "EpochSnapshot",
    "get",
]

# Type alias for backward compatibility / ergonomics
BudgetConfig = DEFAULT_BUDGET

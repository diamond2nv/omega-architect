#!/usr/bin/env python3
"""Ω-Architect Search Module — Proof search infrastructure.

Core abstractions: tree representation, proposer interface,
blueprint generation (P1), pass@k manager (P0).

Designed to support multiple search strategies:
- Goedel-style: parallel sampling + self-correction
- Rethlas-style: blueprint decomposition + retrieval
- Archon-style: multi-strategy ensemble + progress monitoring
- Blueprint Refinement (P1-P3): global DAG → parallel prove → refine
"""

from omega.search.proposer import Proposer
from omega.search.tree import GoalState, ProofTree, SearchNode
from omega.search.passk import OmegaPassKManager, PassKReport
from omega.search.aggregator import SearchAggregator, AggregatedResult
from omega.search.cache import JSONLCache

__all__ = [
    "GoalState",
    "ProofTree",
    "SearchNode",
    "Proposer",
    "OmegaPassKManager",
    "PassKReport",
    "SearchAggregator",
    "AggregatedResult",
    "JSONLCache",
]

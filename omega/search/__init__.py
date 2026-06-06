"""Ω-Architect Search Module — Proof search infrastructure.

Core abstractions for proof search: tree representation, proposer interface,
lemma retrieval, and compilation checking.

Designed to support multiple search strategies:
- Goedel-style: parallel sampling + self-correction
- Rethlas-style: blueprint decomposition + retrieval
- Archon-style: multi-strategy ensemble + progress monitoring
"""

from omega.search.tree import GoalState, ProofTree, SearchNode
from omega.search.proposer import Proposer

__all__ = [
    "GoalState",
    "ProofTree",
    "SearchNode",
    "Proposer",
]

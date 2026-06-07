#!/usr/bin/env python3
"""Omega research module — Phase 0 deep-knowledge pipeline for theorem proving.

Provides data types, data sources, and the KnowledgeProver wrapper
that injects domain knowledge (papers, lemmas, patterns) into any
Omega prover before proof generation.
"""

from omega.research.knowledge import (
    KnowledgePackage,
    LemmaInfo,
    PaperInfo,
    ProofPattern,
    make_cache_key,
)
from omega.research.prover import (
    KnowledgeProver,
    ResearchAwareResult,
    ResearchResult,
)

__all__ = [
    "KnowledgePackage",
    "KnowledgeProver",
    "LemmaInfo",
    "PaperInfo",
    "ProofPattern",
    "ResearchAwareResult",
    "ResearchResult",
    "make_cache_key",
]

#!/usr/bin/env python3
"""KnowledgePackage — structured domain knowledge for theorem proving.

This module defines the data types for the Phase 0 deep-research pipeline.
A KnowledgePackage is built before proof generation begins, giving the
proposer relevant papers, Lean4 lemmas, proof patterns, and import hints.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

# ── Paper Information ───────────────────────────────────────────


@dataclass
class PaperInfo:
    """A research paper relevant to the theorem being proved."""

    arxiv_id: str = ""
    title: str = ""
    abstract: str = ""
    categories: list[str] = field(default_factory=list)
    relevance: float = 0.0
    doi: str = ""
    source_url: str = ""
    code_url: str = ""

    @property
    def short(self) -> str:
        return f"{self.arxiv_id}: {self.title[:80]}"


# ── Lean4 Lemma Information ─────────────────────────────────────


@dataclass
class LemmaInfo:
    """A Lean4 lemma relevant to the current theorem."""

    name: str = ""
    statement: str = ""
    source: str = ""  # mathlib, miniF2F, ai4math, etc.
    file_path: str = ""
    proof_length: int = 0
    relevance: float = 1.0

    @property
    def short(self) -> str:
        return f"{self.name} ({self.source})"


# ── Proof Pattern ────────────────────────────────────────────────


@dataclass
class ProofPattern:
    """A reusable proof pattern (induction, case_split, calc_chain, etc.)."""

    name: str = ""
    applicability: float = 0.0  # 0-1 how well this matches the current goal
    example_use: str = ""
    description: str = ""


# ── KnowledgePackage ────────────────────────────────────────────


@dataclass
class KnowledgePackage:
    """Domain knowledge package built by the research phase.

    Attributes:
        related_papers: Semantically related arXiv/HF papers.
        theorem_statements: Theorem statements extracted from papers.
        key_insights: Natural-language key insights.
        relevant_lemmas: Lean4 lemmas extracted from Mathlib / miniF2F / AI4Math.
        proof_patterns: Reusable proof patterns matching the goal.
        mathlib_imports: Suggested Lean4 imports.
        search_stats: Metadata about the research phase.
        cache_hit: Whether this was served from a prior-run cache.
    """

    related_papers: list[PaperInfo] = field(default_factory=list)
    theorem_statements: list[str] = field(default_factory=list)
    key_insights: list[str] = field(default_factory=list)

    relevant_lemmas: list[LemmaInfo] = field(default_factory=list)
    proof_patterns: list[ProofPattern] = field(default_factory=list)
    mathlib_imports: list[str] = field(default_factory=list)

    search_stats: dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            not self.related_papers
            and not self.relevant_lemmas
            and not self.proof_patterns
            and not self.key_insights
        )

    @property
    def summary(self) -> str:
        """Human-readable summary of the knowledge package."""
        lines = [
            f"KnowledgePackage — {len(self.related_papers)} papers, "
            f"{len(self.relevant_lemmas)} lemmas, "
            f"{len(self.proof_patterns)} patterns",
        ]
        if self.cache_hit:
            lines.append("  cache: hit")
        if self.search_stats:
            lines.append(f"  search: {self.search_stats.get('elapsed_s', 0):.1f}s")
        if self.mathlib_imports:
            lines.append(f"  import suggestions: {', '.join(self.mathlib_imports[:5])}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (JSON-safe)."""
        return {
            "n_papers": len(self.related_papers),
            "n_lemmas": len(self.relevant_lemmas),
            "n_patterns": len(self.proof_patterns),
            "n_imports": len(self.mathlib_imports),
            "cache_hit": self.cache_hit,
            "search_stats": self.search_stats,
        }


# ── Research Cache Key ──────────────────────────────────────────


def make_cache_key(theorem_header: str) -> str:
    """Generate a deterministic cache key from a theorem header."""
    raw = theorem_header.strip()
    return hashlib.sha256(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

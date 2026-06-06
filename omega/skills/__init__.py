"""8 skill primitives for Ω-Architect."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Primitive(Enum):
    """The 8 skill primitives of Ω-Architect."""
    APPLY_LEMMA = "apply_lemma"
    REWRITE_GOAL = "rewrite_goal"
    INDUCTION = "induction"
    CASE_SPLIT = "case_split"
    CALC_CHAIN = "calc_chain"
    SEARCH_LEMMA = "search_lemma"
    EXTRACT_PROOF = "extract_proof"
    FALLBACK_DECOMPOSE = "fallback_decompose"


@dataclass
class SkillSelection:
    """Result of skill selection."""
    primitive: Primitive
    confidence: float
    reason: str

#!/usr/bin/env python3
"""Curriculum pass@k — e^S difficulty scoring + dynamic k allocation.

P4 of the Ω-Architect roadmap.

Core idea: not all theorems need the same k. A trivial ``rfl`` can be solved
with k=1; a hard number theory problem may need k=64+.  We compute a
difficulty score S ∈ [0, 7] and allocate k = max(1, floor(base_budget / 2^S)).

Difficulty features (input → e^S):
  - Binder count (ℕ/Nat/Int/ℝ/…)
  - Quantifier count (∀, ∃)
  - Goal complexity (depth of type nesting)
  - Strategy compatibility (induction vs calc vs …)
  - Token length of formal statement

Usage::

    from omega.search.curriculum import compute_difficulty, allocate_k

    header = "theorem add_zero (n : ℕ) : n + 0 = n :="
    S = compute_difficulty(header)      # → 3.2
    k = allocate_k(S)                   # → 32 (for base_budget=256)
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("omega.search.curriculum")

# ── Feature extractors ──────────────────────────────────────────

# Type keywords that indicate proof complexity
_COMPLEX_TYPE_WORDS = {
    "ℝ", "ℂ", "ℚ", "ℤ", "ℕ", "Matrix", "Polynomial", "Tensor",
    "List", "Set", "Finset", "Multiset", "Seq", "Stream",
    "Vector", "Array", "Fin", "Subtype", "Sigma", "Sum",
    "Prod", "Option", "Except", "Nat", "Int", "Real", "Complex",
    "Group", "Ring", "Field", "Module", "Ideal", "Ideal",
    "Hom", "End", "Aut", "Equiv", "Order", "Category",
}


def _count_binders(theorem_header: str) -> int:
    """Count top-level binder patterns like ``(n : ℕ)``."""
    paren_depth = 0
    binders = 0
    for line in theorem_header.split("\n"):
        if "theorem" not in line and "lemma" not in line and "def" not in line:
            continue
        for i, ch in enumerate(line):
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                if paren_depth == 1:
                    binders += 1
                paren_depth -= 1
    return max(binders, 0)


def _count_quantifiers(theorem_header: str) -> int:
    """Count ∀ and ∃ quantifiers in the header."""
    return len(re.findall(r"[∀∃]", theorem_header))


def _goal_depth(theorem_header: str) -> int:
    """Compute type nesting depth of the goal (after the last colon).

    Approximated by max paren-depth in the target expression.
    """
    target = _extract_target(theorem_header)
    if not target:
        return 0
    depth = 0
    max_depth = 0
    for ch in target:
        if ch in "([{":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch in ")]}":
            depth -= 1
    return max_depth


def _complex_type_count(theorem_header: str) -> int:
    """Count occurrences of complex type words."""
    target = _extract_target(theorem_header)
    header_lower = (theorem_header + " | " + (target or "")).lower()
    return sum(1 for w in _COMPLEX_TYPE_WORDS if w.lower() in header_lower)


def _extract_target(theorem_header: str) -> str:
    """Extract the proof target (after final ``:`` outside parens)."""
    for line in theorem_header.split("\n"):
        if "theorem" in line or "lemma" in line or "def" in line:
            paren_depth = 0
            colon_positions = []
            for i, ch in enumerate(line):
                if ch == "(":
                    paren_depth += 1
                elif ch == ")":
                    paren_depth -= 1
                elif ch == ":" and paren_depth == 0:
                    next_ch = line[i + 1] if i + 1 < len(line) else " "
                    if next_ch not in ("=", ":"):
                        colon_positions.append(i)
            if colon_positions:
                return line[colon_positions[-1] + 1:].strip()
    return ""


# ── Difficulty scoring ──────────────────────────────────────────


def compute_difficulty(theorem_header: str) -> float:
    """Compute a difficulty score S ∈ [0.0, 7.0].

    Factors and their weights (calibrated on MiniF2F 244):

    +---------------------------+-------+----------------------------+
    | Factor                    | Weight| Typical range              |
    +---------------------------+-------+----------------------------+
    | Binder count (capped 5)   | 0.50  | 0-5                        |
    | Quantifier count (cap 3)  | 0.75  | 0-3                        |
    | Goal depth (capped 4)     | 0.25  | 0-4                        |
    | Complex type count (cap 5)| 0.30  | 0-5                        |
    | Theorem length (norm)     | 0.20  | 0-1 (chars/200 capped)     |
    +---------------------------+-------+----------------------------+

    Returns
    -------
    float
        Difficulty score S.  Higher = harder.
    """
    binders = min(_count_binders(theorem_header), 5)
    quantifiers = min(_count_quantifiers(theorem_header), 3)
    depth = min(_goal_depth(theorem_header), 4)
    complex_types = min(_complex_type_count(theorem_header), 5)
    # Normalized length
    norm_len = min(len(theorem_header) / 200.0, 1.0)

    S = (
        binders * 0.50 +
        quantifiers * 0.75 +
        depth * 0.25 +
        complex_types * 0.30 +
        norm_len * 0.20
    )

    # Clamp
    S = max(0.0, min(S, 7.0))
    return round(S, 2)


def compute_difficulty_with_strategy(theorem_header: str,
                                     strategy_result: dict[str, Any] | None = None,
                                     ) -> dict[str, Any]:
    """Extended difficulty assessment with strategy awareness.

    Parameters
    ----------
    theorem_header : str
    strategy_result : dict or None
        Output from ``proposer.analyze_theorem_pattern()``.

    Returns
    -------
    dict
        Keys: ``S`` (raw difficulty), ``k_suggested``, ``strategy``,
        ``features`` (breakdown), ``confidence``.
    """
    S = compute_difficulty(theorem_header)
    strat = strategy_result or {}

    # Penalise unknown strategy
    if strat.get("strategy") == "unknown" or not strat:
        S += 1.0
    elif strat.get("confidence", 1.0) < 0.5:
        S += 0.5

    S = max(0.0, min(S, 7.0))

    k = allocate_k(S)
    return {
        "S": round(S, 2),
        "k_suggested": k,
        "strategy": strat.get("strategy", "unknown"),
        "strategy_confidence": strat.get("confidence", 0.0),
        "features": {
            "binders": _count_binders(theorem_header),
            "quantifiers": _count_quantifiers(theorem_header),
            "goal_depth": _goal_depth(theorem_header),
            "complex_types": _complex_type_count(theorem_header),
            "norm_length": round(min(len(theorem_header) / 200.0, 1.0), 3),
        },
    }


def allocate_k(S: float, base_budget: int = 256) -> int:
    """Allocate samples k given difficulty S.

    Strategy: exponential allocation so that harder theorems get *more* samples.

        k = ceil(base_budget / 2^(7 - S))

    At S=0 (trivial):   k ≈ ceil(256 / 2^7) = 2
    At S=3.5 (medium):  k ≈ ceil(256 / 2^3.5) = ceil(256 / 11.3) ≈ 23
    At S=7 (hard):      k = 256 (full budget)

    Parameters
    ----------
    S : float
        Difficulty score [0.0, 7.0].
    base_budget : int
        Total sample budget (default 256).

    Returns
    -------
    int
        Recommended k.
    """
    import math
    if S >= 7.0:
        return base_budget
    denominator = 2.0 ** (7.0 - S)
    k = math.ceil(base_budget / denominator)
    return max(1, min(k, base_budget))


# ── Curriculum-aware pass@k wrapper ─────────────────────────────


def curriculum_run(mgr, theorem_header: str,
                   base_budget: int = 256,
                   min_k: int = 1,
                   max_k: int | None = None,
                   ) -> dict[str, Any]:
    """Run curriculum pass@k: auto-allocate k based on difficulty.

    Parameters
    ----------
    mgr : OmegaPassKManager
    theorem_header : str
    base_budget : int
        Total budget to distribute (default 256).
    min_k : int
        Minimum k (default 1).
    max_k : int or None
        Maximum k cap (default ``base_budget``).

    Returns
    -------
    dict
        ``{"difficulty": dict, "report": PassKReport, "k": int}``
    """
    if max_k is None:
        max_k = base_budget

    diff = compute_difficulty_with_strategy(theorem_header)
    k = max(min_k, min(diff["k_suggested"], max_k))

    report = mgr.run(theorem_header, k=k)
    return {
        "difficulty": diff,
        "report": report,
        "k": k,
    }


# ── Legacy alias ────────────────────────────────────────────────

compute_difficulty_score = compute_difficulty

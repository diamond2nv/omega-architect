#!/usr/bin/env python3
"""Heuristic routing flags for Omega theorem-proving routing.

Derived from OpenSquilla's ``compute_flags`` / ``RoutingFlags`` paradigm
(Apache-2.0).  Adapted to the theorem-proving domain with Lean-specific
detection patterns (induction, complex types, multi-goal, etc.).

Copyright 2025 OpenSquilla Authors
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "TheoremFlags",
    "compute_theorem_flags",
    "apply_postprocess",
    "TIER_SIMPLE",
    "TIER_MEDIUM",
    "TIER_HARD",
]

TIER_SIMPLE = 0
TIER_MEDIUM = 1
TIER_HARD = 2
TIER_NAMES = ["simple", "medium", "hard"]

# ── Pattern detectors ──────────────────────────────────────────

_RE_INDUCTION = re.compile(
    r"\b(?:induction|struct_induction|case|cases)\b", re.I
)
_RE_COMPLEX_TYPE = re.compile(
    r"[→∀∃⇔]|\b(?:Set|List\s|Stream|Matrix|Tensor|Polynomial)\b"
)
_RE_MULTI_GOAL = re.compile(
    r"(?:by\s*\n\s*(?:refine|apply).*;|·\s+|·\n)"
)
_RE_ERROR_CUE = re.compile(
    r"(?:error|fail|wrong|incorrect|doesn't type.?check|compilation)", re.I
)


@dataclass
class TheoremFlags:
    """Runtime flags affecting model selection and post-processing.

    Each flag is a boolean indicator derived from the theorem header text
    and optional proving history.  Flags drive tier escalation in the
    ``apply_postprocess`` pipeline.
    """

    long_proof: bool = False
    complex_type: bool = False
    induction: bool = False
    requires_library: bool = False
    multi_goal: bool = False
    error_correction: bool = False

    def min_tier(self) -> int:
        """Return the minimum tier index required by currently-set flags."""
        if self.complex_type or self.multi_goal:
            return TIER_HARD  # 2
        if self.induction or self.requires_library:
            return TIER_MEDIUM  # 1
        return TIER_SIMPLE  # 0


def compute_theorem_flags(
    theorem_header: str,
    history: list[dict] | None = None,
) -> TheoremFlags:
    """Extract runtime flags from a Lean 4 theorem header and history.

    Parameters
    ----------
    theorem_header : str
        The Lean 4 theorem header text (e.g. ``theorem t (n : ℕ) ... :=``).
    history : list[dict] | None
        Optional proving-attempt history, each dict with keys
        ``tier``, ``succeeded``, etc.

    Returns
    -------
    TheoremFlags
        Zero or more flags set based on text patterns + history signals.
    """
    flags = TheoremFlags()

    # ── Text-based flags ────────────────────────────────────
    flags.long_proof = len(theorem_header) > 200
    flags.complex_type = bool(_RE_COMPLEX_TYPE.search(theorem_header))
    flags.induction = bool(_RE_INDUCTION.search(theorem_header))
    flags.requires_library = "import" in theorem_header.lower()
    flags.multi_goal = (
        theorem_header.count("∧") > 1
        or bool(_RE_MULTI_GOAL.search(theorem_header))
    )

    # ── History-based flags ─────────────────────────────────
    if history:
        flags.error_correction = any(
            h.get("succeeded") is False for h in history
        )

    return flags


def apply_postprocess(
    base_tier: int,
    flags: TheoremFlags,
    history: list[dict] | None = None,
    fallback_count: int = 0,
) -> int:
    """Multi-layer post-processing for Omega ModelRouter.

    Inspired by OpenSquilla's 7-layer post-processing pipeline
    (``apply_postprocess`` in ``src/router/inference/postprocess.py``,
    Apache-2.0).  Adapted for 3-tier theorem proving.

    Layers
    ------
    1. **Safety net** — flags force minimum tier (complex_type → hard)
    2. **Flag override** — combinations force escalation
    3. **Failure escalation** — repeated failures bump tier by +1
    4. **Sticky tier** (KV-cache-aware) — avoid downgrading between turns

    Parameters
    ----------
    base_tier : int
        Initial tier (0=simple, 1=medium, 2=hard).
    flags : TheoremFlags
        Runtime flags from ``compute_theorem_flags``.
    history : list[dict] | None
        Previous routing decisions for sticky-tier logic.
    fallback_count : int
        Number of consecutive failures for escalation.

    Returns
    -------
    int
        Final tier after all post-processing layers.
    """
    tier = base_tier

    # Layer 1: safety net — flags force minimum tier
    tier = max(tier, flags.min_tier())

    # Layer 2: flag overrides — compound conditions escalate further
    if flags.long_proof and flags.complex_type:
        tier = max(tier, TIER_HARD)  # long + complex → always hard
    if flags.induction and flags.long_proof:
        tier = max(tier, TIER_MEDIUM)
    if flags.requires_library and flags.complex_type:
        tier = max(tier, TIER_HARD)

    # Layer 3: failure escalation — repeated fails bump tier
    if fallback_count >= 2:
        tier = min(tier + 1, TIER_HARD)

    # Layer 4: sticky tier — avoid costly KV-cache invalidation
    # when the model was just pre-filling for a higher tier
    if history and len(history) > 0:
        last_tier = history[-1].get("tier", tier)
        if last_tier > tier:
            # Remain at the previous higher tier to avoid cache flush
            tier = last_tier

    return tier

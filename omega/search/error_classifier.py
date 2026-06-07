#!/usr/bin/env python3
"""Lean Error Classifier — parse and categorise Lean 4 compiler diagnostics.

Provides a structured replacement for the keyword-matching approach in
``error_based_suggestions()``, enabling targeted correction strategies
for different error categories.

Usage::

    from omega.search.error_classifier import LeanErrorClassifier, ErrorCategory

    classifier = LeanErrorClassifier()
    for err_msg in t2_result.errors:
        cat, detail = classifier.classify(err_msg)
        strategy = classifier.strategy(cat)
        print(f"  [{cat.name}] {detail} → {strategy}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ErrorCategory(Enum):
    """Lean 4 compiler error categories.

    Each maps to a concrete correction strategy (see :meth:`LeanErrorClassifier.strategy`).
    """

    SYNTAX_ERROR = "syntax_error"
    """Malformed syntax: missing parentheses, wrong indentation, etc."""

    TYPE_ERROR = "type_error"
    """Type mismatch: expression has wrong type for the context."""

    UNSOLVED_GOAL = "unsolved_goal"
    """Tactic left some goals unproven (incomplete proof)."""

    MISSING_LEMMA = "missing_lemma"
    """Unknown identifier or theorem — likely needs an import or lemma name."""

    TACTIC_ERROR = "tactic_error"
    """Tactic failed: the tactic syntax is valid but can't close the goal."""

    TIMEOUT = "timeout"
    """Compiler timed out or was interrupted."""

    INCOMPLETE_BLOCK = "incomplete_block"
    """``:= by`` with empty body or ``sorry`` placeholder."""

    UNKNOWN_MODULE = "unknown_module"
    """Missing import — module not found."""

    AMBIGUOUS = "ambiguous"
    """Ambiguous overload or name resolution issue."""

    UNKNOWN_ERROR = "unknown"
    """Unrecognised error pattern — fallback behaviour."""


# ── Pattern library ──────────────────────────────────────────────

# Each entry: (pattern, category, extract_detail_fn)
# patterns are compiled regexes, matched case-insensitively on the raw
# error message.

_PATTERN_LIBRARY: list[tuple[re.Pattern, ErrorCategory, str | None]] = [
    # ── TYPE_ERROR (must check before SYNTAX_ERROR's "expected") ──
    (re.compile(r"type mismatch"), ErrorCategory.TYPE_ERROR, None),
    (re.compile(r"has type\s+.*\n?\s*but is expected to have type"), ErrorCategory.TYPE_ERROR, None),
    (re.compile(r"but is expected to have type"), ErrorCategory.TYPE_ERROR, None),
    (re.compile(r"cannot apply"), ErrorCategory.TYPE_ERROR, None),

    # ── SYNTAX_ERROR ──
    (re.compile(r"syntax error"), ErrorCategory.SYNTAX_ERROR, None),
    (re.compile(r"expected"), ErrorCategory.SYNTAX_ERROR, None),
    (re.compile(r"unexpected"), ErrorCategory.SYNTAX_ERROR, None),
    (re.compile(r"mismatch\b"), ErrorCategory.SYNTAX_ERROR, None),
    (re.compile(r"end of input"), ErrorCategory.SYNTAX_ERROR, None),

    # ── UNSOLVED_GOAL ──
    (re.compile(r"unsolved goals?"), ErrorCategory.UNSOLVED_GOAL, None),
    (re.compile(r"unsolved goal"), ErrorCategory.UNSOLVED_GOAL, None),
    (re.compile(r"goal\s*:\s*$", re.MULTILINE), ErrorCategory.UNSOLVED_GOAL, None),

    # ── MISSING_LEMMA (unknown identifier / constant) ──
    (re.compile(r"unknown\s+(identifier|constant|theorem|lemma)"), ErrorCategory.MISSING_LEMMA, None),
    (re.compile(r"unknown declaration"), ErrorCategory.MISSING_LEMMA, None),

    # ── TACTIC_ERROR ──
    (re.compile(r"tactic\s+'?(\w+)'?\s+failed"), ErrorCategory.TACTIC_ERROR, r"tactic '\1' failed"),
    (re.compile(r"tactic\s+(\w+)\s+failed"), ErrorCategory.TACTIC_ERROR, r"tactic '\1' failed"),
    (re.compile(r"don't know how to"), ErrorCategory.TACTIC_ERROR, None),
    (re.compile(r"no applicable"), ErrorCategory.TACTIC_ERROR, None),

    # ── TIMEOUT ──
    (re.compile(r"timeout?"), ErrorCategory.TIMEOUT, None),
    (re.compile(r"interrupted"), ErrorCategory.TIMEOUT, None),
    (re.compile(r"maximum number of heartbeats"), ErrorCategory.TIMEOUT, None),

    # ── INCOMPLETE_BLOCK ──
    (re.compile(r"missing\s+body"), ErrorCategory.INCOMPLETE_BLOCK, None),
    (re.compile(r"expected\s+body"), ErrorCategory.INCOMPLETE_BLOCK, None),
    (re.compile(r"`:=` expected"), ErrorCategory.INCOMPLETE_BLOCK, None),

    # ── UNKNOWN_MODULE ──
    (re.compile(r"unknown module"), ErrorCategory.UNKNOWN_MODULE, None),
    (re.compile(r"module\s+not\s+found"), ErrorCategory.UNKNOWN_MODULE, None),
    (re.compile(r"could not resolve"), ErrorCategory.UNKNOWN_MODULE, None),

    # ── AMBIGUOUS ──
    (re.compile(r"ambiguous"), ErrorCategory.AMBIGUOUS, None),
    (re.compile(r"overload"), ErrorCategory.AMBIGUOUS, None),
]

# ── Correction strategies ───────────────────────────────────────

_STRATEGIES: dict[ErrorCategory, str] = {
    ErrorCategory.SYNTAX_ERROR: "Reformat the proof block — check parentheses, indentation, and trailing colons",
    ErrorCategory.TYPE_ERROR: "Use ``exact`` or ``apply`` with explicit type annotation; check term types",
    ErrorCategory.UNSOLVED_GOAL: "Try alternative tactic (omega / arith / nlinarith / simp / ring)",
    ErrorCategory.MISSING_LEMMA: "Search Mathlib with ``loogle`` or ``leansearch`` for relevant lemma",
    ErrorCategory.TACTIC_ERROR: "Switch to a different tactic family (e.g. induction → cases → omega)",
    ErrorCategory.TIMEOUT: "Reduce proof complexity; split into smaller lemmas or use more direct reasoning",
    ErrorCategory.INCOMPLETE_BLOCK: "Fill in the missing proof body — ``:= by`` block is empty",
    ErrorCategory.UNKNOWN_MODULE: "Add the missing ``import`` statement",
    ErrorCategory.AMBIGUOUS: "Add namespace prefix or type annotation to disambiguate",
    ErrorCategory.UNKNOWN_ERROR: "Fallback: try simp / trivial / rfl and escalate",
}


@dataclass
class ClassificationResult:
    """Result of classifying a single Lean error message.

    Attributes
    ----------
    category : ErrorCategory
        The determined error category.
    detail : str
        Human-readable detail about what was found.
    original : str
        The original error message.
    """
    category: ErrorCategory
    detail: str = ""
    original: str = ""


class LeanErrorClassifier:
    """Classify Lean 4 compiler error messages into structured categories.

    Uses regex pattern matching against a curated library of Lean error
    message signatures (see ``_PATTERN_LIBRARY``).

    Thread-safe (no mutable state between calls).
    """

    def __init__(self) -> None:
        self._patterns = _PATTERN_LIBRARY

    def classify(self, error_message: str) -> ClassificationResult:
        """Classify a single Lean error message.

        Parameters
        ----------
        error_message : str
            Raw error message from the Lean compiler (e.g. from
            ``T2Result.errors``).

        Returns
        -------
        ClassificationResult
            The category and detail extracted from the message.
        """
        if not error_message:
            return ClassificationResult(
                category=ErrorCategory.UNKNOWN_ERROR,
                detail="Empty error message",
                original=error_message,
            )

        msg = error_message.strip()

        # Try each pattern
        for pattern, category, detail_template in self._patterns:
            match = pattern.search(msg)
            if match is not None:
                if detail_template:
                    detail = match.expand(detail_template)
                else:
                    # Use the matched text as detail
                    matched_text = match.group(0)
                    detail = matched_text[:80] if len(matched_text) > 80 else matched_text
                return ClassificationResult(
                    category=category,
                    detail=detail,
                    original=error_message,
                )

        # Fallback: check for incomplete ``:= by`` blocks
        if ":=" in msg and "by" in msg:
            return ClassificationResult(
                category=ErrorCategory.INCOMPLETE_BLOCK,
                detail="Incomplete ``:= by`` block detected in error context",
                original=error_message,
            )

        return ClassificationResult(
            category=ErrorCategory.UNKNOWN_ERROR,
            detail=f"No pattern matched: {msg[:100]}",
            original=error_message,
        )

    def classify_many(self, error_messages: list[str]) -> dict[ErrorCategory, list[str]]:
        """Classify multiple error messages, grouped by category.

        Parameters
        ----------
        error_messages : list[str]
            List of raw error messages.

        Returns
        -------
        dict[ErrorCategory, list[str]]
            Errors grouped by category.  Categories with no matches
            are omitted.
        """
        groups: dict[ErrorCategory, list[str]] = {}
        for msg in error_messages:
            result = self.classify(msg)
            groups.setdefault(result.category, []).append(msg)
        return groups

    @staticmethod
    def strategy(category: ErrorCategory) -> str:
        """Return the recommended correction strategy for a category.

        Parameters
        ----------
        category : ErrorCategory
            The error category.

        Returns
        -------
        str
            Human-readable correction strategy.
        """
        return _STRATEGIES.get(category, _STRATEGIES[ErrorCategory.UNKNOWN_ERROR])

    @staticmethod
    def suggestion_tactics(
        category: ErrorCategory,
        tried_tactics: set[str] | None = None,
    ) -> list[tuple[str, float, str]]:
        """Return tactic suggestions appropriate for an error category.

        Parameters
        ----------
        category : ErrorCategory
            The error category.
        tried_tactics : set[str] or None
            Tactics already attempted (will be excluded from suggestions).

        Returns
        -------
        list of (tactic, confidence, description) tuples
        """
        tried = tried_tactics or set()

        pools: dict[ErrorCategory, list[tuple[str, float, str]]] = {
            ErrorCategory.UNSOLVED_GOAL: [
                ("omega", 0.6, "Arithmetic decision procedure"),
                ("arith", 0.5, "Linear arithmetic solver"),
                ("nlinarith", 0.5, "Non-linear arithmetic solver"),
                ("simp", 0.4, "Simplification"),
                ("norm_num", 0.4, "Normalise numeric expressions"),
                ("ring", 0.4, "Ring algebra solver"),
            ],
            ErrorCategory.TACTIC_ERROR: [
                ("simp", 0.5, "Fall back to simplification"),
                ("trivial", 0.4, "Try trivial"),
                ("rfl", 0.4, "Try reflexivity"),
                ("omega", 0.3, "Arithmetic decision procedure"),
            ],
            ErrorCategory.TYPE_ERROR: [
                ("exact ?_", 0.3, "Provide exact term"),
                ("apply ?_", 0.3, "Apply a lemma or hypothesis"),
                ("refine ?_", 0.2, "Refine the goal"),
            ],
            ErrorCategory.MISSING_LEMMA: [
                ("apply?", 0.5, "Search for applicable lemmas"),
                ("simp", 0.2, "Simplification as fallback"),
            ],
            ErrorCategory.INCOMPLETE_BLOCK: [
                ("simp", 0.5, "Simplification to fill block"),
                ("trivial", 0.4, "Trivial to fill block"),
                ("rfl", 0.3, "Reflexivity to fill block"),
            ],
            ErrorCategory.UNKNOWN_ERROR: [
                ("simp", 0.2, "Backup: simplification"),
                ("trivial", 0.15, "Backup: trivial"),
            ],
        }

        candidates = pools.get(category, pools[ErrorCategory.UNKNOWN_ERROR])
        return [(t, c, d) for t, c, d in candidates if t not in tried]

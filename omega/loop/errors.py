#!/usr/bin/env python3
"""Compile error classifier — 13 classes.

Parses lean --stdin diagnostic output and classifies errors.
Replaces string-equality dead-loop detection with classification.
"""

from __future__ import annotations

import re
from enum import Enum


class CompileErrorClass(Enum):
    """Lean compile error classes."""

    UNKNOWN_IDENT = "unknown_identifier"
    TYPE_MISMATCH = "type_mismatch"
    SYNTAX_ERROR = "syntax_error"
    UNIVERSE = "universe_constraint"
    FUNCTION_EXPECTED = "function_expected"
    UNUSED_VARIABLE = "unused_variable"
    FAILED_SYNTHESIS = "failed_to_synthesize"
    # Tactic-level failures. UNSOLVED_GOAL is the single most common Lean outcome
    # for a failing proof step (the tactic ran but did not close the goal), so it
    # must not fall through to NO_ERROR.
    UNSOLVED_GOAL = "unsolved_goal"
    TACTIC_FAILED = "tactic_failed"
    AMBIGUOUS = "ambiguous"
    TIMEOUT = "timeout"
    MEMORY = "memory"
    CYCLIC_DEP = "cyclic_dependency"
    FILE_IO = "file_io_error"
    OTHER = "other"
    NO_ERROR = "no_error"


# Pattern -> class mapping (order matters: more specific first)
_ERROR_PATTERNS: list[tuple[re.Pattern, CompileErrorClass]] = [
    # Timing
    (re.compile(r"(?i)\bheartbeat\b|\btimeout\b|maxRecDepth"), CompileErrorClass.TIMEOUT),
    (re.compile(r"(?i)\bout of memory\b|memory\s+exhausted"), CompileErrorClass.MEMORY),

    # Tactic / goal state (checked early: these are the most frequent real-Lean
    # failures, and their messages contain none of the keywords matched below)
    (re.compile(r"unsolved goals?"), CompileErrorClass.UNSOLVED_GOAL),
    (re.compile(r"(?i)Tactic\s+[`'][^`']+[`']\s+failed"), CompileErrorClass.TACTIC_FAILED),
    (re.compile(r"(?i)\btactic\b[^\n]{0,48}\bfailed"), CompileErrorClass.TACTIC_FAILED),
    (re.compile(r"(?i)\btactic\s+(failed|did not)"), CompileErrorClass.TACTIC_FAILED),

    # Identifiers
    (re.compile(r"unknown\s+(identifier|constant|declaration|tactic)"), CompileErrorClass.UNKNOWN_IDENT),

    # Types
    (re.compile(r"type mismatch"), CompileErrorClass.TYPE_MISMATCH),
    (re.compile(r"is not a proposition"), CompileErrorClass.TYPE_MISMATCH),
    (re.compile(r"type of theorem"), CompileErrorClass.TYPE_MISMATCH),
    (re.compile(r"function\s+expected|expected.*function"), CompileErrorClass.FUNCTION_EXPECTED),
    (re.compile(r"failed to synthesize"), CompileErrorClass.FAILED_SYNTHESIS),
    (re.compile(r"don't know how to"), CompileErrorClass.FAILED_SYNTHESIS),
    (re.compile(r"can't find"), CompileErrorClass.FAILED_SYNTHESIS),

    # Syntax
    (re.compile(r"syntax\s+error"), CompileErrorClass.SYNTAX_ERROR),
    (re.compile(r"unexpected"), CompileErrorClass.SYNTAX_ERROR),

    # Universe
    (re.compile(r"universe"), CompileErrorClass.UNIVERSE),

    # Other
    (re.compile(r"unused\s+(variable|parameter)"), CompileErrorClass.UNUSED_VARIABLE),
    (re.compile(r"ambiguous"), CompileErrorClass.AMBIGUOUS),
    (re.compile(r"cyclic"), CompileErrorClass.CYCLIC_DEP),
    (re.compile(r"(?i)\b(file|IO)\b"), CompileErrorClass.FILE_IO),
]


def classify_compile_error(diagnostic: str) -> CompileErrorClass:
    """Classify a single Lean diagnostic line.

    NOTE: ``parse_lean_diagnostics`` (t2_real) strips the ``error:``/``warning:``
    prefix into a separate ``severity`` field, so an *unrecognised* error message
    arrives here **without** the literal ``"error:"`` token. Returning ``NO_ERROR``
    in that case would silently label a real failure as success, so unrecognised
    non-empty messages fall back to ``OTHER`` instead. Use
    ``classify_diagnostic_entry`` when the severity field is available.
    """
    if not diagnostic or diagnostic.isspace():
        return CompileErrorClass.OTHER

    for pattern, cls in _ERROR_PATTERNS:
        if pattern.search(diagnostic):
            return cls

    if "error:" in diagnostic or "warning:" in diagnostic:
        return CompileErrorClass.OTHER
    if diagnostic.startswith("<stdin>"):
        return CompileErrorClass.OTHER

    # Unrecognised, but non-empty => a real message we simply cannot name yet.
    return CompileErrorClass.OTHER


def classify_diagnostic_entry(entry: dict) -> CompileErrorClass:
    """Classify one parsed diagnostic *entry* (severity-aware).

    ``severity == "error"`` is the ground truth from Lean; if the message text
    cannot be matched to a named class, the result must be ``OTHER`` - never
    ``NO_ERROR``. ``NO_ERROR`` is reserved for entries that Lean did not flag as
    errors or warnings at all (e.g. trailing ``⊢ goal`` context lines).
    """
    msg = (entry.get("message") or "").strip()
    severity = (entry.get("severity") or "").lower()

    cls = classify_compile_error(msg) if msg else CompileErrorClass.NO_ERROR
    if severity in ("error", "warning"):
        # Lean said this is a problem - never report it as NO_ERROR.
        return CompileErrorClass.OTHER if cls is CompileErrorClass.NO_ERROR else cls
    if cls is CompileErrorClass.OTHER:
        # info/note-level context lines (e.g. a trailing "⊢ goal") are not errors;
        # counting them as OTHER pollutes the diagnosis heat buckets.
        return CompileErrorClass.NO_ERROR
    return cls


def classify_diagnostics(diagnostics: list[dict]) -> dict[CompileErrorClass, int]:
    """Classify a list of Lean diagnostics. Returns count per class."""
    counts: dict[CompileErrorClass, int] = {}
    for d in diagnostics:
        cls = classify_diagnostic_entry(d)
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def is_dead_loop(errors_seen: dict[CompileErrorClass, int],
                 threshold: int = 3) -> tuple[bool, CompileErrorClass | None]:
    """Check if proof is stuck in a dead loop (same error class >= threshold)."""
    for cls, count in errors_seen.items():
        if cls == CompileErrorClass.NO_ERROR or cls == CompileErrorClass.OTHER:
            continue
        if count >= threshold:
            return True, cls
    return False, None

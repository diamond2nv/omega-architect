#!/usr/bin/env python3
"""Compile error classifier — 13 classes.

Parses lean --stdin diagnostic output and classifies errors.
Replaces string-equality dead-loop detection with classification.
"""

from __future__ import annotations

import re
from enum import Enum


class CompileErrorClass(Enum):
    """13 classes of Lean compile errors."""
    UNKNOWN_IDENT = "unknown_identifier"
    TYPE_MISMATCH = "type_mismatch"
    SYNTAX_ERROR = "syntax_error"
    UNIVERSE = "universe_constraint"
    FUNCTION_EXPECTED = "function_expected"
    UNUSED_VARIABLE = "unused_variable"
    FAILED_SYNTHESIS = "failed_to_synthesize"
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
    """Classify a single Lean diagnostic line."""
    if not diagnostic or diagnostic.isspace():
        return CompileErrorClass.OTHER

    for pattern, cls in _ERROR_PATTERNS:
        if pattern.search(diagnostic):
            return cls

    if "error:" in diagnostic or "warning:" in diagnostic:
        return CompileErrorClass.OTHER
    if diagnostic.startswith("<stdin>"):
        return CompileErrorClass.OTHER

    return CompileErrorClass.NO_ERROR


def classify_diagnostics(diagnostics: list[dict]) -> dict[CompileErrorClass, int]:
    """Classify a list of Lean diagnostics. Returns count per class."""
    counts: dict[CompileErrorClass, int] = {}
    for d in diagnostics:
        msg = d.get("message", "")
        cls = classify_compile_error(msg)
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

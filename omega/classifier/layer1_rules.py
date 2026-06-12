"""Layer 1: Rule-Based Fast Path — extended regex patterns for known Lean errors.

<1ms, ~95% precision for 30+ known error patterns.
Returns None for unrecognised errors → falls through to Layer 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── New error classes beyond CompileErrorClass ──────────────────

# We use string labels to stay independent of omega.loop.errors.
# The vote.py layer maps these back to CompileErrorClass.

KNOWN_CLASSES: list[str] = [
    "UNKNOWN_IDENT",
    "CAST_NEEDED",
    "SYNTHESIS_FAILED",
    "IMPORT_POSITION",
    "SYNTAX_ERROR",
    "FUNCTION_EXPECTED",
    "TIMEOUT",
    "TYPE_MISMATCH",
    "UNSOLVED_GOAL",
    "TACTIC_FAILED",
    "INCOMPLETE_BLOCK",
    "UNKNOWN_MODULE",
    "AMBIGUOUS",
    "RECURSIVE_LIMIT",
    "AUTO_PARAM",
    "DONT_KNOW_HOW_TO",
    "MISSING_INSTANCE",
    "OVERFLOW",
    "OTHER",
]

# ── Pattern library ─────────────────────────────────────────────


@dataclass
class PatternMatch:
    """Result from matching a known pattern."""

    class_label: str
    params: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.95
    matched_text: str = ""


# Pattern tuple: (regex, class_label, param_extract_fn_or_None)
# param_extract_fn receives the match object and returns a dict of extracted values.

_PATTERNS: list[tuple[re.Pattern, str, callable | None]] = [
    # ── UNKNOWN_IDENT ──
    (re.compile(r"unknown identifier\s+'([^']+)'"), "UNKNOWN_IDENT", lambda m: {"ident": m.group(1)}),
    (re.compile(r"unknown\s+(identifier|constant|theorem|lemma|declaration)"), "UNKNOWN_IDENT", lambda m: {"ident": m.group(1)}),
    (re.compile(r"unknown declaration\s+'([^']+)'"), "UNKNOWN_IDENT", lambda m: {"ident": m.group(1)}),
    # ── SYNTHESIS_FAILED ──
    (re.compile(r"failed to synthesize instance"), "SYNTHESIS_FAILED", None),
    (re.compile(r"don't know how to synthesize"), "SYNTHESIS_FAILED", None),
    (re.compile(r"can't synthesize"), "SYNTHESIS_FAILED", None),
    # ── CAST_NEEDED ──
    (re.compile(r"type mismatch.*ℕ.*ℝ"), "CAST_NEEDED", lambda m: {"from": "ℕ", "to": "ℝ"}),
    (re.compile(r"type mismatch.*ℝ.*ℕ"), "CAST_NEEDED", lambda m: {"from": "ℝ", "to": "ℕ"}),
    (re.compile(r"type mismatch.*\bNat\b.*\b(Real|ℕ)\b"), "CAST_NEEDED", lambda m: {"from": "Nat"}),
    # ── TYPE_MISMATCH (general) ──
    (re.compile(r"type mismatch"), "TYPE_MISMATCH", None),
    (re.compile(r"has type\s+.*\n?\s*but is expected to have type"), "TYPE_MISMATCH", None),
    (re.compile(r"but is expected to have type"), "TYPE_MISMATCH", None),
    (re.compile(r"cannot apply"), "TYPE_MISMATCH", None),
    # ── SYNTAX_ERROR ──
    (re.compile(r"syntax error"), "SYNTAX_ERROR", None),
    (re.compile(r"expected\s+'[^']*'"), "SYNTAX_ERROR", None),
    (re.compile(r"unexpected\s+(token|symbol|';'|ident)"), "SYNTAX_ERROR", None),
    (re.compile(r"end of input"), "SYNTAX_ERROR", None),
    (re.compile(r"invalid\s+token"), "SYNTAX_ERROR", None),
    (re.compile(r"unclosed\s+(block|match|paren|bracket)"), "SYNTAX_ERROR", None),
    # ── IMPORT_POSITION ──
    (re.compile(r"invalid\s+'import'\s+command"), "IMPORT_POSITION", None),
    (re.compile(r"import statements must be"), "IMPORT_POSITION", None),
    # ── FUNCTION_EXPECTED ──
    (re.compile(r"function expected at"), "FUNCTION_EXPECTED", None),
    (re.compile(r"function is missing"), "FUNCTION_EXPECTED", None),
    # ── TIMEOUT ──
    (re.compile(r"timeout?"), "TIMEOUT", None),
    (re.compile(r"interrupted"), "TIMEOUT", None),
    (re.compile(r"maximum number of heartbeats"), "TIMEOUT", None),
    # ── UNSOLVED_GOAL ──
    (re.compile(r"unsolved goals?"), "UNSOLVED_GOAL", None),
    (re.compile(r"goal\s*:\s*$", re.MULTILINE), "UNSOLVED_GOAL", None),
    # ── TACTIC_FAILED ──
    (re.compile(r"tactic\s+'?(\w+)'?\s+failed"), "TACTIC_FAILED", lambda m: {"tactic": m.group(1)}),
    (re.compile(r"tactic\s+(\w+)\s+failed"), "TACTIC_FAILED", lambda m: {"tactic": m.group(1)}),
    (re.compile(r"no applicable"), "TACTIC_FAILED", None),
    # ── INCOMPLETE_BLOCK ──
    (re.compile(r"missing\s+body"), "INCOMPLETE_BLOCK", None),
    (re.compile(r"expected\s+body"), "INCOMPLETE_BLOCK", None),
    (re.compile(r"`:=` expected"), "INCOMPLETE_BLOCK", None),
    # ── UNKNOWN_MODULE ──
    (re.compile(r"unknown module"), "UNKNOWN_MODULE", None),
    (re.compile(r"module\s+not\s+found"), "UNKNOWN_MODULE", None),
    (re.compile(r"could not resolve"), "UNKNOWN_MODULE", None),
    # ── AMBIGUOUS ──
    (re.compile(r"ambiguous"), "AMBIGUOUS", None),
    (re.compile(r"overload"), "AMBIGUOUS", None),
    # ── RECURSIVE_LIMIT ──
    (re.compile(r"recursion\s+limit"), "RECURSIVE_LIMIT", None),
    (re.compile(r"deep recursion"), "RECURSIVE_LIMIT", None),
    # ── AUTO_PARAM ──
    (re.compile(r"auto\s+param"), "AUTO_PARAM", None),
    # ── DONT_KNOW_HOW_TO ──
    (re.compile(r"don't know how to"), "DONT_KNOW_HOW_TO", None),
    # ── MISSING_INSTANCE ──
    (re.compile(r"instance\s+(of|for)\s+\S+\s+is\s+missing"), "MISSING_INSTANCE", None),
    (re.compile(r"no\s+instance"), "MISSING_INSTANCE", None),
    # ── OVERFLOW ──
    (re.compile(r"overflow"), "OVERFLOW", None),
    (re.compile(r"stack overflow"), "OVERFLOW", None),
]

# ── Correction strategies ───────────────────────────────────────

_STRATEGIES: dict[str, str] = {
    "UNKNOWN_IDENT": "Search Mathlib with leansearch/loogle for the missing lemma; check import",
    "CAST_NEEDED": "Add explicit cast: use ``Nat.cast``, ``Int.cast``, or ``(x : ℝ)`` syntax",
    "SYNTHESIS_FAILED": "Add ``open scoped`` or provide explicit instance with ``haveI``",
    "IMPORT_POSITION": "Move all ``import`` statements to the top of the file",
    "SYNTAX_ERROR": "Reformat the proof block — check parentheses, indentation, trailing colons",
    "FUNCTION_EXPECTED": "Check the function signature; you may need to apply/refine differently",
    "TIMEOUT": "Reduce proof complexity — split into smaller lemmas or use more direct reasoning",
    "TYPE_MISMATCH": "Use ``exact`` or ``apply`` with explicit type annotation; check term types",
    "UNSOLVED_GOAL": "Try alternative tactic (omega / arith / nlinarith / simp / ring)",
    "TACTIC_FAILED": "Switch to a different tactic family (e.g. induction → cases → omega)",
    "INCOMPLETE_BLOCK": "Fill in the missing proof body — ``:= by`` block is empty",
    "UNKNOWN_MODULE": "Add the missing ``import`` statement",
    "AMBIGUOUS": "Add namespace prefix or type annotation to disambiguate",
    "RECURSIVE_LIMIT": "Rewrite with ``partial`` or restructure to avoid deep recursion",
    "AUTO_PARAM": "Provide explicit argument; autoparam inference failed",
    "DONT_KNOW_HOW_TO": "Try ``aesop``, ``simp``, or a different approach entirely",
    "MISSING_INSTANCE": "Provide the missing instance with ``have`` or ``haveI``",
    "OVERFLOW": "Reduce recursion depth; consider iterative approach",
    "OTHER": "Fallback: try simp / trivial / rfl and escalate",
}


def match_known_pattern(error_msg: str) -> PatternMatch | None:
    """Match a Lean error message against known patterns.

    Returns a PatternMatch with class_label, params, and confidence,
    or None if no pattern matches.
    """
    if not error_msg or not error_msg.strip():
        return None

    msg = error_msg.strip()

    for pattern, class_label, extract_fn in _PATTERNS:
        m = pattern.search(msg)
        if m is not None:
            params = extract_fn(m) if extract_fn else {}
            return PatternMatch(
                class_label=class_label,
                params=params,
                confidence=0.95,
                matched_text=m.group(0)[:80],
            )

    return None


def strategy_for(class_label: str) -> str:
    """Get the correction strategy for a given class label."""
    return _STRATEGIES.get(class_label, _STRATEGIES["OTHER"])


def fix_strategies_for(class_label: str) -> list[str]:
    """Get fix strategies as a list."""
    return [strategy_for(class_label)]

#!/usr/bin/env python3
"""T2 Verifier: full Lean compiler verification (~30s target).

T2 is the authoritative gate — it actually runs the Lean compiler on
the code, catching all type errors, missing theorems, and proof failures
that T1's structural checks can't see.

Two verification modes:
  1. **Code snippet** (default): uses ``lean_run_code`` MCP tool to
     compile a self-contained theorem + proof.  Fast, no project setup.
  2. **Project build** (fallback): uses ``lean_build`` on a Lean project
     directory.  Slower but catches real-world project issues.

Interface: the actual MCP tool call is abstracted behind a callback,
so the T2 module is pure Python (no MCP dependency) and fully testable
with mock data.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# ── data models ────────────────────────────────────────────────


@dataclass
class T2Result:
    """Result from Lean compiler verification."""

    verified: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def summary(self) -> str:
        if self.verified:
            return f"✅ Pass ({self.elapsed_ms}ms)"
        return f"❌ Fail ({self.elapsed_ms}ms, {len(self.errors)} errors)"


# ── code formatting ────────────────────────────────────────────


# Boilerplate preamble injected when no imports are detected.
_PREAMBLE = """import Mathlib
open Real Complex

set_option pp.fieldNotation false
"""


def format_code(code: str) -> str:
    """Ensure the code is self-contained for lean_run_code.

    If the code has no explicit ``import`` or ``open`` statements,
    prepend a minimal Mathlib preamble so the theorem can compile.
    This is safe for most physics/math theorems.
    """
    stripped = code.strip()
    if not stripped:
        return _PREAMBLE + "\n-- empty theorem"
    has_import = bool(re.search(r"^(import|open)\s", stripped, re.MULTILINE))
    if not has_import:
        return _PREAMBLE + "\n" + stripped
    return stripped


# ── diagnostics parsing ────────────────────────────────────────


def parse_diagnostics(raw: dict | list | str | None) -> T2Result:
    """Parse MCP ``lean_run_code`` / ``lean_diagnostic_messages`` output.

    Accepts several shapes:
      - ``dict`` with ``diagnostics`` key (MCP return)
      - ``list`` of diagnostic dicts
      - ``str`` (JSON string or error message)

    Returns
    -------
    T2Result
    """
    errors: list[str] = []
    warnings: list[str] = []

    if raw is None:
        return T2Result(verified=False, errors=["No response from Lean compiler"])

    # Unwrap
    if isinstance(raw, str):
        try:
            import json

            parsed = json.loads(raw)
        except ValueError:
            return T2Result(verified=False, errors=[raw])
    elif isinstance(raw, dict):
        # MCP shape: {"severity": "error", ...} or {"diagnostics": [...]}
        if "diagnostics" in raw:
            parsed = raw["diagnostics"]
        elif "severity" in raw:
            parsed = [raw]
        else:
            errors.append(str(raw))
            return T2Result(verified=False, errors=errors)
    elif isinstance(raw, list):
        parsed = raw
    else:
        return T2Result(verified=False, errors=[f"Unexpected type: {type(raw).__name__}"])

    # Parse each entry
    for diag in parsed:
        if not isinstance(diag, dict):
            continue
        message = diag.get("message", diag.get("fullMessage", str(diag)))
        severity = diag.get("severity", "error")
        pos = diag.get("pos", diag.get("position", None))
        if pos:
            line = pos.get("line", "?")
            col = pos.get("character", pos.get("column", "?"))
            message = f"[{line}:{col}] {message}"

        if severity in ("warning", "info", "hint"):
            warnings.append(message)
        else:
            errors.append(message)

    return T2Result(
        verified=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


# ── verify (callback-based) ────────────────────────────────────


CompileFn = Callable[[str], dict | list | str | None]


def verify(code: str, compile_fn: CompileFn | None = None) -> T2Result:
    """Run T2 verification on Lean code.

    Parameters
    ----------
    code : str
        Lean 4 code (theorem + proof).  Must be self-contained or
        ``format_code`` will prepend Mathlib imports.
    compile_fn : callable, optional
        Takes formatted code, returns raw compiler output
        (dict, list, str, or None).  If omitted, returns a description
        of what would be compiled.

    Returns
    -------
    T2Result
    """
    formatted = format_code(code)

    if compile_fn is None:
        return T2Result(
            verified=False,
            errors=["No compile_fn provided — T2 compiler not available"],
            warnings=[f"Code would be compiled:\n{formatted[:200]}..."],
        )

    t0 = time.perf_counter()
    try:
        raw = compile_fn(formatted)
    except Exception as e:
        return T2Result(
            verified=False,
            errors=[f"T2 internal error: {e}"],
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    elapsed = int((time.perf_counter() - t0) * 1000)

    result = parse_diagnostics(raw)
    result.elapsed_ms = elapsed
    return result


# ── T2 prompt template (for agent delegation) ──────────────────


T2_VERIFY_PROMPT = """You are a Lean 4 compiler assistant.
Use the `lean_run_code` MCP tool to compile the following Lean code.
Report ALL compiler errors and warnings verbatim.

Code to compile:
```lean4
{code}
```

After running, use `lean_diagnostic_messages` to get any remaining diagnostics.
Return the results as JSON:
{{"verified": true/false, "errors": [...], "warnings": [...], "elapsed_ms": int}}
"""


# ── known theorem store (for regression testing) ────────────────


# Simple theorems known to compile cleanly on Mathlib 4.
_CANONICAL_EXAMPLES: dict[str, str] = {
    "add_zero": """
theorem add_zero (n : ℕ) : n + 0 = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp [ih]
""",
    "zero_add": """
theorem zero_add (n : ℕ) : 0 + n = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp [ih]
""",
    "mul_comm_basic": """
theorem mul_comm_basic (a b : ℕ) : a * b = b * a := by
  induction a with
  | zero => simp
  | succ a ih => simp [add_comm, add_left_comm, add_assoc, ih]
""",
}


def get_canonical(name: str) -> str | None:
    """Return a canonical theorem by name, or None if unknown."""
    return _CANONICAL_EXAMPLES.get(name)

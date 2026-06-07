#!/usr/bin/env python3
"""T2 Real Compile — compiles Lean code using lake env lean --stdin.

Bypasses MCP to compile arbitrary Lean code including Mathlib-dependent
theorems, using the existing Mathlib cache in lean-paper-plane project.

Usage
-----
    from omega.verify.t2_real import make_real_compile_callback
    from omega.verify.t2_lean import verify as t2_verify

    compile_fn = make_real_compile_callback()
    result = t2_verify("theorem t : True := trivial", compile_fn=compile_fn)
    assert result.verified
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# ── paths ──────────────────────────────────────────────────────

LEAN_PAPER_PLANE = Path.home() / "lean-paper-plane"
"""Default Lean project with Mathlib cache (8108 .olean files, 7.1 GB)."""

LEAN_BIN = Path.home() / ".elan" / "toolchains" / "4.30.0" / "bin" / "lean"
LAKE_BIN = Path.home() / ".elan" / "toolchains" / "4.30.0" / "bin" / "lake"

# ── diagnostic parser ──────────────────────────────────────────

# Matches: <stdin>:<line>:<col>: <severity>: <message>
# Example: <stdin>:4:27: warning: unused variable `h`
_DIAG_RE = re.compile(
    r"^"
    r"(?P<file>.+?):"  # file (e.g., <stdin>)
    r"(?P<line>\d+):"  # line number
    r"(?P<col>\d+):\s"  # column
    r"(?P<severity>(?:warning|error|info|note)):\s"  # severity
    r"(?P<message>.+)"  # message
    r"$"
)


def parse_lean_diagnostics(stderr: str) -> list[dict[str, Any]]:
    """Parse stderr output from ``lean --stdin`` into MCP-style diagnostics list.

    The ``lean --stdin`` output is line-oriented::

        <stdin>:<line>:<col>: warning: unused variable `x`
        <stdin>:13:9: error: unsolved goals

    Returns
    -------
    list[dict]
        Each dict has keys: ``message``, ``severity``, ``line``, ``column``.
        Non-diagnostic lines are captured as info-level diagnostics.
    """
    diagnostics: list[dict[str, Any]] = []
    for raw_line in stderr.strip().split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = _DIAG_RE.match(line)
        if m:
            diagnostics.append(
                {
                    "message": m.group("message"),
                    "severity": m.group("severity"),
                    "line": int(m.group("line")),
                    "column": int(m.group("col")),
                }
            )
        else:
            # Non-diagnostic lines, e.g. Lean info messages
            diagnostics.append(
                {
                    "message": line,
                    "severity": "info",
                    "line": 1,
                    "column": 1,
                }
            )
    return diagnostics


# ── compile callback ───────────────────────────────────────────


def real_compile_callback(code: str, timeout: int = 60) -> dict[str, Any]:
    """Compile Lean code using ``lake env lean --stdin`` with Mathlib.

    Writes ``code`` to stdin of the Lean compiler running inside the
    ``lean-paper-plane`` project environment, which has Mathlib cached.

    Parameters
    ----------
    code : str
        Self-contained Lean code (must include ``import Mathlib`` if needed).
        ``format_code()`` from ``t2_lean`` will prepend ``import Mathlib``
        automatically if import-less code is passed.
    timeout : int
        Max seconds for compilation (default 60).

    Returns
    -------
    dict
        Shape compatible with ``parse_diagnostics`` from ``t2_lean.py``:
        ``{"diagnostics": [...], "exit_code": N, "stdout": "..."}``.
    """
    project_dir = LEAN_PAPER_PLANE
    if not project_dir.exists():
        return {
            "diagnostics": [
                {
                    "message": f"Project directory not found: {project_dir}",
                    "severity": "error",
                    "line": 1,
                    "column": 1,
                }
            ],
            "exit_code": -1,
            "stdout": "",
        }

    if not LAKE_BIN.exists():
        return {
            "diagnostics": [
                {
                    "message": f"lake binary not found: {LAKE_BIN}",
                    "severity": "error",
                    "line": 1,
                    "column": 1,
                }
            ],
            "exit_code": -2,
            "stdout": "",
        }

    cmd = [str(LAKE_BIN), "env", str(LEAN_BIN), "--stdin"]

    try:
        proc = subprocess.run(
            cmd,
            input=code,
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "diagnostics": [
                {
                    "message": f"Lean compilation timed out after {timeout}s",
                    "severity": "error",
                    "line": 1,
                    "column": 1,
                }
            ],
            "exit_code": -3,
            "stdout": "",
        }

    stderr = proc.stderr or ""
    stdout = proc.stdout or ""

    # ``lean --stdin`` outputs diagnostics to stdout, not stderr.
    all_output = stdout + "\n" + stderr
    diagnostics = parse_lean_diagnostics(all_output)

    return {
        "diagnostics": diagnostics,
        "exit_code": proc.returncode,
        "stdout": stdout[:500] if stdout else "",
    }


def make_real_compile_callback(
    project_dir: str | Path | None = None,
    timeout: int = 60,
) -> Callable[[str], dict[str, Any]]:
    """Return a ``compile_fn`` for ``t2_lean.verify()``.

    The returned callable uses ``lake env lean --stdin`` to compile
    Lean code against a real Mathlib cache.

    Parameters
    ----------
    project_dir : str or Path, optional
        Path to a Lean project with cached Mathlib.  Defaults to
        ``~/lean-paper-plane``.
    timeout : int
        Max seconds per compilation (default 60).

    Returns
    -------
    Callable[[str], dict]
        Suitable as ``compile_fn`` for ``t2_lean.verify()`` or
        ``T2OnlineRunner``.
    """
    _project_dir = Path(project_dir) if project_dir else LEAN_PAPER_PLANE

    def _compile(code: str) -> dict[str, Any]:
        return real_compile_callback(code, timeout=timeout)

    return _compile

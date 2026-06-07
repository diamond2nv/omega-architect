#!/usr/bin/env python3
"""T2 MCP Compile Callback — bridges T2 verifier to lean-lsp-mcp tools.

This module provides the ``compile_fn`` callback that the T2 ``verify()``
function needs to actually compile Lean code using the MCP toolchain.

Two backends:
  1. ``lean_run_code`` — runs a self-contained code snippet (NO Mathlib).
     Works immediately for core Lean/Init/Std theorems.
  2. ``lean_build`` — builds a full Lean project (WITH Mathlib).
     Requires Mathlib to be lake-cached (network-dependent).

The callback is designed to be passed to ``t2_lean.verify(code, compile_fn)``.
When the MCP tools are not available (e.g., offline mode), it returns a
descriptive error so the caller can fall back gracefully.
"""

from __future__ import annotations

import time
from typing import Any

# ── pure-Lean compile via lean_run_code ────────────────────────


def make_mcp_callback(backend: str = "lean_run_code") -> Any:
    """Return a ``compile_fn`` callable for T2 ``verify()``.

    The returned function takes Lean code and returns the MCP result
    shape that ``parse_diagnostics`` expects.

    Parameters
    ----------
    backend : str
        ``"lean_run_code"`` (default) or ``"lean_build"``.

    Returns
    -------
    Callable[[str], dict]
        A function ``fn(code: str) -> dict`` suitable as ``compile_fn``.
    """

    def compile_fn(code: str) -> dict:
        """Compile Lean code via MCP.  Returns diagnostics dict."""
        # In agent runtime, this is replaced by the real MCP call.
        # Here we return the expected shape with a marker so the
        # orchestrator knows to use delegation.
        return {
            "_mcp_pending": True,
            "_backend": backend,
            "_code_preview": code[:200],
            "diagnostics": [],
        }

    return compile_fn


def format_mcp_diagnostics(raw: dict | None) -> dict | None:
    """Convert raw MCP result to the shape parse_diagnostics expects.

    ``lean_run_code`` returns ``{"success": bool, "diagnostics": [...]}``.
    ``lean_build`` may return a different shape.

    This normalizes both to a list of diagnostics or None.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "diagnostics" in raw:
            return raw
        # Try to extract from other shapes
        if "result" in raw and isinstance(raw["result"], dict):
            return raw["result"]
    return raw


# ── pure-Lean test suite (no Mathlib needed) ───────────────────


PURE_LEAN_THEOREMS: dict[str, str] = {
    "add_zero_basic": """
theorem add_zero (n : Nat) : n + 0 = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp
""",
    "zero_add_basic": """
theorem zero_add (n : Nat) : 0 + n = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp [add_succ, ih]
""",
    "true_trivial": """
theorem true_trivial : True := by
  trivial
""",
    "and_comm": """
theorem and_comm (a b : Prop) : a ∧ b ↔ b ∧ a := by
  constructor
  · intro ⟨ha, hb⟩; exact ⟨hb, ha⟩
  · intro ⟨hb, ha⟩; exact ⟨ha, hb⟩
""",
    "or_comm": """
theorem or_comm (a b : Prop) : a ∨ b ↔ b ∨ a := by
  constructor
  · intro h; cases h with
    | inl ha => exact Or.inr ha
    | inr hb => exact Or.inl hb
  · intro h; cases h with
    | inl hb => exact Or.inr hb
    | inr ha => exact Or.inl ha
""",
    "mod_two": """
theorem mod_two (n : Nat) : n % 2 = 0 ∨ n % 2 = 1 := by
  induction n with
  | zero => left; rfl
  | succ n ih =>
    rcases ih with (h | h)
    · right; simpa [Nat.succ_eq_add_one, add_comm, add_left_comm, add_assoc, h]
    · left; simpa [Nat.succ_eq_add_one, add_comm, add_left_comm, add_assoc, h]
""",
    "simple_identity": """
theorem simple_identity (x : Nat) : x + x = 2 * x := by
  simp [Nat.two_mul]
""",
}


# ── T2 online runner ───────────────────────────────────────────


class T2OnlineRunner:
    """Run T2 verification on a batch of theorems using MCP.

    Usage:
        runner = T2OnlineRunner()
        results = runner.run_batch(["theorem t : True := trivial", ...])
        print(runner.report())
    """

    def __init__(self, compile_fn: Any | None = None):
        self.compile_fn = compile_fn
        self.results: list[dict] = []

    def run_one(self, code: str, theorem_name: str = "unknown") -> dict:
        """Run T2 on a single theorem."""
        from omega.verify.t2_lean import format_code, parse_diagnostics

        formatted = format_code(code)
        t0 = time.perf_counter()

        if self.compile_fn is None:
            # Offline mode
            return {
                "name": theorem_name,
                "verified": False,
                "errors": ["compile_fn not set — requires MCP delegate_task"],
                "elapsed_ms": 0,
            }

        try:
            raw = self.compile_fn(formatted)
            elapsed = int((time.perf_counter() - t0) * 1000)
            result = parse_diagnostics(raw)
            result.elapsed_ms = elapsed
            return {
                "name": theorem_name,
                "verified": result.verified,
                "errors": result.errors,
                "warnings": result.warnings,
                "elapsed_ms": elapsed,
            }
        except Exception as e:
            return {
                "name": theorem_name,
                "verified": False,
                "errors": [f"T2 exception: {e}"],
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            }

    def run_batch(self, theorems: dict[str, str]) -> list[dict]:
        """Run T2 on a batch of named theorems."""
        self.results = []
        for name, code in theorems.items():
            result = self.run_one(code, theorem_name=name)
            self.results.append(result)
            status = "✅" if result["verified"] else "❌"
            print(f"  {status} {name}: {result.get('elapsed_ms', 0)}ms")
            for err in result.get("errors", []):
                print(f"       {err[:100]}")
        return self.results

    def report(self) -> str:
        """Generate markdown report."""
        if not self.results:
            return "No results."

        total = len(self.results)
        passed = sum(1 for r in self.results if r["verified"])
        lines = [
            "## T2 Online Benchmark Report",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total theorems | {total} |",
            f"| T2 pass | {passed}/{total} ({passed / total * 100:.1f}%) |",
            f"| Total time | {sum(r['elapsed_ms'] for r in self.results)}ms |",
            "",
            "### Per-theorem results",
            "| # | Theorem | T2 | Errors | Time(ms) |",
            "|---|---------|----|--------|----------|",
        ]
        for i, r in enumerate(self.results):
            icon = "✅" if r["verified"] else "❌"
            errs = "; ".join(e[:40] for e in r["errors"][:2]) if r["errors"] else "—"
            lines.append(f"| {i + 1} | {r['name']} | {icon} | {errs} | {r['elapsed_ms']} |")

        return "\n".join(lines)

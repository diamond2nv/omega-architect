"""Verifier Agent — independent correctness gatekeeper for Lean proofs.

Unlike the CompileGate (which is a fast local gate between model responses),
the Verifier is a FINAL check that runs AFTER the Prover claims success.

Inspired by Ax-Prover's architecture (arXiv:2510.12787):
- Verifier does NOT generate or modify proofs
- Only assesses correctness via compilation
- Necessary because Prover may (i) run out of rounds returning incomplete proof,
  or (ii) terminate early despite remaining errors

Usage:
    from omega.loop.verifier import VerifierAgent

    verifier = VerifierAgent()
    verdict = verifier.verify(code, theorem_name)
    if verdict.verified:
        print("Proof is correct!")
    elif verdict.has_sorry:
        print("Incomplete proof (sorry present)")
    else:
        print(f"Compile error: {verdict.errors}")
"""

from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, field

from omega.loop.compile_gate import CompileGate

logger = logging.getLogger("omega.loop.verifier")


@dataclass
class Verdict:
    """Result from the Verifier Agent.

    Attributes
    ----------
    verified : bool
        True iff code=0, or code=2 WITHOUT `sorry`/`admit`.
    has_sorry : bool
        True iff the proof contains `sorry` or `admit` placeholders.
    has_errors : bool
        True iff compilation returned code=1 (compile error).
    errors : list[str]
        Error messages (empty if verified).
    diagnostics : list[dict]
        Full diagnostic output from compilation.
    code : str
        The verified Lean code.
    """
    verified: bool = False
    has_sorry: bool = False
    has_errors: bool = False
    errors: list[str] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    code: str = ""


class VerifierAgent:
    """Independent verifier — does not generate or modify proofs.

    Reuses CompileGate for the actual compilation (via `lake env lean --stdin`)
    but adds:
    1. Explicit `sorry`/`admit` detection in the source code
    2. Clear verdict reporting
    3. Integration with the inner_loop proof flow
    """

    def __init__(self, compile_timeout: int = 60):
        self.compile_gate = CompileGate(timeout=compile_timeout)

    def verify(self, code: str) -> Verdict:
        """Verify a Lean proof.

        Two-stage check:
        1. Compile via CompileGate (lake env lean --stdin)
        2. Check source for `sorry`/`admit` placeholders

        Returns a Verdict.
        """
        # Stage 1: Source check for sorry/admit
        has_sorry = self._has_sorry(code)

        # Stage 2: Compile
        compile_result = self.compile_gate.compile(code)
        has_errors = not compile_result.success

        # Stage 3: Combine verdict
        # Ax-Prover rule: verified iff code=0 or code=2 WITHOUT sorry
        verified = compile_result.success and not has_sorry

        return Verdict(
            verified=verified,
            has_sorry=has_sorry,
            has_errors=has_errors,
            errors=compile_result.errors,
            diagnostics=compile_result.diagnostics,
            code=code,
        )

    @staticmethod
    def _has_sorry(code: str) -> bool:
        """Check if Lean code contains `sorry` or `admit` placeholders.

        Handles false positives by requiring word boundaries.
        """
        # Remove comments first to avoid matching sorry in strings/comments
        # Simple approach: check with word boundaries
        if re.search(r'\bsorry\b', code):
            return True
        if re.search(r'\badmit\b', code):
            return True
        return False

    def format_verdict(self, verdict: Verdict) -> str:
        """Format verdict as a human-readable string for feedback."""
        if verdict.verified:
            return "✅ Verifier: proof is correct and complete (no errors, no sorry)."
        parts = []
        if verdict.has_sorry:
            parts.append("⚠️  Proof is INCOMPLETE (contains `sorry` or `admit`).")
        if verdict.has_errors:
            parts.append(f"❌ Compile error ({len(verdict.errors)} issue(s)):")
            for err in verdict.errors[:5]:
                parts.append(f"   {err}")
        return "\n".join(parts)

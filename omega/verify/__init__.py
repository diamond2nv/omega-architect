"""Verification modules.

Tier 1 (T1): Fast structural checks for Lean 4 code (~5s).
  - Pattern-based: unclosed blocks, sorry, missing bodies, imports
  - LLM-assisted: semantic structural review (optional)
  - No compiler invocation

Tier 2 (T2): Full Lean compiler verification (~30s).
  - WIP: lean-lsp-mcp integration
"""
from .t1_llm import (
    T1_VERIFY_PROMPT,
    VerificationResult,
    llm_verify,
    pattern_verify,
    verify,
)

__all__ = [
    "VerificationResult",
    "pattern_verify",
    "llm_verify",
    "verify",
    "T1_VERIFY_PROMPT",
]

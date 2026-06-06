"""Verification modules.

Tier 1 (T1): Fast structural checks for Lean 4 code (~5s).
  - Pattern-based: unclosed blocks, sorry, missing bodies, imports
  - LLM-assisted: semantic structural review (optional)
  - No compiler invocation

Tier 2 (T2): Full Lean compiler verification (~30s).
  - Uses ``lean_run_code`` MCP tool for fast per-theorem compilation
  - ``lean_build`` for full project verification
  - Callback-based interface: testable without MCP server
"""
from .t1_llm import (
    T1_VERIFY_PROMPT,
    llm_verify,
    pattern_verify,
)
from .t1_llm import (
    VerificationResult as T1Result,
)
from .t1_llm import (
    verify as t1_verify,
)
from .t2_lean import (
    T2_VERIFY_PROMPT,
    T2Result,
    format_code,
    get_canonical,
    parse_diagnostics,
)
from .t2_lean import (
    verify as t2_verify,
)

__all__ = [
    "T1Result",
    "pattern_verify",
    "llm_verify",
    "t1_verify",
    "T1_VERIFY_PROMPT",
    "T2Result",
    "format_code",
    "parse_diagnostics",
    "t2_verify",
    "T2_VERIFY_PROMPT",
    "get_canonical",
]

"""T1 Verifier: fast (~5s), LLM-light structural check for Lean 4 proofs.

T1 is the first gate in the two-tier verification pipeline.

  Tier 1 (T1, this module): Fast structural/syntax/pattern checks.
    - No Lean compiler invoked
    - Regex-based: unclosed blocks, missing definitions, dangling `sorry`
    - LLM-assisted: semantic plausibility of the proof structure
    - Target: ~5s per theorem, ~80% recall of real errors

  Tier 2 (T2): Full Lean compiler verification via `lake build` or lean-lsp-mcp.

Design principle: T1 should catch the 80% obvious errors so T2 only runs on
structurally valid code.  A T1 pass does NOT guarantee correctness — only
that the proof is well-formed enough for T2.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# ── data models ────────────────────────────────────────────────


@dataclass
class VerificationResult:
    """Structured result from T1 verification."""
    verified: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0


# ── check functions ───────────────────────────────────────────


def _check_unclosed_blocks(code: str) -> list[str]:
    """Check balanced braces/brackets/parens outside string literals.

    This is a fast heuristic — does NOT handle every edge case (nested
    strings, comments), but catches the most common T2 blockers.
    """
    stripped = re.sub(r'--.*$', '', code, flags=re.MULTILINE)      # -- comments
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL)  # block comments
    stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '', stripped)            # string literals

    stack: list[tuple[str, int, int]] = []
    pairs = {'{': '}', '(': ')', '[': ']'}
    errors = []

    for lineno, line in enumerate(stripped.split('\n'), 1):
        for col, ch in enumerate(line, 1):
            if ch in pairs:
                stack.append((ch, lineno, col))
            elif ch in pairs.values():
                if not stack:
                    errors.append(f"Unmatched closing '{ch}' at line {lineno}:{col}")
                else:
                    open_ch, open_ln, _ = stack.pop()
                    if pairs[open_ch] != ch:
                        errors.append(
                            f"Mismatched bracket at line {lineno}:{col}: "
                            f"expected '{pairs[open_ch]}' but got '{ch}' "
                            f"(opened at line {open_ln})"
                        )

    for ch, ln, col in stack:
        errors.append(f"Unclosed '{ch}' at line {ln}:{col}")

    return errors


def _check_dangling_sorry(code: str) -> list[str]:
    """Find `sorry` outside of comments or strings."""
    stripped = re.sub(r'--.*$', '', code, flags=re.MULTILINE)
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL)
    stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '', stripped)

    issues = []
    for m in re.finditer(r'\bsorry\b', stripped):
        lineno = code[:m.start()].count('\n') + 1
        issues.append(f"Unresolved `sorry` at line {lineno}")

    return issues


def _check_missing_proof_body(code: str) -> list[str]:
    """Find theorem/lemma/def that lack a proof body.

    A theorem/lemma must eventually have a `:=`, `by`, or `:=` somewhere
    before the next declaration or end of file.  This is a heuristic —
    some valid forms may be missed, but false positives are rare.
    """
    stripped = re.sub(r'--.*$', '', code, flags=re.MULTILINE)
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL)

    # Find all declaration start positions
    decls: list[tuple[int, str, str]] = []
    for m in re.finditer(r'\b(theorem|lemma|def)\s+(\w+)', stripped):
        decls.append((m.start(), m.group(1), m.group(2)))

    if not decls:
        return []

    issues = []
    for i, (pos, decl_type, name) in enumerate(decls):
        # Look at text between this declaration and the next one (or EOF)
        end_pos = decls[i + 1][0] if i + 1 < len(decls) else len(stripped)
        body_text = stripped[pos:end_pos]

        # Skip declarations that are just `set_option` pattern or annotations
        # Check if this declaration has a proof body
        has_body = bool(re.search(r':=\s|:=\n|^by\s|\nby\s', body_text))
        if not has_body:
            issues.append(f"Declaration '{name}' ({decl_type}) appears to have no body")

    return issues


def _check_import_existence(code: str) -> list[str]:
    """Flag import statements for known common typos."""
    known_packages = {
        "Mathlib", "Aesop", "Std", "Qq", "Lean", "Init",
        "Mathlib.Tactic", "Mathlib.Data", "Mathlib.Algebra",
        "Mathlib.Analysis", "Mathlib.Geometry", "Mathlib.NumberTheory",
        "Mathlib.Combinatorics", "Mathlib.Probability",
        "Omega", "Omega.Verify",
    }
    issues = []
    for m in re.finditer(r'^(import|open)\s+(.+)$', code, re.MULTILINE):
        target = m.group(2).strip()
        parts = target.split()
        # Simple heuristic: if the first component looks unknown, warn
        first = parts[0].split('.')[0]
        if first not in {p.split('.')[0] for p in known_packages} and not first[0].isupper():
            issues.append(f"Possible typo in import: '{target}' (lowercase module name)")
    return issues


def _check_admit(code: str) -> list[str]:
    """Find `admit` statements (Lean 4 legacy)."""
    stripped = re.sub(r'--.*$', '', code, flags=re.MULTILINE)
    stripped = re.sub(r'/\*.*?\*/', '', stripped, flags=re.DOTALL)
    issues = []
    for m in re.finditer(r'\badmit\b', stripped):
        lineno = code[:m.start()].count('\n') + 1
        issues.append(f"Unresolved `admit` at line {lineno}")
    return issues


def _check_inductive_patterns(code: str) -> list[str]:
    """Check that `induction` or `cases` have all cases covered in a simple way."""
    stripped = re.sub(r'--.*$', '', code, flags=re.MULTILINE)
    # Simple: check that `cases`/`induction` is followed by `case` or `next`
    for m in re.finditer(r'\b(cases|induction)\s+(\w+)', stripped):
        rest = stripped[m.end():m.end() + 300]
        if not re.search(r'\b(case|next|·|all_goals)', rest):
            # This is a weak check — many valid uses don't need explicit cases
            # Return as warning not error
            pass
    return []


# ── composite runner ──────────────────────────────────────────


DEFAULT_CHECKS: list[Callable[[str], list[str]]] = [
    _check_unclosed_blocks,
    _check_dangling_sorry,
    _check_missing_proof_body,
    _check_import_existence,
    _check_admit,
]


def pattern_verify(code: str, checks: list | None = None) -> VerificationResult:
    """Run fast structural checks on Lean code. No LLM required.

    Parameters
    ----------
    code : str
        Lean 4 code to verify.
    checks : list of callables, optional
        Each callable takes ``code`` and returns a list of issue strings.
        Defaults to DEFAULT_CHECKS.

    Returns
    -------
    VerificationResult
    """
    checks = checks or DEFAULT_CHECKS
    all_issues: list[str] = []
    for check_fn in checks:
        all_issues.extend(check_fn(code))

    n_issues = len(all_issues)
    confidence = max(0.0, 1.0 - n_issues * 0.15)

    return VerificationResult(
        verified=n_issues == 0,
        issues=all_issues,
        confidence=confidence,
    )


# ── LLM-assisted verification ─────────────────────────────────


T1_VERIFY_PROMPT = """You are a Lean 4 proof verification assistant.
Your job is a QUICK (~5 second) structural review of a Lean theorem and proof.
Do NOT run the compiler — use your knowledge of Lean syntax, tactics, and libraries.

Check the following Lean code:

```lean4
{code}
```

Check for these issues:
1. **Missing imports**: Are all used symbols from known Mathlib/Std modules?
2. **Type consistency**: Does the proof target the stated type?
3. **Tactic errors**: Are tactics being applied to compatible goals?
4. **Structural completeness**: Is the proof complete (no `sorry`, `admit`, incomplete cases)?
5. **Variable scope**: Are all named variables declared correctly?

Return your answer as JSON **ONLY** — no markdown, no explanation:
{{"verified": true/false, "issues": [...list of specific issues...], "warnings": [...list of minor concerns...], "confidence": 0.0-1.0}}
"""


def llm_verify(code: str, llm_callback: Callable[[str], str]) -> VerificationResult:
    """Run T1 verification using an LLM callback.

    Parameters
    ----------
    code : str
        Lean 4 code to verify.
    llm_callback : Callable[[str], str]
        Function that takes a prompt and returns LLM response (JSON).

    Returns
    -------
    VerificationResult
    """
    import json
    prompt = T1_VERIFY_PROMPT.format(code=code)

    try:
        raw = llm_callback(prompt)
        # Extract JSON from response (handle markdown-wrapped responses)
        json_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if not json_match:
            return VerificationResult(
                verified=False,
                issues=["LLM did not return parseable JSON"],
                confidence=0.0,
            )
        data = json.loads(json_match.group())
        return VerificationResult(
            verified=data.get("verified", False),
            issues=data.get("issues", []),
            warnings=data.get("warnings", []),
            confidence=data.get("confidence", 0.0),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return VerificationResult(
            verified=False,
            issues=[f"LLM response parse error: {e}"],
            confidence=0.0,
        )


# ── composite T1 ──────────────────────────────────────────────


def verify(code: str, llm_callback: Callable[[str], str] | None = None) -> VerificationResult:
    """Run full T1 verification: pattern checks + optional LLM.

    Pattern checks always run (no dependencies).  If an LLM callback is
    provided, LLM-assisted checks run second and their issues are merged.

    Parameters
    ----------
    code : str
        Lean 4 code to verify.
    llm_callback : Callable[[str], str] | None
        Optional. If provided, runs LLM-assisted semantic checks.

    Returns
    -------
    VerificationResult
    """
    result = pattern_verify(code)
    if llm_callback and result.verified:
        # Only run LLM check if pattern check passes
        llm_result = llm_verify(code, llm_callback)
        result.issues.extend(llm_result.issues)
        result.warnings.extend(llm_result.warnings)
        result.verified = llm_result.verified
        result.confidence = min(result.confidence, llm_result.confidence)

    return result

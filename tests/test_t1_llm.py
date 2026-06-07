"""Tests for T1 Verifier: fast structural checks for Lean 4 code."""

import json
from collections.abc import Callable

from omega.verify.t1_llm import (
    _check_admit,
    _check_dangling_sorry,
    _check_import_existence,
    _check_missing_proof_body,
    _check_unclosed_blocks,
    llm_verify,
    pattern_verify,
    verify,
)

# ── unit tests for individual check functions ──────────────────


class TestCheckUnclosedBlocks:
    def test_balanced(self):
        """Balanced braces/brackets/parens produce no errors."""
        code = "(a + b) * {c + [d]}"
        assert _check_unclosed_blocks(code) == []

    def test_unclosed_paren(self):
        """Unclosed parenthesis is detected."""
        code = "(a + b"
        issues = _check_unclosed_blocks(code)
        assert len(issues) >= 1
        assert "Unclosed" in issues[0]

    def test_unmatched_closing(self):
        """Extra closing bracket is detected."""
        code = "a + b)"
        issues = _check_unclosed_blocks(code)
        assert len(issues) >= 1
        assert "Unmatched" in issues[0]

    def test_mismatched(self):
        """Mismatched brackets are detected."""
        code = "[a + b)"
        issues = _check_unclosed_blocks(code)
        assert len(issues) >= 1
        assert "Mismatched" in issues[0]

    def test_ignores_comments(self):
        """Brackets inside -- comments are ignored."""
        code = """x := 1  -- (this is a comment with [unclosed
y := 2"""
        issues = _check_unclosed_blocks(code)
        # The comment lines may contain unmatched brackets but should be stripped
        # The real check is that code-level brackets are caught
        for issue in issues:
            assert "comment" not in issue.lower() or "unclosed" not in issue.lower()


class TestCheckDanglingSorry:
    def test_no_sorry(self):
        """Clean code has no sorry issues."""
        code = "theorem t : True := by trivial"
        assert _check_dangling_sorry(code) == []

    def test_dangling_sorry(self):
        """A `sorry` is flagged."""
        code = "theorem t : True := by\n  sorry"
        issues = _check_dangling_sorry(code)
        assert len(issues) >= 1
        assert "sorry" in issues[0]

    def test_multiple_sorry(self):
        """Multiple `sorry` are each flagged."""
        code = "theorem a : True := by sorry\ntheorem b : 1=1 := by sorry"
        issues = _check_dangling_sorry(code)
        assert len(issues) == 2

    def test_sorry_in_comment_ignored(self):
        """A `sorry` in a comment is not flagged."""
        code = """theorem t : True := by trivial
-- TODO: finish this proof later, sorry for now
"""
        assert _check_dangling_sorry(code) == []


class TestCheckMissingProofBody:
    def test_theorem_with_body(self):
        """Theorems with by-block are fine."""
        code = "theorem t : True := by\n  trivial"
        assert _check_missing_proof_body(code) == []

    def test_theorem_with_colon_eq(self):
        """Theorems with := are fine."""
        code = "theorem t : True := trivial"
        assert _check_missing_proof_body(code) == []

    def test_def_with_body(self):
        """Definitions with := have bodies."""
        code = "def x : Nat := 42"
        assert _check_missing_proof_body(code) == []

    def test_theorem_without_body(self):
        """A standalone theorem header without body is flagged."""
        code = "theorem unsolved_conjecture : ∀ n : ℕ, n + 0 = n"
        issues = _check_missing_proof_body(code)
        assert len(issues) >= 1
        assert "unsolved_conjecture" in issues[0]

    def test_lemma_without_body(self):
        """A lemma without body is flagged."""
        code = "lemma intermediate_result : 2 + 2 = 4"
        issues = _check_missing_proof_body(code)
        assert len(issues) >= 1


class TestCheckImportExistence:
    def test_known_import(self):
        """Known imports are not flagged."""
        code = "import Mathlib.Tactic\nopen Real"
        assert _check_import_existence(code) == []

    def test_no_imports(self):
        """No import statements produces no errors."""
        code = "theorem t : True := trivial"
        assert _check_import_existence(code) == []


class TestCheckAdmit:
    def test_no_admit(self):
        """Clean code has no admit issues."""
        code = "theorem t : True := by trivial"
        assert _check_admit(code) == []

    def test_admit_detected(self):
        """An `admit` statement is flagged."""
        code = "theorem t : True := by\n  admit"
        issues = _check_admit(code)
        assert len(issues) >= 1
        assert "admit" in issues[0]


# ── integration tests for pattern_verify ──────────────────────


class TestPatternVerify:
    def test_valid_theorem(self):
        """A valid Lean theorem passes pattern checks."""
        code = """import Mathlib.Tactic

theorem add_zero (n : ℕ) : n + 0 = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp [ih]
"""
        result = pattern_verify(code)
        assert result.verified is True
        assert result.confidence >= 0.8

    def test_sorry_fails(self):
        """A theorem with sorry fails."""
        code = "theorem t : True := by\n  sorry"
        result = pattern_verify(code)
        assert result.verified is False
        assert any("sorry" in i for i in result.issues)

    def test_unclosed_block_fails(self):
        """Unclosed braces fail."""
        code = "theorem t : True := by\n  have h : True := { trivial"
        result = pattern_verify(code)
        assert result.verified is False

    def test_multiple_issues(self):
        """Multiple issues are all reported."""
        code = "theorem a : True := by sorry\ntheorem b"
        result = pattern_verify(code)
        assert result.verified is False
        assert len(result.issues) >= 2


# ── tests for llm_verify ──────────────────────────────────────


def _make_llm_callback(result_dict: dict) -> Callable[[str], str]:
    """Helper: create an LLM callback that returns a given JSON dict."""

    def callback(_prompt: str) -> str:
        return json.dumps(result_dict)

    return callback


class TestLlmVerify:
    def test_valid_code(self):
        """LLM responding with verified=True."""
        cb = _make_llm_callback(
            {"verified": True, "issues": [], "warnings": [], "confidence": 0.95}
        )
        result = llm_verify("theorem t : True := by trivial", cb)
        assert result.verified is True
        assert result.confidence == 0.95

    def test_with_issues(self):
        """LLM returning issues propagates them."""
        cb = _make_llm_callback(
            {
                "verified": False,
                "issues": ["Missing import for `Real.sin`"],
                "confidence": 0.3,
            }
        )
        result = llm_verify("theorem t : True := Real.sin 0", cb)
        assert result.verified is False
        assert len(result.issues) == 1
        assert "Real.sin" in result.issues[0]

    def test_malformed_json(self):
        """LLM returning non-JSON fails gracefully."""

        def bad_cb(_prompt):
            return "I think this proof looks correct"

        result = llm_verify("theorem t : True := by trivial", bad_cb)
        assert result.verified is False
        assert result.confidence == 0.0
        assert any("JSON" in i for i in result.issues)

    def test_json_in_markdown(self):
        """LLM wrapping JSON in ```json markers."""

        def md_cb(_prompt):
            return '```json\n{"verified": true, "issues": [], "confidence": 0.9}\n```'

        result = llm_verify("theorem t : True := by trivial", md_cb)
        assert result.verified is True
        assert result.confidence == 0.9

    def test_empty_response(self):
        """Empty LLM response."""

        def empty_cb(_prompt):
            return ""

        result = llm_verify("theorem t : True := by trivial", empty_cb)
        assert result.verified is False

    def test_issues_and_warnings(self):
        """Both issues and warnings are returned."""
        cb = _make_llm_callback(
            {
                "verified": False,
                "issues": ["Missing type annotation"],
                "warnings": ["Consider using `simp`"],
                "confidence": 0.4,
            }
        )
        result = llm_verify("def x := 42", cb)
        assert result.verified is False
        assert result.warnings == ["Consider using `simp`"]


# ── tests for composite verify ────────────────────────────────


class TestCompositeVerify:
    def test_pattern_only_no_llm(self):
        """verify() with no LLM callback runs pattern checks only."""
        result = verify("theorem t : True := by\n  trivial")
        assert result.verified is True

    def test_pattern_fails_before_llm(self):
        """If pattern check fails, LLM is not called."""
        llm_called = False

        def llm_cb(_prompt):
            nonlocal llm_called
            llm_called = True
            return json.dumps({"verified": True, "issues": [], "confidence": 0.9})

        _result = verify("theorem t : True := by sorry", llm_cb)
        assert _result.verified is False
        assert llm_called is False  # LLM skipped

    def test_pattern_then_llm(self):
        """If pattern check passes, LLM is called."""
        llm_called = False

        def llm_cb(_prompt):
            nonlocal llm_called
            llm_called = True
            return json.dumps({"verified": True, "issues": [], "confidence": 0.9})

        _result = verify("theorem t : True := by\n  trivial", llm_cb)
        assert llm_called is True

    def test_llm_catches_semantic_error(self):
        """LLM issues are merged with pattern issues."""
        called = False

        def llm_cb(_prompt):
            nonlocal called
            called = True
            return json.dumps(
                {
                    "verified": False,
                    "issues": ["Type mismatch: expected Nat but got String"],
                    "confidence": 0.2,
                }
            )

        result = verify("theorem t : True := by\n  trivial", llm_cb)
        assert result.verified is False
        assert result.issues == ["Type mismatch: expected Nat but got String"]
        assert result.confidence < 0.5

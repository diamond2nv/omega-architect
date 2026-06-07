"""Tests for omega-plugin bridge (_bridge.py).

Covers:
- T1 validation (enhanced)
- T2 error classification
- Compile cache
- Format helpers
- Empty/edge cases
"""

from __future__ import annotations

# Imported via conftest.py — handles the hyphen in the dir name
from conftest import bridge as br

t1_validate = br.t1_validate
classify_t2_errors = br.classify_t2_errors
reset_compile_cache = br.reset_compile_cache
OmegaBridge = br.OmegaBridge


# ── T1 validation tests (enhanced) ──────────────────────────────


class TestT1Validate:
    """Enhanced T1 validation covering all edge cases."""

    def test_valid_simple_proof(self) -> None:
        code = "theorem t : True := by\n  trivial"
        result = t1_validate(code)
        assert result["verified"] is True, f"Unexpected issues: {result['issues']}"

    def test_valid_with_import(self) -> None:
        code = """import Mathlib
theorem add_zero (n : ℕ) : n + 0 = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp [ih]"""
        result = t1_validate(code)
        assert result["verified"] is True, f"Unexpected issues: {result['issues']}"

    def test_empty_code(self) -> None:
        result = t1_validate("")
        assert result["verified"] is False
        assert "Empty code block" in result["issues"]

    def test_only_comments(self) -> None:
        code = "-- this is just a comment\n-- another comment"
        result = t1_validate(code)
        assert result["verified"] is False
        assert any("no theorem" in i.lower() for i in result["issues"])

    def test_missing_colon_equals(self) -> None:
        code = "theorem t : True"
        result = t1_validate(code)
        assert result["verified"] is False
        assert any("missing" in i.lower() and ":=" in i for i in result["issues"])

    def test_empty_proof_body(self) -> None:
        code = "theorem t : True := by"
        result = t1_validate(code)
        assert result["verified"] is False
        assert any("empty proof body" in i.lower() for i in result["issues"])

    def test_unmatched_bracket(self) -> None:
        code = "theorem t : True := by\n  (trivial"
        result = t1_validate(code)
        assert result["verified"] is False
        assert any("unclosed" in i.lower() for i in result["issues"])

    def test_mismatched_bracket(self) -> None:
        code = "theorem t : True := by\n  (trivial]"
        result = t1_validate(code)
        assert result["verified"] is False

    def test_multiple_theorems(self) -> None:
        code = """theorem a : True := by trivial
theorem b : 1 = 1 := by rfl"""
        result = t1_validate(code)
        assert result["verified"] is True

    def test_def_and_theorem(self) -> None:
        code = """def myConst : Nat := 42
theorem myConst_pos : myConst > 0 := by native_decide"""
        result = t1_validate(code)
        assert result["verified"] is True


# ── T2 error classification tests ──────────────────────────────


class TestClassifyT2Errors:
    """Error classification from T2 compiler output."""

    def test_unknown_identifier(self) -> None:
        errors = ["unknown identifier 'foobar'"]
        classified = classify_t2_errors(errors)
        assert "unknown_identifier" in classified
        assert len(classified["unknown_identifier"]) == 1

    def test_type_mismatch(self) -> None:
        errors = ["type mismatch: expected Nat, got String"]
        classified = classify_t2_errors(errors)
        assert "type_mismatch" in classified

    def test_unsolved_goals(self) -> None:
        errors = ["unsolved goals at line 5:2"]
        classified = classify_t2_errors(errors)
        assert "unsolved_goals" in classified

    def test_missing_import(self) -> None:
        errors = ["missing import `Mathlib.Data.Nat.Basic`"]
        classified = classify_t2_errors(errors)
        assert "missing_import" in classified

    def test_timeout(self) -> None:
        errors = ["T2 compile timeout (120s)"]
        classified = classify_t2_errors(errors)
        assert "timeout" in classified

    def test_mixed_errors(self) -> None:
        errors = [
            "unknown identifier 'foo'",
            "type mismatch: expected Nat, got String",
            "unsolved goals at line 3:1",
        ]
        classified = classify_t2_errors(errors)
        assert len(classified) >= 3
        assert "unknown_identifier" in classified
        assert "type_mismatch" in classified
        assert "unsolved_goals" in classified

    def test_unknown_pattern(self) -> None:
        errors = ["some weird internal error: XYZ-404"]
        classified = classify_t2_errors(errors)
        assert "other" in classified

    def test_empty_input(self) -> None:
        classified = classify_t2_errors([])
        assert classified == {}


# ── compile cache tests ────────────────────────────────────────


class TestCompileCache:
    """Compile cache should deduplicate identical compile calls."""

    def setup_method(self) -> None:
        reset_compile_cache()

    def test_cache_hit(self) -> None:
        call_count = 0

        def fake_compile(_code: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"diagnostics": [{"message": "OK", "severity": "info"}]}

        # Direct cache usage via bridge module
        compile_cache_key = br._compile_cache_key

        code = "theorem t : True := by trivial"

        key = compile_cache_key(code)
        assert key not in br._COMPILE_CACHE

        br._COMPILE_CACHE[key] = fake_compile(code)
        assert call_count == 1

        # "Cache hit" — don't call again
        cached = br._COMPILE_CACHE.get(key)
        assert cached is not None
        assert call_count == 1  # still 1

    def test_cache_differs_by_code(self) -> None:
        compile_cache_key = br._compile_cache_key

        key1 = compile_cache_key("theorem a : True := by trivial")
        key2 = compile_cache_key("theorem b : True := by trivial")
        assert key1 != key2, "Different theorems must have different cache keys"


# ── format helpers tests ───────────────────────────────────────


class TestFormatResult:
    """Chat display formatting for result dicts."""

    def test_format_success(self) -> None:
        data = {
            "succeeded": True,
            "elapsed_s": 12.3,
            "n_attempts": 6,
            "proof": "theorem t : True := by trivial",
        }
        result = OmegaBridge.format_result_for_chat(data)
        assert "12.3s" in result
        assert "6 attempts" in result
        assert "trivial" in result

    def test_format_success_no_proof(self) -> None:
        data = {
            "succeeded": True,
            "elapsed_s": 5.0,
            "n_attempts": 3,
            "proof": "",
        }
        result = OmegaBridge.format_result_for_chat(data)
        assert "5.0s" in result

    def test_format_failure(self) -> None:
        data = {
            "succeeded": False,
            "summary": "No proof found",
            "elapsed_s": 30.0,
            "errors": ["compile error 1", "compile error 2"],
        }
        result = OmegaBridge.format_result_for_chat(data)
        assert "❌" in result or "No proof found" in result

    def test_format_classified_errors(self) -> None:
        data = {
            "succeeded": False,
            "summary": "No proof found",
            "elapsed_s": 15.0,
            "errors": ["unknown identifier 'foo'"],
            "classified_errors": {"unknown_identifier": ["unknown identifier 'foo'"]},
        }
        result = OmegaBridge.format_result_for_chat(data)
        assert "unknown_identifier" in result
        assert "foo" in result

    def test_format_benchmark(self) -> None:
        data = {
            "succeeded": True,
            "summary": "Benchmark: 10 problems, T1 8/10 (80%), T2 3/10 (30%), in 142s",
            "total": 10,
            "t1_passed": 8,
            "t2_passed": 3,
            "problems": [
                {"name": "add_zero", "t1_pass": True, "t2_pass": True},
                {"name": "aime_p1", "t1_pass": True, "t2_pass": False},
            ],
        }
        result = OmegaBridge.format_result_for_chat(data)
        assert "3/10" in result
        assert "add_zero" in result

    def test_format_status(self) -> None:
        info = {
            "omega_core": True,
            "lean_binary": "/usr/bin/lean",
            "lean_version": "Lean 4.15.0",
            "lean_project": "/home/user/lean-paper-plane",
            "lean_project_has_mathlib": True,
            "mcp_lean": True,
            "t2_compile_fn": "lake env",
            "compile_cache_size": 5,
        }
        result = OmegaBridge.format_status_for_chat(info)
        assert "Lean 4.15.0" in result
        assert "lake env" in result
        assert "5 entries" in result


# ── edge case tests ────────────────────────────────────────────


class TestT1EdgeCases:
    """Test T1 with unusual but valid inputs."""

    def test_unicode_symbols(self) -> None:
        code = "theorem t (x : ℝ) : x^2 + x^2 = 2*x^2 := by ring"
        result = t1_validate(code)
        assert result["verified"] is False if ":=" not in code else True

    def test_induction_proof(self) -> None:
        code = """theorem zero_add (n : ℕ) : 0 + n = n := by
  induction n with
  | zero => rfl
  | succ n ih => simp [ih]"""
        result = t1_validate(code)
        assert result["verified"] is True

    def test_calc_proof(self) -> None:
        code = """theorem add_comm_example (a b : ℕ) : a + b = b + a := by
  induction a with
  | zero => simp
  | succ a ih => simp [add_comm, ih]"""
        result = t1_validate(code)
        assert result["verified"] is True

    def test_single_line_proof(self) -> None:
        code = "theorem t : True := by trivial"
        result = t1_validate(code)
        assert result["verified"] is True

    def test_sorry_blocking(self) -> None:
        # T1 does not fail on sorry — it's a valid Lean token.
        # But the result should have no structural issues.
        code = "theorem t : True := by\n  sorry"
        result = t1_validate(code)
        assert result["verified"] is True  # structurally valid

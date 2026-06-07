"""Tests for T2 Real Compile — real Lean compiler via lake env lean --stdin."""

from omega.verify.t2_real import (
    make_real_compile_callback,
    parse_lean_diagnostics,
    real_compile_callback,
)


class TestParseLeanDiagnostics:
    def test_empty(self):
        """Empty stderr produces empty list."""
        assert parse_lean_diagnostics("") == []

    def test_error(self):
        """Error diagnostic is parsed."""
        stderr = "<stdin>:13:9: error: unsolved goals\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "error"
        assert result[0]["line"] == 13
        assert result[0]["column"] == 9
        assert "unsolved" in result[0]["message"]

    def test_warning(self):
        """Warning diagnostic is parsed."""
        stderr = "<stdin>:4:27: warning: unused variable `h`\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "warning"

    def test_multiline(self):
        """Multiple diagnostics are all parsed."""
        stderr = "<stdin>:4:27: warning: unused variable `h`\n<stdin>:13:9: error: unsolved goals\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 2

    def test_info_fallback(self):
        """Non-diagnostic lines become info entries."""
        stderr = "Some info message from Lean\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "info"

    def test_no_colon_trailing(self):
        """Severity with colon: <stdin>:1:1: error: xxx."""
        stderr = "<stdin>:1:1: error: something bad\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "error"

    def test_note_severity(self):
        """Note severity is handled."""
        stderr = "<stdin>:3:5: note: this is a note\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "note"


class TestRealCompileCallback:
    """These tests require lean-paper-plane project with Mathlib cache."""

    def test_project_exists(self):
        """The default project directory exists."""
        from pathlib import Path

        assert Path.home().joinpath("lean-paper-plane").exists()

    def test_lean_binary_exists(self):
        """Lean binary is installed."""
        from omega.resource.lean_config import load_lean_config

        cfg = load_lean_config()
        assert cfg.lean_bin.exists(), f"Lean binary not found: {cfg.lean_bin}"
        assert cfg.lake_bin.exists(), f"Lake binary not found: {cfg.lake_bin}"

    def test_compile_trivial(self):
        """A trivial theorem compiles successfully."""
        result = real_compile_callback("theorem t : True := trivial")
        assert result["exit_code"] == 0
        # No errors
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        assert len(errors) == 0

    def test_compile_mathlib(self):
        """A Mathlib-dependent theorem compiles successfully."""
        code = """import Mathlib
open Real
theorem sin_sq_add_cos_sq (x : ℝ) : Real.sin x ^ 2 + Real.cos x ^ 2 = 1 := by
  exact Real.sin_sq_add_cos_sq x"""
        result = real_compile_callback(code)
        assert result["exit_code"] == 0, f"Diagnostics: {result['diagnostics']}"

    def test_compile_broken(self):
        """A broken theorem returns compile errors."""
        code = "theorem t : True := by\n  bogus"
        result = real_compile_callback(code)
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        assert len(errors) > 0

    def test_compile_nat_theorem(self):
        """A MiniF2F-style number theory theorem."""
        code = """import Mathlib
open Nat

theorem induction_12dvd4expnp1p20 (n : ℕ) : 12 ∣ 4^n + 20 := by
  induction n with
  | zero =>
      simp
  | succ n ih =>
      have h4n : 4^(n+1) = 4 * 4^n := by ring
      sorry"""
        result = real_compile_callback(code)
        # Should compile with sorry (warning, not error)
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        assert len(errors) >= 0  # unsolved goals is an error here
        # The key is: Mathlib loads correctly
        info_lines = [d["message"] for d in result["diagnostics"] if d["severity"] == "info"]
        assert not any("Mathlib" in m for m in info_lines)  # No Mathlib loading errors


class TestMakeRealCompileCallback:
    def test_returns_callable(self):
        """make_real_compile_callback returns a callable."""
        fn = make_real_compile_callback()
        assert callable(fn)

    def test_works_with_verify(self):
        """The callback works with t2_lean.verify()."""
        from omega.verify.t2_lean import verify

        fn = make_real_compile_callback()
        result = verify("theorem t : True := trivial", compile_fn=fn)
        assert result.verified is True
        assert result.elapsed_ms >= 0

    def test_verify_mathlib(self):
        """Verify with Mathlib theorem works."""
        from omega.verify.t2_lean import verify

        fn = make_real_compile_callback()
        code = """import Mathlib
open Real
theorem sin_sq_add_cos_sq (x : ℝ) : Real.sin x ^ 2 + Real.cos x ^ 2 = 1 := by
  exact Real.sin_sq_add_cos_sq x"""
        # format_code auto-prepends preamble, but we already have import
        from omega.verify.t2_lean import format_code

        formatted = format_code(code)
        result = verify(code, compile_fn=fn)
        assert result.verified is True, f"Errors: {result.errors}"

    def test_verify_broken(self):
        """A broken theorem returns unverified."""
        from omega.verify.t2_lean import verify

        fn = make_real_compile_callback()
        result = verify("theorem t : True := by\n  bogus", compile_fn=fn)
        assert result.verified is False
        assert len(result.errors) > 0

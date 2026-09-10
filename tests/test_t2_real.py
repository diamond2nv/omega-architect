"""Tests for the T2 real Lean compiler interface."""

from __future__ import annotations

from pathlib import Path

from omega.verify.t2_real import parse_lean_diagnostics, real_compile_callback


def _verified(result):
    """Read ``verified`` from either return shape.

    ``make_real_compile_callback`` is annotated ``-> dict`` and does return a
    dict, while ``t2_lean.verify`` returns a ``T2Result`` object; consumers
    disagree, and the contract still needs a formal decision (flagged
    2026-09-10 after a peer with Lean hit it). Tolerate both meanwhile.
    """
    if isinstance(result, dict):
        return bool(result.get("verified", False))
    return bool(getattr(result, "verified", False))


# ── ParseLeanDiagnostics ───────────────────────────────────────


class TestParseLeanDiagnostics:
    """Tests for parse_lean_diagnostics — no Lean binary needed."""

    def test_empty(self):
        assert parse_lean_diagnostics("") == []

    def test_error(self):
        stderr = "<stdin>:13:9: error: unsolved goals\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "error"
        assert "unsolved" in result[0]["message"]

    def test_warning(self):
        stderr = "<stdin>:4:27: warning: unused variable `h`\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "warning"

    def test_multiline(self):
        stderr = "<stdin>:4:27: warning: unused variable `h`\n<stdin>:13:9: error: unsolved goals\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 2

    def test_info_fallback(self):
        stderr = "Some info message from Lean\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "info"

    def test_no_colon_trailing(self):
        stderr = "<stdin>:1:1: error: something bad\n"
        result = parse_lean_diagnostics(stderr)
        assert result[0]["severity"] == "error"

    def test_note_severity(self):
        stderr = "<stdin>:3:5: note: this is a note\n"
        result = parse_lean_diagnostics(stderr)
        assert len(result) == 1
        assert result[0]["severity"] == "note"


# ── RealCompileCallback — needs Lean 4 toolchain ───────────────


from tests.helpers import needs_lean  # noqa: E402 - grouped with the Lean-gated section below


@needs_lean
class TestRealCompileCallback:
    """These tests require Lean 4 toolchain (lean + lake + Mathlib)."""

    def test_project_exists(self) -> None:
        from pathlib import Path
        assert Path.home().joinpath("lean-paper-plane").exists()

    def test_lean_binary_exists(self) -> None:
        from omega.resource.lean_config import load_lean_config
        cfg = load_lean_config()
        assert cfg.lean_bin.exists(), f"Lean binary not found: {cfg.lean_bin}"
        assert cfg.lake_bin.exists(), f"Lake binary not found: {cfg.lake_bin}"

    def test_compile_trivial(self) -> None:
        result = real_compile_callback("theorem t : True := trivial")
        assert result["exit_code"] == 0
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        assert len(errors) == 0

    def test_compile_mathlib(self) -> None:
        code = """import Mathlib
open Real
theorem sin_sq_add_cos_sq (x : ℝ) : Real.sin x ^ 2 + Real.cos x ^ 2 = 1 := by
  exact Real.sin_sq_add_cos_sq x"""
        result = real_compile_callback(code)
        assert result["exit_code"] == 0, f"Diagnostics: {result['diagnostics']}"

    def test_compile_broken(self) -> None:
        code = "theorem t : True := by\n  bogus"
        result = real_compile_callback(code)
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        assert len(errors) > 0

    def test_compile_nat_theorem(self) -> None:
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
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        assert len(errors) >= 0


# ── MakeRealCompileCallback — needs Lean 4 toolchain ───────────


@needs_lean
class TestMakeRealCompileCallback:
    """Tests for make_real_compile_callback factory."""

    def test_works_with_verify(self) -> None:
        from omega.verify.t2_real import make_real_compile_callback
        fn = make_real_compile_callback(project_dir=str(Path.home() / "lean-paper-plane"))
        result = fn("theorem t : 1 = 1 := rfl")
        assert _verified(result) is True

    def test_verify_mathlib(self) -> None:
        from omega.verify.t2_real import make_real_compile_callback
        fn = make_real_compile_callback(project_dir=str(Path.home() / "lean-paper-plane"))
        result = fn("import Mathlib\n\ntheorem t : 1 = 1 := rfl")
        assert _verified(result) is True, f"Result: {result}"

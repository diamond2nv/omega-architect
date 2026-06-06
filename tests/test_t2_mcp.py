"""Tests for T2 MCP compile callback and online runner."""
from omega.verify.t2_mcp import (
    PURE_LEAN_THEOREMS,
    T2OnlineRunner,
    format_mcp_diagnostics,
    make_mcp_callback,
)


class TestMakeMcpCallback:
    def test_returns_callable(self):
        """make_mcp_callback returns a callable."""
        fn = make_mcp_callback()
        assert callable(fn)

    def test_returns_pending_marker(self):
        """Default callback returns _mcp_pending marker."""
        fn = make_mcp_callback()
        result = fn("theorem t : True := trivial")
        assert result["_mcp_pending"] is True

    def test_code_preview_included(self):
        """Code preview is included in result."""
        fn = make_mcp_callback()
        result = fn("theorem t : True := by trivial")
        assert "theorem t" in result["_code_preview"]

    def test_backend_selection(self):
        """Backend parameter is reflected."""
        fn = make_mcp_callback(backend="lean_build")
        assert fn("")["_backend"] == "lean_build"


class TestFormatMcpDiagnostics:
    def test_none(self):
        """None returns None."""
        assert format_mcp_diagnostics(None) is None

    def test_with_diagnostics_key(self):
        """Dict with diagnostics key passes through."""
        raw = {"diagnostics": [{"severity": "error", "message": "err"}]}
        result = format_mcp_diagnostics(raw)
        assert result is raw

    def test_with_success_key(self):
        """Dict with 'result' key unwraps."""
        raw = {"result": {"diagnostics": []}}
        result = format_mcp_diagnostics(raw)
        assert result is raw["result"]

    def test_plain_dict_no_diagnostics(self):
        """Plain dict without diagnostics passes through."""
        raw = {"message": "hello"}
        result = format_mcp_diagnostics(raw)
        assert result is raw


class TestPureLeanTheorems:
    def test_all_theorems_have_code(self):
        """All pure-Lean theorems have non-empty code."""
        for name, code in PURE_LEAN_THEOREMS.items():
            assert len(code.strip()) > 10, f"{name} has empty code"

    def test_all_contain_theorem_keyword(self):
        """All are real Lean theorems."""
        for name, code in PURE_LEAN_THEOREMS.items():
            assert "theorem" in code, f"{name} missing 'theorem'"

    def test_none_import_mathlib(self):
        """None depend on Mathlib."""
        for name, code in PURE_LEAN_THEOREMS.items():
            assert "import Mathlib" not in code, f"{name} imports Mathlib"


class TestT2OnlineRunner:
    def test_offline_mode(self):
        """Without compile_fn, returns descriptive error."""
        runner = T2OnlineRunner()
        result = runner.run_one("theorem t : True := trivial", "test")
        assert result["verified"] is False
        assert "compile_fn" in result["errors"][0]

    def test_run_one_with_mock(self):
        """Mock compile_fn returns expected result."""
        def mock_compile(_code):
            return {"diagnostics": []}

        runner = T2OnlineRunner(compile_fn=mock_compile)
        result = runner.run_one("theorem t : True := trivial", "test")
        assert result["verified"] is True
        assert result["elapsed_ms"] >= 0

    def test_run_one_with_errors(self):
        """Mock compile_fn with errors."""
        def mock_compile(_code):
            return {"diagnostics": [{"message": "type error", "severity": "error"}]}

        runner = T2OnlineRunner(compile_fn=mock_compile)
        result = runner.run_one("theorem t : True := bad", "test")
        assert result["verified"] is False
        assert len(result["errors"]) >= 1

    def test_run_batch(self):
        """Batch run processes all theorems."""
        def mock_compile(_code):
            return {"diagnostics": []}

        runner = T2OnlineRunner(compile_fn=mock_compile)
        theorems = {"t1": "theorem t1 : True := trivial", "t2": "theorem t2 : 1=1 := rfl"}
        results = runner.run_batch(theorems)
        assert len(results) == 2
        assert all(r["verified"] for r in results)

    def test_report_generated(self):
        """Report is a non-empty string."""
        runner = T2OnlineRunner()
        report = runner.report()
        assert isinstance(report, str)
        assert len(report) > 0

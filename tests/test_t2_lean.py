"""Tests for T2 Verifier: Lean compiler verification."""
import json

from omega.verify.t2_lean import (
    format_code,
    get_canonical,
    parse_diagnostics,
    verify,
)

# ── format_code ────────────────────────────────────────────────


class TestFormatCode:
    def test_empty(self):
        """Empty code gets preamble + placeholder."""
        result = format_code("")
        assert "import Mathlib" in result

    def test_no_import_prepends_preamble(self):
        """Code without imports gets Mathlib preamble."""
        code = "theorem t : True := trivial"
        result = format_code(code)
        assert result.startswith("import Mathlib")
        assert "theorem t : True := trivial" in result

    def test_with_import_preserved(self):
        """Code with imports is NOT prepended preamble."""
        code = "import Mathlib.Tactic\n\ntheorem t : True := by trivial"
        result = format_code(code)
        assert result == code

    def test_with_open_preserved(self):
        """Code with `open` is treated as self-contained."""
        code = "open Real\n\ntheorem t : sin 0 = 0 := by norm_num"
        result = format_code(code)
        assert result == code

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is handled."""
        result = format_code("  \n  theorem t : True := trivial  \n  ")
        assert "theorem t : True := trivial" in result


# ── parse_diagnostics ─────────────────────────────────────────


class TestParseDiagnostics:
    def test_empty_list(self):
        """No diagnostics = pass."""
        result = parse_diagnostics([])
        assert result.verified is True

    def test_none_input(self):
        """None input returns fail with message."""
        result = parse_diagnostics(None)
        assert result.verified is False
        assert "No response" in result.errors[0]

    def test_single_error(self):
        """Single error diagnostic."""
        raw = [{"message": "type mismatch", "severity": "error"}]
        result = parse_diagnostics(raw)
        assert result.verified is False
        assert len(result.errors) == 1
        assert "type mismatch" in result.errors[0]

    def test_single_warning(self):
        """Warning does NOT cause fail."""
        raw = [{"message": "unused variable `h`", "severity": "warning"}]
        result = parse_diagnostics(raw)
        assert result.verified is True
        assert len(result.warnings) == 1

    def test_mixed_errors_and_warnings(self):
        """Both errors and warnings are captured."""
        raw = [
            {"message": "type mismatch", "severity": "error"},
            {"message": "unused variable", "severity": "warning"},
        ]
        result = parse_diagnostics(raw)
        assert result.verified is False
        assert len(result.errors) == 1
        assert len(result.warnings) == 1

    def test_with_position(self):
        """Position info is included in error message."""
        raw = [{
            "message": "unknown identifier",
            "severity": "error",
            "pos": {"line": 5, "character": 12},
        }]
        result = parse_diagnostics(raw)
        assert "[5:12]" in result.errors[0]

    def test_dict_with_diagnostics_key(self):
        """MCP shape: dict with 'diagnostics' list."""
        raw = {"diagnostics": [
            {"message": "expected ';'", "severity": "error"},
        ]}
        result = parse_diagnostics(raw)
        assert result.verified is False
        assert "expected" in result.errors[0]

    def test_single_error_dict_no_diagnostics_key(self):
        """Single dict with 'severity' treated as single diagnostic."""
        raw = {"message": "syntax error", "severity": "error"}
        result = parse_diagnostics(raw)
        assert result.verified is False
        assert "syntax error" in result.errors[0]

    def test_string_json(self):
        """JSON string is parsed."""
        raw = json.dumps([{"message": "type error", "severity": "error"}])
        result = parse_diagnostics(raw)
        assert result.verified is False

    def test_plain_string(self):
        """Plain string (non-JSON) is treated as error message."""
        result = parse_diagnostics("Lean compiler crashed: out of memory")
        assert result.verified is False
        assert "out of memory" in result.errors[0]

    def test_empty_dict(self):
        """Empty dict results in error."""
        result = parse_diagnostics({})
        assert result.verified is False

    def test_position_variants(self):
        """Both 'pos' and 'position' keys are accepted."""
        r1 = parse_diagnostics([{
            "message": "err", "severity": "error", "pos": {"line": 1, "character": 3},
        }])
        r2 = parse_diagnostics([{
            "message": "err", "severity": "error", "position": {"line": 2, "column": 5},
        }])
        assert "[1:3]" in r1.errors[0]
        assert "[2:5]" in r2.errors[0]


# ── verify ────────────────────────────────────────────────────


class TestVerify:
    def test_no_callback(self):
        """Without compile_fn, returns a descriptive failure."""
        result = verify("theorem t : True := trivial")
        assert result.verified is False
        assert "No compile_fn" in result.errors[0]

    def test_mock_pass(self):
        """Mock callback returning empty diagnostics = pass."""
        result = verify(
            "theorem t : True := trivial",
            compile_fn=lambda _code: [],
        )
        assert result.verified is True
        assert result.elapsed_ms >= 0

    def test_mock_fail(self):
        """Mock callback returning errors = fail."""
        result = verify(
            "theorem t : True := by sorry",
            compile_fn=lambda _code: [{
                "message": "unsolved goals", "severity": "error",
            }],
        )
        assert result.verified is False
        assert result.elapsed_ms >= 0

    def test_mock_exception(self):
        """Callback raising exception is caught."""
        def bad_compile(_code):
            raise RuntimeError("MCP connection lost")
        result = verify("theorem t : True := trivial", compile_fn=bad_compile)
        assert result.verified is False
        assert "MCP connection lost" in result.errors[0]

    def test_callback_receives_formatted_code(self):
        """Callback receives formatted (not raw) code."""
        received = []

        def capture(code):
            received.append(code)
            return []

        verify("theorem t : True := trivial", compile_fn=capture)
        assert len(received) == 1
        assert "import Mathlib" in received[0]
        assert "theorem t : True := trivial" in received[0]


# ── canonical theorems ────────────────────────────────────────


class TestCanonical:
    def test_get_canonical_exists(self):
        """Known theorems are retrievable."""
        code = get_canonical("add_zero")
        assert code is not None
        assert "add_zero" in code

    def test_get_canonical_missing(self):
        """Unknown name returns None."""
        assert get_canonical("nonexistent") is None

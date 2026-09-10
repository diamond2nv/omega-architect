"""Contract tests for the T2 compile-callback result shape.

Regression guard for the 2026-09-10 ruling: ``compile_fn`` returns a **dict**,
and that dict is self-describing (``verified`` / ``errors`` are runtime fields,
not something each caller recomputes).  The bug this locks down: four call
sites in ``omega/search/passk.py`` read ``t2_result.verified`` as an *attribute*
on a dict, raising ``AttributeError`` against the real callback — invisible to
any test that stubs ``compile_fn`` with a custom object.
"""

from __future__ import annotations

from omega.verify.t2_real import _compile_result, read_errors, read_verified


class TestCompileResultShape:
    """``_compile_result`` builds the canonical dict."""

    def test_success_is_verified(self) -> None:
        r = _compile_result([], exit_code=0)
        assert r["verified"] is True
        assert r["errors"] == []
        assert r["diagnostics"] == []

    def test_nonzero_exit_is_not_verified(self) -> None:
        r = _compile_result([], exit_code=1)
        assert r["verified"] is False

    def test_error_severity_is_not_verified_even_on_exit_zero(self) -> None:
        """Lean can exit 0 while still reporting an error-severity diagnostic."""
        diag = [{"message": "unsolved goals", "severity": "error", "line": 1, "column": 1}]
        r = _compile_result(diag, exit_code=0)
        assert r["verified"] is False
        assert r["errors"] == diag

    def test_warning_severity_does_not_block_verified(self) -> None:
        diag = [{"message": "unused variable", "severity": "warning", "line": 1, "column": 1}]
        r = _compile_result(diag, exit_code=0)
        assert r["verified"] is True
        assert r["errors"] == []

    def test_dict_has_verified_key_accessor_works(self) -> None:
        assert read_verified(_compile_result([], exit_code=0)) is True


class TestShapeTolerantReaders:
    """``compile_fn`` is an injectable seam: support dict *and* attribute shapes."""

    class _Obj:
        def __init__(self, verified: bool, errors: list | None = None) -> None:
            self.verified = verified
            self.errors = errors or []

    def test_read_verified_dict(self) -> None:
        assert read_verified({"verified": True}) is True
        assert read_verified({"verified": False}) is False
        assert read_verified({}) is False

    def test_read_verified_object(self) -> None:
        assert read_verified(self._Obj(True)) is True
        assert read_verified(self._Obj(False)) is False

    def test_read_verified_object_without_attribute(self) -> None:
        assert read_verified(object()) is False

    def test_read_errors_dict(self) -> None:
        assert read_errors({"errors": ["a"]}) == ["a"]
        assert read_errors({"errors": None}) == []
        assert read_errors({}) == []

    def test_read_errors_object(self) -> None:
        assert read_errors(self._Obj(True, ["b"])) == ["b"]
        assert read_errors(object()) == []

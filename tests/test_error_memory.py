"""Test ProofErrorMemory: normalization, Jaccard, lookup/record, persistence."""
import os, sys, tempfile, shutil, json, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omega.loop.error_memory import (
    ProofErrorMemory, _normalize_error, _jaccard_similarity, _error_signature,
    _auto_classify, ErrorRecord,
)


# ── Fixture ──────────────────────────────────────────────

@pytest.fixture
def mem():
    d = tempfile.mkdtemp()
    m = ProofErrorMemory(cache_dir=d)
    m.clear()
    yield m
    shutil.rmtree(d, ignore_errors=True)


# ══════════════════════════════════════════════════════════
# Normalization
# ══════════════════════════════════════════════════════════

class TestNormalization:
    def test_strip_line_numbers(self):
        a = _normalize_error("type mismatch at line 42: expected Nat")
        b = _normalize_error("type mismatch at line 99: expected Nat")
        assert a == b

    def test_strip_bare_numbers(self):
        a = _normalize_error("line 42 col 5: expected Nat, got Int")
        b = _normalize_error("line 99 col 3: expected Nat, got Int")
        assert a == b

    def test_unicode_mapping(self):
        assert "nat" in _normalize_error("ℕ")
        assert "int" in _normalize_error("ℤ")
        assert "real" in _normalize_error("ℝ")
        assert "nat" in _normalize_error("ℕ → ℕ")

    def test_different_errors_different_sigs(self):
        a = _error_signature("type mismatch: expected Nat, got Int")
        b = _error_signature("syntax error: unexpected token")
        assert a != b


# ══════════════════════════════════════════════════════════
# Jaccard Similarity
# ══════════════════════════════════════════════════════════

class TestJaccard:
    def test_identical(self):
        a = _normalize_error("type mismatch: expected Nat, got Int at line 42")
        b = _normalize_error("type mismatch: expected Nat, got Int at line 99")
        assert _jaccard_similarity(a, b) == 1.0

    def test_extra_diagnostic(self):
        a = _normalize_error("type mismatch: expected Nat, got Int")
        b = _normalize_error("type mismatch: expected Nat, got Int -- additional context note")
        assert _jaccard_similarity(a, b) >= 0.50

    def test_different(self):
        a = _normalize_error("type mismatch: expected Nat, got Int")
        b = _normalize_error("syntax error: unexpected token")
        assert _jaccard_similarity(a, b) < 0.3

    def test_empty(self):
        assert _jaccard_similarity("", "test") == 0.0
        assert _jaccard_similarity("test", "") == 0.0


# ══════════════════════════════════════════════════════════
# Record & Lookup
# ══════════════════════════════════════════════════════════

class TestRecordLookup:
    def test_exact_match(self, mem):
        mem.record("type mismatch: expected Real, got Int", "TYPE_MISMATCH",
                   "use natCast to convert", theorem_name="test")
        h = mem.lookup("type mismatch: expected Real, got Int", "TYPE_MISMATCH")
        assert h is not None
        assert "natCast" in h

    def test_jaccard_fallback(self, mem):
        mem.record("type mismatch: expected Real, got Int", "TYPE_MISMATCH",
                   "use natCast to convert", theorem_name="test")
        h = mem.lookup(
            "type mismatch at line 42: expected Real, got Int -- extra diag",
            "TYPE_MISMATCH"
        )
        assert h is not None, "Jaccard fallback should find match"

    def test_category_isolation(self, mem):
        mem.record("type mismatch: expected Real, got Int", "TYPE_MISMATCH",
                   "use natCast", theorem_name="test")
        h = mem.lookup("type mismatch: expected Real, got Int", "SYNTAX_ERROR")
        assert h is None

    def test_min_fix_length_filter(self, mem):
        mem.record("some error", "TEST", "short")  # < MIN_FIX_LENGTH=20
        h = mem.lookup("some error", "TEST")
        assert h is None

    def test_empty_lookup(self, mem):
        assert mem.lookup("", "TEST") is None
        assert mem.lookup("test", "") is None

    def test_miss_returns_none(self, mem):
        h = mem.lookup("unknown error", "TEST")
        assert h is None

    def test_persistence(self, mem):
        d = str(mem.cache_dir)
        mem.record("persistent error", "TEST",
                   "this is a fix template for persistence test",
                   theorem_name="t1")
        mem2 = ProofErrorMemory(cache_dir=d)
        h = mem2.lookup("persistent error", "TEST")
        assert h is not None

    def test_multiple_records_same_sig(self, mem):
        mem.record("error type A", "TYPE_MISMATCH",
                   "fix version 1 abc def ghi jkl",
                   theorem_name="t1")
        mem.record("error type A", "TYPE_MISMATCH",
                   "fix version 2 mno pqr stu vwx",
                   theorem_name="t2")
        # Both should be findable (tail scan will find last first)
        h = mem.lookup("error type A", "TYPE_MISMATCH")
        assert h is not None


# ══════════════════════════════════════════════════════════
# Stats & Prune
# ══════════════════════════════════════════════════════════

class TestMaintenance:
    def test_stats_empty(self, mem):
        s = mem.stats()
        assert s["total"] == 0

    def test_stats(self, mem):
        mem.record("error1", "CAT_A", "fix template one two three four", theorem_name="t1")
        mem.record("error2", "CAT_B", "fix template alpha beta gamma", theorem_name="t2")
        s = mem.stats()
        assert s["total"] == 2
        assert "CAT_A" in s["categories"]

    def test_prune_noop_when_small(self, mem):
        for i in range(10):
            mem.record(f"error{i}", "TEST", f"fix template number {i} for error", theorem_name=f"t{i}")
        removed = mem.prune()
        assert removed == 0  # 10 < MAX_ENTRIES

    def test_clear(self, mem):
        mem.record("error x", "TEST", "fix template long enough for test", theorem_name="tx")
        mem.clear()
        assert mem.stats()["total"] == 0


# ══════════════════════════════════════════════════════════
# Auto-classify
# ══════════════════════════════════════════════════════════

class TestAutoClassify:
    def test_algebra(self):
        assert _auto_classify("mathd_algebra_33") == "algebra"
        assert _auto_classify("algebra_sqineq") == "algebra"

    def test_contest(self):
        assert _auto_classify("imo_1992_p1") == "contest"
        assert _auto_classify("aime_1983_p1") == "contest"

    def test_number_theory(self):
        assert _auto_classify("induction_12dvd4expnp1p20") == "number_theory"

    def test_unknown(self):
        assert _auto_classify("") == "unknown"
        assert _auto_classify("custom_theorem_xyz") == "other"

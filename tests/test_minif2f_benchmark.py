"""Tests for MiniF2F benchmark runner."""

import json
from pathlib import Path

import pytest

from benchmarks.minif2f.run_benchmark import (
    Problem,
    ProblemResult,
    generate_report,
    load_problems,
    run_t1_on_problem,
)

# ── load_problems ─────────────────────────────────────────────


def _make_sample_jsonl(tmp_path: Path, n: int = 5) -> Path:
    """Create a sample MiniF2F JSONL file for testing."""
    path = tmp_path / "minif2f_sample.jsonl"
    with open(path, "w") as f:
        for i in range(n):
            entry = {
                "name": f"test_problem_{i}",
                "informal_prefix": f"Problem {i} description." if i > 0 else "",
                "formal_statement": f"theorem test_problem_{i} : True := by\n  trivial",
                "split": "test" if i % 2 == 0 else "valid",
                "lean4_code": f"theorem test_problem_{i} : True := by\n  trivial",
                "problem_id": i,
            }
            f.write(json.dumps(entry) + "\n")
    return path


class TestLoadProblems:
    def test_load_all(self, tmp_path):
        """All problems loaded."""
        path = _make_sample_jsonl(tmp_path, n=5)
        problems = load_problems(path)
        assert len(problems) == 5

    def test_load_split_filter(self, tmp_path):
        """Split filter works."""
        path = _make_sample_jsonl(tmp_path, n=5)
        problems = load_problems(path, split="test")
        assert len(problems) == 3  # 3 even indices out of 5

    def test_load_max(self, tmp_path):
        """Max limit works."""
        path = _make_sample_jsonl(tmp_path, n=10)
        problems = load_problems(path, max_problems=3)
        assert len(problems) == 3

    def test_load_max_with_split(self, tmp_path):
        """Max limit + split filter."""
        path = _make_sample_jsonl(tmp_path, n=10)
        problems = load_problems(path, split="test", max_problems=2)
        assert len(problems) == 2

    def test_missing_file(self):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_problems("/nonexistent/minif2f.jsonl")

    def test_empty_line_skipped(self, tmp_path):
        """Empty lines are skipped."""
        path = tmp_path / "empty.jsonl"
        with open(path, "w") as f:
            f.write("{}\n\n\n{}\n")
        problems = load_problems(path)
        assert len(problems) == 2


# ── run_t1_on_problem ─────────────────────────────────────────


class TestRunT1:
    def test_valid_theorem(self):
        """A valid theorem passes T1."""
        problem = Problem(
            name="test",
            informal_prefix="",
            formal_statement="theorem t : True := by\n  trivial",
            split="test",
        )
        result = run_t1_on_problem(problem)
        assert result.t1_verified is True
        assert result.name == "test"

    def test_invalid_theorem(self):
        """A broken theorem fails T1."""
        problem = Problem(
            name="broken",
            informal_prefix="",
            formal_statement="theorem t : True := by\n  sorry",
            split="test",
        )
        result = run_t1_on_problem(problem)
        assert result.t1_verified is False
        assert any("sorry" in i for i in result.t1_issues)

    def test_minif2f_style(self):
        """MiniF2F-style statement (theorem header with := by but no body after by).
        T1 passes structurally — the `:= by` indicates a proof block.
        T2 would catch the missing proof.
        """
        problem = Problem(
            name="minif2f_style",
            informal_prefix="",
            formal_statement="theorem mathd_algebra_478 (h : ℕ) : True := by\n",
            split="test",
        )
        result = run_t1_on_problem(problem)
        # T1 passes: `:= by` is syntactically valid
        assert result.t1_verified is True

    def test_elapsed_recorded(self):
        """Elapsed time is > 0."""
        problem = Problem(
            name="t", informal_prefix="", formal_statement="theorem t : 1 = 1 := rfl", split="test"
        )
        result = run_t1_on_problem(problem)
        assert result.elapsed_ms >= 0


# ── generate_report ───────────────────────────────────────────


class TestGenerateReport:
    def test_empty_results(self):
        """Empty results produce zeroed report."""
        report = generate_report([])
        assert report.total == 0
        assert report.t1_pass == 0
        assert report.t1_pass_rate == 0.0

    def test_all_pass(self):
        """All passing."""
        results = [
            ProblemResult(name="a", split="test", t1_verified=True),
            ProblemResult(name="b", split="test", t1_verified=True),
        ]
        report = generate_report(results)
        assert report.total == 2
        assert report.t1_pass == 2
        assert report.t1_pass_rate == 100.0

    def test_mixed(self):
        """Mixed pass/fail."""
        results = [
            ProblemResult(name="a", split="test", t1_verified=True),
            ProblemResult(name="b", split="test", t1_verified=False, t1_issues=["sorry at line 1"]),
            ProblemResult(name="c", split="test", t1_verified=True),
            ProblemResult(
                name="d", split="test", t1_verified=False, t1_issues=["unclosed bracket"]
            ),
        ]
        report = generate_report(results)
        assert report.total == 4
        assert report.t1_pass == 2
        assert report.t1_pass_rate == 50.0
        assert len(report.worst_issues) >= 1

    def test_full_mode_with_t2(self):
        """T2 results included in report."""
        results = [
            ProblemResult(
                name="a", split="test", t1_verified=True, t2_verified=True, t2_elapsed_ms=150
            ),
            ProblemResult(
                name="b",
                split="test",
                t1_verified=True,
                t2_verified=False,
                t2_errors=["type mismatch"],
                t2_elapsed_ms=200,
            ),
        ]
        report = generate_report(results, mode="full")
        assert report.t2_pass == 1
        assert report.t2_pass_rate == 50.0

    def test_worst_issues_collected(self):
        """Most common issues appear in worst_issues."""
        results = [
            ProblemResult(name="a", split="test", t1_verified=False, t1_issues=["sorry at line 1"]),
            ProblemResult(name="b", split="test", t1_verified=False, t1_issues=["sorry at line 1"]),
            ProblemResult(
                name="c", split="test", t1_verified=False, t1_issues=["unclosed bracket"]
            ),
        ]
        report = generate_report(results)
        assert any("2×" in w and "sorry" in w for w in report.worst_issues)

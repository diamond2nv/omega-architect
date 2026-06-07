"""Tests for OmegaRunner — multi-theorem autonomous proof search."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omega.runner import OmegaRunner, TheoremSpec, ResearchReport, Checkpoint
from omega.resource import BudgetTracker, BudgetConfig


class TestTheoremSpec:
    def test_defaults(self):
        t = TheoremSpec(header="theorem t : True :=")
        assert t.status == "pending"
        assert t.priority == 1
        assert t.domain == "general"

    def test_dependency_check(self):
        t = TheoremSpec(
            header="theorem b : True :=",
            depends_on=["theorem a : True :="],
        )
        assert "theorem a" in t.depends_on[0]


class TestCheckpoint:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            cp = Checkpoint(
                research_goal="Test goal",
                started_at="2026-06-06T10:00:00",
                elapsed_s=100.0,
                theorems=[{"header": "theorem t : True :=", "status": "succeeded"}],
                discoveries=["Found lemma X"],
                budget={"tokens": 50000},
            )
            cp.save(str(path))
            assert path.is_file()

            loaded = Checkpoint.load(str(path))
            assert loaded.research_goal == "Test goal"
            assert loaded.theorems[0]["status"] == "succeeded"
            assert loaded.discoveries == ["Found lemma X"]


class TestResearchReport:
    def test_summary_format(self):
        report = ResearchReport(
            research_goal="Test NV Hamiltonian",
            started_at="2026-06-06T10:00:00",
            total_elapsed_s=3600.0,
            theorems=[
                TheoremSpec(header="theorem t1 : True :=", status="succeeded"),
                TheoremSpec(header="theorem t2 : True :=", status="failed"),
                TheoremSpec(header="theorem t3 : True :=", status="pending"),
            ],
            discoveries=["Found lemma"],
            n_succeeded=1,
            n_failed=1,
            n_pending=1,
            budget_summary="Budget OK",
        )
        summary = report.summary()
        assert "✅ 1 succeeded" in summary
        assert "❌ 1 failed" in summary
        assert "⏳ 1 pending" in summary
        assert "Test NV Hamiltonian" in summary


class TestOmegaRunner:
    def test_init_defaults(self):
        runner = OmegaRunner()
        assert runner._time_budget == 14400.0  # 4h default
        assert runner._model_id == "local/default"

    def test_init_custom_budget(self):
        bt = BudgetTracker()
        runner = OmegaRunner(time_budget_s=7200.0, model_id="deepseek/deepseek-chat", budget_tracker=bt)
        assert runner._time_budget == 7200.0
        assert runner._model_id == "deepseek/deepseek-chat"

    def test_next_theorem_returns_highest_priority(self):
        runner = OmegaRunner()
        runner._theorems = [
            TheoremSpec(header="theorem a : True :=", priority=2),
            TheoremSpec(header="theorem b : True :=", priority=1),
            TheoremSpec(header="theorem c : True :=", priority=3),
        ]
        next_t = runner._next_theorem()
        assert next_t is not None
        assert next_t.header == "theorem c : True :="  # highest priority

    def test_next_theorem_skips_running(self):
        runner = OmegaRunner()
        runner._theorems = [
            TheoremSpec(header="theorem a : True :=", status="running"),
            TheoremSpec(header="theorem b : True :=", status="pending"),
        ]
        next_t = runner._next_theorem()
        assert next_t is not None
        assert next_t.header == "theorem b : True :="

    def test_next_theorem_waits_for_dependencies(self):
        runner = OmegaRunner()
        runner._theorems = [
            TheoremSpec(header="theorem a : True :=", status="pending", depends_on=["theorem b : True :="]),
            TheoremSpec(header="theorem b : True :=", status="pending"),
        ]
        # b has priority, but a depends on b... let me check
        # Both are pending. b has no deps, so it should be returned first
        next_t = runner._next_theorem()
        assert next_t is not None
        assert next_t.header == "theorem b : True :="

    def test_next_theorem_returns_none_when_all_done(self):
        runner = OmegaRunner()
        runner._theorems = [
            TheoremSpec(header="theorem a : True :=", status="succeeded"),
        ]
        assert runner._next_theorem() is None

    def test_time_remaining(self):
        import time
        runner = OmegaRunner(time_budget_s=10.0)
        runner._start_time = time.perf_counter()
        time.sleep(0.01)
        remaining = runner._time_remaining()
        assert 0 < remaining < 10.0

    def test_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = OmegaRunner(checkpoint_dir=tmp)
            runner._theorems = [
                TheoremSpec(header="theorem a : True :=", status="succeeded", proof_code="by trivial"),
                TheoremSpec(header="theorem b : True :=", status="pending"),
            ]
            path = runner._checkpoint()
            assert Path(path).is_file()

            runner2 = OmegaRunner(checkpoint_dir=tmp)
            cp = Checkpoint.load(path)
            runner2._restore_from_checkpoint(cp)
            assert len(runner2._theorems) == 2
            assert runner2._theorems[0].status == "succeeded"

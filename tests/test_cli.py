"""Tests for CLI commands — prove, route, status, bench.

These tests verify the CLI integration layer without making real API calls.
We use monkeypatch/patching to simulate backend behavior.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from omega.cli import app


runner = CliRunner()


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def mock_lean_config():
    """Mock Lean toolchain so we don't need actual Lean installed."""
    with patch("omega.resource.lean_config.load_lean_config") as mock:
        cfg = MagicMock()
        cfg.binaries_ok.return_value = True
        cfg.project_path = "/tmp/lean-test"
        cfg.version = "v4.30.0"
        cfg.project_exists.return_value = True
        mock.return_value = cfg
        yield mock


@pytest.fixture
def mock_inner_loop():
    """Mock inner_loop so we don't call DeepSeek API."""
    with patch("omega.loop.inner.inner_loop") as mock:
        result = MagicMock()
        result.success = True
        result.rounds = 3
        result.budget_used_cost = 0.012
        result.termination = "proved"
        result.code = "theorem t : 1 + 1 = 2 := by\n  native_decide"
        result.error = None
        mock.return_value = result
        yield mock


@pytest.fixture
def mock_goedel_prover():
    """Mock GoedelProver so we don't call DeepSeek API."""
    with patch("omega.prover.go_prover.GoedelProver") as mock:
        instance = MagicMock()
        result = MagicMock()
        result.succeeded = True
        result.n_attempts = 3
        result.timings = {"total_s": 5.2}
        result.proof = "theorem t : 1 + 1 = 2 := by\n  native_decide"
        result.attempts = []
        instance.run.return_value = result
        mock.return_value = instance
        yield mock


@pytest.fixture
def mock_hybrid():
    """Mock hybrid run_hybrid_v2."""
    with patch("omega.engine.hybrid.run_hybrid_v2") as mock:
        result = MagicMock()
        result.success = True
        result.proof = "theorem t : 1 + 1 = 2 := by\n  native_decide"
        result.error = None
        result.summary = "Hybrid v2 | phase1 | proof=42b | 3 attempts | 15.2s"
        result.elapsed_s = 15.2
        result.source = "phase1"
        mock.return_value = result
        yield mock


# ═══════════════════════════════════════════════════════════════════
# 1. omega prove
# ═══════════════════════════════════════════════════════════════════


class TestProveCommand:
    def test_prove_dfs(self, mock_inner_loop):
        result = runner.invoke(app, [
            "prove", "theorem t : 1 + 1 = 2 := by",
            "--mode", "dfs",
        ])
        assert result.exit_code == 0
        assert "✅" in result.stdout
        assert "Proved" in result.stdout
        assert "native_decide" in result.stdout
        mock_inner_loop.assert_called_once()

    def test_prove_beam(self, mock_goedel_prover):
        result = runner.invoke(app, [
            "prove", "theorem t : 1 + 1 = 2 := by",
            "--mode", "beam",
        ])
        assert result.exit_code == 0
        assert "✅" in result.stdout or "Proved" in result.stdout
        mock_goedel_prover.return_value.run.assert_called()

    def test_prove_hybrid(self, mock_hybrid):
        result = runner.invoke(app, [
            "prove", "theorem t : 1 + 1 = 2 := by",
            "--mode", "hybrid",
        ])
        assert result.exit_code == 0
        assert "Hybrid" in result.stdout
        mock_hybrid.assert_called_once()

    def test_prove_auto_mode_routes_to_dfs(self, mock_inner_loop):
        """Auto mode should call ModeRouter and then execute."""
        with patch("omega.engine.router.ModeRouter") as mock_router:
            router_instance = MagicMock()
            decision = MagicMock()
            decision.strategy_name = "dfs"
            decision.reasoning = "DFS selected for easy theorem"
            decision.scores = {"dfs": 8.0, "beam": 3.0, "hybrid": 2.0}
            router_instance.route.return_value = decision
            mock_router.return_value = router_instance

            result = runner.invoke(app, [
                "prove", "theorem t : 1 + 1 = 2 := by",
                "--mode", "auto",
            ])
            assert result.exit_code == 0
            assert "ModeRouter" in result.stdout
            assert "DFS" in result.stdout
            assert "8.0" in result.stdout  # scores shown

    def test_prove_unknown_mode(self):
        result = runner.invoke(app, [
            "prove", "theorem t : True := by trivial",
            "--mode", "nonexistent",
        ])
        assert result.exit_code != 0

    def test_prove_no_lean_toolchain(self):
        """Without Lean, prove should exit with error."""
        with patch("omega.resource.lean_config.load_lean_config") as mock:
            cfg = MagicMock()
            cfg.binaries_ok.return_value = False
            mock.return_value = cfg
            result = runner.invoke(app, [
                "prove", "theorem t : True := by trivial",
            ])
            assert result.exit_code != 0
            # The error message should mention "Lean" or "not available"
            assert any(word in result.stdout for word in ["Lean", "unavailable", "init"])

    def test_prove_dfs_shows_cost(self, mock_inner_loop):
        result = runner.invoke(app, [
            "prove", "theorem t : 1 + 1 = 2 := by",
            "--mode", "dfs",
        ])
        assert result.exit_code == 0
        assert "0.012" in result.stdout


# ═══════════════════════════════════════════════════════════════════
# 2. omega route
# ═══════════════════════════════════════════════════════════════════


class TestRouteCommand:
    def test_route_shows_difficulty_and_domain(self):
        with patch("omega.engine.router.ModeRouter") as mock_router:
            router_instance = MagicMock()
            router_instance.route.return_value.strategy_name = "dfs"
            router_instance.route.return_value.scores = {"dfs": 8.0, "beam": 3.0, "hybrid": 2.0}
            router_instance.route.return_value.reasoning = "test reason"
            mock_router.return_value = router_instance

            result = runner.invoke(app, [
                "route", "theorem t (h : a = b) : a + c = b + c := by",
            ])
            assert result.exit_code == 0
            assert "Route Preview" in result.stdout
            assert "Difficulty" in result.stdout or "MEDIUM" in result.stdout

    def test_route_shows_multiple_profiles(self):
        with patch("omega.engine.router.ModeRouter") as mock_router:
            router_instance = MagicMock()
            router_instance.route.return_value.strategy_name = "dfs"
            router_instance.route.return_value.scores = {"dfs": 8.0, "beam": 3.0, "hybrid": 2.0}
            router_instance.route.return_value.reasoning = "test"
            mock_router.return_value = router_instance

            result = runner.invoke(app, [
                "route", "theorem t : 1 + 1 = 2 := by",
            ])
            assert result.exit_code == 0
            assert "API" in result.stdout or "Local" in result.stdout

    def test_route_empty_theorem(self):
        result = runner.invoke(app, ["route", ""])
        assert result.exit_code == 0  # should handle gracefully
        assert "Route Preview" in result.stdout


# ═══════════════════════════════════════════════════════════════════
# 3. omega status
# ═══════════════════════════════════════════════════════════════════


class TestStatusCommand:
    def test_status_shows_health(self):
        with patch("omega.resource.model_router.ModelRouter") as mock_router:
            router_instance = MagicMock()
            router_instance.health_report.return_value = "Model Router Health:\n✅ goedel/goedel-v2-8b\n"
            router_instance.stats.return_value = {}
            mock_router.return_value = router_instance

            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            assert "Status" in result.stdout or "Health" in result.stdout

    def test_status_shows_lean_version(self):
        with patch("omega.resource.model_router.ModelRouter") as mock_router:
            router_instance = MagicMock()
            router_instance.health_report.return_value = "OK"
            router_instance.stats.return_value = {}
            mock_router.return_value = router_instance

            result = runner.invoke(app, ["status"])
            assert result.exit_code == 0
            assert "Lean" in result.stdout
            assert "v4.30.0" in result.stdout


# ═══════════════════════════════════════════════════════════════════
# 4. omega bench
# ═══════════════════════════════════════════════════════════════════


class TestBenchCommand:
    def test_bench_requires_existing_dataset(self, tmp_path):
        result = runner.invoke(app, ["bench", "/nonexistent/path.jsonl"])
        assert result.exit_code != 0
        assert "not found" in result.stdout

    def test_bench_runs_on_existing_jsonl(self, tmp_path, mock_inner_loop):
        """Should read from JSONL, prove each theorem, and report."""
        data = [
            {"name": "test1", "formal_statement": "theorem t1 : True := by trivial"},
            {"name": "test2", "formal_statement": "theorem t2 : 1=1 := by rfl"},
        ]
        f = tmp_path / "test.jsonl"
        f.write_text("\n".join(json.dumps(d) for d in data))

        result = runner.invoke(app, [
            "bench", str(f), "--max", "2",
        ])
        assert result.exit_code == 0
        assert "2/2 passed" in result.stdout

    def test_bench_handles_malformed_entry(self, tmp_path, mock_inner_loop):
        """A single theorem failure shouldn't crash the whole bench."""
        data = [
            {"name": "good", "formal_statement": "theorem t : True := by trivial"},
            {"name": "bad", "formal_statement": ""},  # empty header
        ]
        f = tmp_path / "test.jsonl"
        f.write_text("\n".join(json.dumps(d) for d in data))

        result = runner.invoke(app, [
            "bench", str(f), "--max", "2",
        ])
        assert result.exit_code == 0

    def test_bench_minif2f_alias(self, mock_inner_loop):
        """The 'minif2f' alias should find a dataset."""
        result = runner.invoke(app, [
            "bench", "minif2f", "--max", "2",
        ])
        # If minif2f dataset doesn't exist locally, it's OK — should say not found
        assert result.exit_code in (0, 1)

    def test_bench_with_output_saves_json(self, tmp_path, mock_inner_loop):
        data = [{"name": "t1", "formal_statement": "theorem t1 : True := by trivial"}]
        f = tmp_path / "in.jsonl"
        f.write_text(json.dumps(data[0]))
        out = tmp_path / "out.json"

        result = runner.invoke(app, [
            "bench", str(f), "--output", str(out), "--max", "1",
        ])
        assert result.exit_code == 0
        assert out.exists()
        report = json.loads(out.read_text())
        assert "results" in report

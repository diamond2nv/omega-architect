#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Verification gates for omega-architect (public/open-source ready).

Self-contained — zero dependency on hermes-verify or any internal package.
Uses standard pytest markers registered in pyproject.toml.

Gate legend:
  B10 — BudgetGate: BudgetTracker init, consume, check, local vs remote tier
  B11 — ModelRegistryGate: Model registry with models, API name resolution, pricing
  C01 — TrajectoryGate: ProofState/ProofAction/Trajectory dataclass creation
  C02 — ResourceGate: Config dataclasses, ConvergenceTracker
  F01 — VersionGate: Version consistency + module imports + file structure
  F02 — GPULayerGate: GPU detection, backend capability abstraction
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ═════════════════════════════════════════════════════════════════
# B10 — BudgetGate: BudgetTracker 验证
# ═════════════════════════════════════════════════════════════════

pytestmark_gate_b = pytest.mark.gate_b


class TestBudgetGate:
    """B10: BudgetTracker initialization and budget tracking."""

    def test_budget_tracker_imports(self):
        from omega.resource.budget import BudgetTracker
        assert BudgetTracker is not None

    def test_budget_tracker_init_default(self):
        from omega.resource.budget import BudgetTracker
        bt = BudgetTracker()
        assert bt is not None

    def test_budget_tracker_consume(self):
        from omega.resource.budget import BudgetTracker
        bt = BudgetTracker()
        bt.consume(input_tokens=100, output_tokens=50, model_id="local/test", elapsed_s=0.5)
        summary = bt.summary()
        # summary is a formatted string for display
        assert isinstance(summary, str)
        assert "tokens" in summary.lower() or "api_calls" in summary

    def test_budget_tracker_is_free(self):
        from omega.resource.budget import BudgetTracker
        bt = BudgetTracker()
        assert bt.is_free_model("ollama/test") is True

    def test_budget_tracker_is_free_remote(self):
        from omega.resource.budget import BudgetTracker
        bt = BudgetTracker()
        result = bt.is_free_model("deepseek/deepseek-chat")
        assert isinstance(result, bool)


# ═════════════════════════════════════════════════════════════════
# B11 — ModelRegistryGate: 模型注册表验证
# ═════════════════════════════════════════════════════════════════


class TestModelRegistryGate:
    """B11: ModelRegistry correctness."""

    def test_models_populated(self):
        from omega.resource.model_registry import MODELS
        assert len(MODELS) >= 3

    def test_resolve_api_name(self):
        from omega.resource.model_registry import resolve_api_name
        name = resolve_api_name("deepseek/deepseek-v4-flash")
        assert name is not None

    def test_is_free_model_true(self):
        from omega.resource.model_registry import is_free_model
        assert is_free_model("ollama/gemma4:26b") is True

    def test_pricing_known_model(self):
        from omega.resource.model_registry import MODELS
        for mid in ("deepseek-v4-flash", "deepseek-v4-pro"):
            if mid in MODELS:
                assert "pricing" in MODELS[mid]

    def test_get_capabilities(self):
        from omega.resource.model_registry import get_capabilities
        caps = get_capabilities("deepseek-v4-flash")
        assert isinstance(caps, list)


# ═════════════════════════════════════════════════════════════════
# C01 — TrajectoryGate: 轨迹数据结构验证 (needs openai)
# ═════════════════════════════════════════════════════════════════

pytestmark_gate_c = pytest.mark.gate_c


class TestTrajectoryGate:
    """C01: ProofState/ProofAction/Trajectory dataclass structure."""

    @staticmethod
    def _ensure_deps():
        pytest.importorskip("openai", reason="openai required for engine imports")

    def test_proof_state_creation(self):
        self._ensure_deps()
        from omega.engine.trajectory import ProofState
        state = ProofState(theorem="test thm", code="example", errors=[])
        assert state.theorem == "test thm"
        assert state.code == "example"

    def test_proof_action_creation(self):
        self._ensure_deps()
        from omega.engine.trajectory import ProofAction
        action = ProofAction(type="tactic", content="apply lemma")
        assert action.type == "tactic"
        assert action.content == "apply lemma"

    def test_trajectory_steps(self):
        self._ensure_deps()
        from omega.engine.trajectory import Trajectory, ProofState, ProofAction
        traj = Trajectory(theorem="test")
        s = ProofState(theorem="test", code="code", errors=[])
        a = ProofAction(type="tactic", content="apply")
        traj.add_step(s, a, success=True)
        assert len(traj.steps) == 1
        assert traj.success is True

    def test_trajectory_serialize(self):
        self._ensure_deps()
        from omega.engine.trajectory import Trajectory
        traj = Trajectory(theorem="test", strategy="dfs")
        d = traj.to_dict()
        assert isinstance(d, dict)
        assert "theorem" in d


# ═════════════════════════════════════════════════════════════════
# C02 — ResourceGate: 资源管理数据类验证
# ═════════════════════════════════════════════════════════════════


class TestResourceGate:
    """C02: Config dataclasses and ConvergenceTracker."""

    def test_budget_config_dataclass(self):
        from omega.resource.config import BudgetConfig
        cfg = BudgetConfig()
        assert cfg is not None

    def test_budget_tier_dataclass(self):
        from omega.resource.config import BudgetTier
        tier = BudgetTier()
        assert tier is not None

    def test_convergence_tracker_imports(self):
        from omega.resource.tracker import ConvergenceTracker
        assert ConvergenceTracker is not None

    def test_convergence_tracker_record(self):
        from omega.resource.tracker import ConvergenceTracker
        ct = ConvergenceTracker(window=3, convergence_threshold=0.1)
        ct.record_epoch(n_errors=3, proof_length=120, elapsed_s=10.5, errors=[])
        assert len(ct._epochs) == 1

    def test_convergence_tracker_status(self):
        from omega.resource.tracker import ConvergenceTracker
        ct = ConvergenceTracker(window=3, convergence_threshold=0.1)
        ct.record_epoch(n_errors=3, proof_length=100, elapsed_s=5, errors=[])
        ct.record_epoch(n_errors=3, proof_length=100, elapsed_s=5, errors=[])
        ct.record_epoch(n_errors=3, proof_length=100, elapsed_s=5, errors=[])
        assert isinstance(ct.is_stuck(), bool)
        assert isinstance(ct.is_diverging(), bool)


# ═════════════════════════════════════════════════════════════════
# F01 — VersionGate: 版本一致性 + 导入结构
# ═════════════════════════════════════════════════════════════════

pytestmark_gate_f = pytest.mark.gate_f


class TestVersionGate:
    """F01: Package version consistency + import structure."""

    REPO_ROOT = Path(__file__).resolve().parent.parent

    def test_version_defined(self):
        from omega import __version__
        assert __version__ and isinstance(__version__, str)

    def test_version_matches_pyproject(self):
        import tomllib
        from omega import __version__

        data = tomllib.loads(
            (self.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert __version__ == data.get("project", {}).get("version", "")

    def test_resource_modules_importable(self):
        for mod in (
            "omega.resource.budget",
            "omega.resource.model_registry",
            "omega.resource.config",
            "omega.resource.tracker",
        ):
            __import__(mod)

    def test_cli_entry_point_defined(self):
        import tomllib
        data = tomllib.loads(
            (self.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert "omega" in data.get("project", {}).get("scripts", {})

    def test_license_exists(self):
        assert (self.REPO_ROOT / "LICENSE").exists()

    def test_readme_exists(self):
        assert (self.REPO_ROOT / "README.md").exists()

    def test_no_internal_deps(self):
        import tomllib
        data = tomllib.loads(
            (self.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        for dep in data.get("project", {}).get("dependencies", []):
            assert "hermes-verify" not in dep


# ═════════════════════════════════════════════════════════════════
# F02 — GPULayerGate: GPU 层基础功能验证
# ═════════════════════════════════════════════════════════════════


class TestGPULayerGate:
    """F02: GPU detection and backend abstraction."""

    def test_detect_all_imports(self):
        from omega.gpu_layer.detector import detect_all
        assert callable(detect_all)

    def test_detect_all_returns(self):
        from omega.gpu_layer.detector import detect_all
        result = detect_all()
        assert result is not None

    def test_backend_classes(self):
        from omega.gpu_layer.backends import VLLMBackend, OllamaBackend, TransformersBackend
        assert VLLMBackend is not None
        assert OllamaBackend is not None
        assert TransformersBackend is not None

    def test_scheduler_imports(self):
        from omega.gpu_layer.scheduler import GPUScheduler
        assert GPUScheduler is not None

    def test_capability_classify_imports(self):
        from omega.gpu_layer.capability import classify_capability
        assert callable(classify_capability)

    def test_capability_describe_imports(self):
        from omega.gpu_layer.capability import describe_capability
        assert callable(describe_capability)

"""Tests for config upgrade: BudgetConfig, file loading, validation, merge."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from omega.resource.config import (
    BudgetConfig,
    EpochConfig,
    _load_env_overrides,
    get,
    load_config,
    reset_config_cache,
)

# ── BudgetConfig validation ─────────────────────────────────────


class TestBudgetConfig:
    def test_defaults(self):
        cfg = BudgetConfig()
        assert cfg.max_tokens == 1_000_000
        assert cfg.max_cost_usd == 0.50
        assert cfg.max_time_s == 300.0
        assert cfg.max_attempts == 50
        assert cfg.min_confidence == 0.3

    def test_validation_positive_int(self):
        with pytest.raises(ValueError, match="must be positive"):
            BudgetConfig(max_tokens=-100)

    def test_validation_positive_float(self):
        with pytest.raises(ValueError, match="must be >= 0"):
            BudgetConfig(max_cost_usd=-1.0)

    def test_validation_min_confidence_range(self):
        with pytest.raises(ValueError, match="must be in"):
            BudgetConfig(min_confidence=1.5)

    def test_epochs_from_dict(self):
        cfg = BudgetConfig(epochs={"max_epochs": 3, "convergence_threshold": 0.05, "window": 4})
        assert cfg.epochs.max_epochs == 3
        assert cfg.epochs.convergence_threshold == 0.05
        assert cfg.epochs.window == 4


# ── Serialization ────────────────────────────────────────────────


class TestBudgetConfigSerialization:
    def test_to_dict_roundtrip(self):
        cfg1 = BudgetConfig(max_tokens=500_000, max_cost_usd=1.0)
        d = cfg1.to_dict()
        cfg2 = BudgetConfig.from_dict(d)
        assert cfg2.max_tokens == 500_000
        assert cfg2.max_cost_usd == 1.0
        assert cfg2.epochs.max_epochs == cfg1.epochs.max_epochs

    def test_to_toml_contains_keys(self):
        cfg = BudgetConfig()
        toml = cfg.to_toml()
        assert "max_tokens" in toml
        assert "max_cost_usd" in toml
        assert "max_attempts" in toml

    def test_merge_dot_path(self):
        cfg = BudgetConfig()
        merged = cfg.merge({"budget.max_tokens": 2_000_000})
        assert merged.max_tokens == 2_000_000
        # Original unchanged
        assert cfg.max_tokens == 1_000_000

    def test_merge_nested(self):
        cfg = BudgetConfig()
        merged = cfg.merge({"models.free": ["test/"]})
        assert merged.free_models == ["test/"]

    def test_from_dict_with_source(self):
        cfg = BudgetConfig.from_dict({"budget": {"max_tokens": 999}}, source="test.toml")
        assert cfg.max_tokens == 999
        assert cfg._source_file == "test.toml"


# ── Config file loading ──────────────────────────────────────────


class TestConfigFileLoading:
    def _write_toml(self, path: str, data: dict) -> str:
        """Write a minimal TOML file."""
        import tomllib  # noqa: F401  # ensure available

        # We write a proper TOML manually
        lines = ["[budget]", f"max_tokens = {data.get('max_tokens', 1000000)}", ""]
        if "max_cost_usd" in data:
            lines.insert(1, f"max_cost_usd = {data['max_cost_usd']}")
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        return path

    @pytest.fixture(autouse=True)
    def _no_cache(self):
        reset_config_cache()
        yield
        reset_config_cache()

    def test_load_from_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._write_toml(f"{tmp}/omega.toml", {"max_tokens": 500_000})
            cfg = load_config(config_path=cfg_path, use_cache=False)
            assert cfg.max_tokens == 500_000
            assert cfg._source_file == cfg_path

    def test_load_defaults_when_no_file(self):
        cfg = load_config(use_cache=False)
        assert cfg.max_tokens == 1_000_000  # defaults preserved

    def test_load_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = self._write_toml(f"{tmp}/omega.toml", {"max_tokens": 500_000})
            os.environ["OMEGA_BUDGET__MAX_TOKENS"] = "750000"
            try:
                cfg = load_config(config_path=cfg_path, use_cache=False)
                assert cfg.max_tokens == 750000  # env overrides file
            finally:
                del os.environ["OMEGA_BUDGET__MAX_TOKENS"]

    def test_load_cli_overrides(self):
        cfg = load_config(cli_overrides={"budget.max_tokens": 200_000}, use_cache=False)
        assert cfg.max_tokens == 200_000


# ── Env var parsing ──────────────────────────────────────────────


class TestEnvOverrideParsing:
    def test_env_int_parsing(self):
        os.environ["OMEGA_BUDGET__MAX_TOKENS"] = "750000"
        try:
            overrides = _load_env_overrides()
            assert overrides.get("budget.max_tokens") == 750000
        finally:
            del os.environ["OMEGA_BUDGET__MAX_TOKENS"]

    def test_env_list_parsing(self):
        os.environ["OMEGA_MODELS__FREE"] = '["hf/", "local/"]'
        try:
            overrides = _load_env_overrides()
            val = overrides.get("models.free")
            assert val == ["hf/", "local/"]
        finally:
            del os.environ["OMEGA_MODELS__FREE"]


# ── Dot-path getter ──────────────────────────────────────────────


class TestGet:
    def test_get_existing(self):
        value = get("budget.max_tokens")
        assert value == 1_000_000

    def test_get_default(self):
        value = get("nonexistent.key", 42)
        assert value == 42

    def test_get_from_cfg(self):
        cfg = BudgetConfig(max_tokens=777)
        value = get("budget.max_tokens", cfg=cfg)
        assert value == 777


# ── EpochConfig ──────────────────────────────────────────────────


class TestEpochConfig:
    def test_defaults(self):
        ep = EpochConfig()
        assert ep.max_epochs == 5
        assert ep.convergence_threshold == 0.1
        assert ep.window == 3

    def test_window_must_be_at_least_2(self):
        with pytest.raises(ValueError, match="window must be >= 2"):
            EpochConfig(window=1)

    def test_validation(self):
        with pytest.raises(ValueError, match="must be positive"):
            EpochConfig(max_epochs=0)

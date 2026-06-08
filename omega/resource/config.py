#!/usr/bin/env python3
"""Omega budget configuration with validation, file loading, and override chain.

Usage:
    >>> from omega.resource.config import BudgetConfig, load_config, get
    >>> cfg = load_config()                          # defaults + file + env
    >>> cfg.max_tokens
    1000000
    >>> get(cfg, "models.free")
    ["ollama/", "local/"]
    >>> cfg.to_toml()                                 # serialize to TOML string

Config loading chain (later overrides earlier):
    1. BudgetConfig() defaults (hardcoded)
    2. omega.toml / ~/.omega/config.toml (optional file overrides)
    3. OMEGA_* environment variables
    4. CLI --budget-* flags (passed as dict to merge())
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]
# ── Validation helpers ──────────────────────────────────────────


def _positive_int(v: Any, field_name: str = "") -> int:
    n = int(v)
    if n <= 0:
        raise ValueError(f"{field_name or 'value'} must be positive, got {n}")
    return n


def _positive_float(v: Any, field_name: str = "") -> float:
    n = float(v)
    if n < 0:
        raise ValueError(f"{field_name or 'value'} must be >= 0, got {n}")
    return n


def _range_0_1(v: Any, field_name: str = "") -> float:
    n = float(v)
    if not (0 <= n <= 1.0):
        raise ValueError(f"{field_name or 'value'} must be in [0, 1], got {n}")
    return n


# ── EpochConfig ─────────────────────────────────────────────────


@dataclass
class EpochConfig:
    """Correction-round convergence tracking configuration."""

    max_epochs: int = 5
    convergence_threshold: float = 0.1
    window: int = 3

    def __post_init__(self) -> None:
        self.max_epochs = _positive_int(self.max_epochs, "max_epochs")
        self.convergence_threshold = _range_0_1(self.convergence_threshold, "convergence_threshold")
        self.window = _positive_int(self.window, "window")
        if self.window < 2:
            raise ValueError("window must be >= 2")

    def to_dict(self) -> dict:
        return asdict(self)


# ── BudgetConfig ────────────────────────────────────────────────


DEFAULT_MODEL_PRICES: dict[str, dict[str, float]] = {
    # DeepSeek V4 official pricing (June 2026, post-discount permanent prices)
    # Source: https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    # Pro: ¥3/M input (was ¥12), Flash: ¥1/M input. USDRMB ≈ 7.2
    "deepseek/deepseek-v4-flash": {
        "input_per_token": 1.0 / 7.2 * 1e-6,  # $0.139/M (¥1/M)
        "output_per_token": 2.0 / 7.2 * 1e-6,  # $0.278/M (¥2/M)
    },
    "deepseek/deepseek-v4-pro": {
        "input_per_token": 3.0 / 7.2 * 1e-6,  # $0.417/M (¥3/M)
        "output_per_token": 6.0 / 7.2 * 1e-6,  # $0.833/M (¥6/M)
    },
    "anthropic/claude-sonnet-4": {
        "input_per_token": 3.0e-6,  # $3/M
        "output_per_token": 1.5e-5,  # $15/M
    },
    "openrouter/anthropic/claude-sonnet-4": {
        "input_per_token": 3.0e-6,
        "output_per_token": 1.5e-5,
    },
}


@dataclass
class BudgetTier:
    """Budget limits for a specific model tier (local or remote).

    When ``tok_s > 0``, the BudgetTracker uses **time-based dynamic budget**
    instead of hardcoded ``max_tokens``: the effective token limit is
    ``min(max_tokens, remaining_time × tok_s)``.  This binds the budget to
    real hardware throughput rather than an arbitrary number.

    When ``tok_s == 0`` (default), token counting falls back to the
    traditional static ``max_tokens`` limit.
    """

    max_tokens: int = 1_000_000
    """Maximum total tokens (soft cap when tok_s > 0)."""

    max_cost_usd: float = 0.50
    """Maximum USD cost (0 for local)."""

    max_time_s: float = 300.0
    """Maximum wall-clock seconds."""

    max_attempts: int = 50
    """Maximum attempts."""

    min_confidence: float = 0.3
    """Minimum proposer confidence."""

    tok_s: float = 0.0
    """Measured tokens-per-second throughput (0 = use static max_tokens)."""

    def __post_init__(self) -> None:
        from omega.resource.config import _positive_float, _positive_int, _range_0_1

        self.max_tokens = _positive_int(self.max_tokens, "max_tokens")
        self.max_cost_usd = _positive_float(self.max_cost_usd, "max_cost_usd")
        self.max_time_s = _positive_float(self.max_time_s, "max_time_s")
        self.max_attempts = _positive_int(self.max_attempts, "max_attempts")
        self.min_confidence = _range_0_1(self.min_confidence, "min_confidence")
        self.tok_s = _positive_float(self.tok_s, "tok_s")

    def effective_token_budget(self, remaining_time_s: float = 0.0) -> int:
        """Compute the effective token budget.

        When ``tok_s > 0``, uses ``min(max_tokens, remaining_time_s × tok_s)``.
        When ``tok_s == 0``, returns ``max_tokens`` directly (traditional).
        """
        if self.tok_s > 0 and remaining_time_s > 0:
            dynamic = int(remaining_time_s * self.tok_s)
            return min(self.max_tokens, dynamic)
        return self.max_tokens

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_time_s": self.max_time_s,
            "max_attempts": self.max_attempts,
            "min_confidence": self.min_confidence,
            "tok_s": self.tok_s,
        }


# ── Default tier values ─────────────────────────────────────────

LOCAL_DEFAULT = BudgetTier(
    max_tokens=100_000_000,  # 100M safety cap (will never be reached — time-bound)
    max_cost_usd=0.0,  # local is free
    max_time_s=43200,  # 12 hours
    max_attempts=10_000,  # 10K T2 attempts
    min_confidence=0.2,  # local is cheap, lower bar
    tok_s=15.0,  # conservative throughput (RTX 4500 Ada, Q4_K_M)
)

REMOTE_DEFAULT = BudgetTier(
    max_tokens=5_000_000,  # 5M tokens — ~$1.40 DeepSeek
    max_cost_usd=2.00,  # $2 max spend
    max_time_s=3600,  # 1 hour
    max_attempts=500,  # 500 attempts
    min_confidence=0.4,  # paid model, higher bar
)


@dataclass
class BudgetConfig:
    """Validated budget configuration for one Omega theorem-proving run.

    Supports dual-tier budgets: ``local`` (free/ollama models) and
    ``remote`` (paid API models).  The ``BudgetTracker`` automatically
    selects the right tier based on ``is_free_model()``.
    """

    # ── Budget dimensions (defaults to remote) ──
    max_tokens: int = 1_000_000
    """Maximum total tokens (prompt + completion) across all models."""

    max_cost_usd: float = 0.50
    """Maximum USD cost (paid providers only; free models don't count)."""

    max_time_s: float = 300.0
    """Maximum wall-clock seconds per theorem."""

    max_attempts: int = 50
    """Maximum T2 compile attempts."""

    min_confidence: float = 0.3
    """Minimum proposer confidence to attempt (0.0 = accept all)."""

    # ── Epoch tracking ──
    epochs: EpochConfig = field(default_factory=EpochConfig)

    # ── Models ──
    free_models: list[str] = field(default_factory=lambda: ["ollama/", "local/"])
    """Prefixes for free (zero-cost) models."""

    model_prices: dict[str, dict[str, float]] = field(
        default_factory=lambda: dict(DEFAULT_MODEL_PRICES)
    )
    """Model ID → {input_per_token, output_per_token} in USD."""

    # ── Dual-tier budgets ──
    local: BudgetTier = field(default_factory=lambda: __import__("copy").copy(LOCAL_DEFAULT))
    """Budget limits for local/free models (ollama, local/...)."""

    remote: BudgetTier = field(default_factory=lambda: __import__("copy").copy(REMOTE_DEFAULT))
    """Budget limits for paid API models (DeepSeek, Claude, ...)."""

    # ── Research budget ──
    research_max_tokens: int = 50_000
    """Tokens reserved for the knowledge-research phase."""

    research_max_time_s: float = 60.0
    """Seconds reserved for the knowledge-research phase."""

    max_papers: int = 5
    """Max related papers to retrieve during research."""

    max_github_results: int = 10
    """Max GitHub Lean4 code results to retrieve."""

    # ── Config source tracking ──
    _source_file: str = ""
    """Path to the config file this was loaded from (empty = defaults)."""

    def __post_init__(self) -> None:
        # Validate all fields
        self.max_tokens = _positive_int(self.max_tokens, "max_tokens")
        self.max_cost_usd = _positive_float(self.max_cost_usd, "max_cost_usd")
        self.max_time_s = _positive_float(self.max_time_s, "max_time_s")
        self.max_attempts = _positive_int(self.max_attempts, "max_attempts")
        self.min_confidence = _range_0_1(self.min_confidence, "min_confidence")
        self.research_max_tokens = _positive_int(self.research_max_tokens, "research_max_tokens")
        self.research_max_time_s = _positive_float(self.research_max_time_s, "research_max_time_s")
        self.max_papers = _positive_int(self.max_papers, "max_papers")
        self.max_github_results = _positive_int(self.max_github_results, "max_github_results")

        # If epochs is a plain dict, convert to EpochConfig
        if isinstance(self.epochs, dict):
            self.epochs = EpochConfig(**self.epochs)
        elif not isinstance(self.epochs, EpochConfig):
            raise TypeError(f"epochs must be EpochConfig or dict, got {type(self.epochs)}")

    # ── Serialisation ──

    def to_dict(self) -> dict[str, Any]:
        """Export to a plain dict (JSON-safe)."""
        return {
            "budget": {
                "max_tokens": self.max_tokens,
                "max_cost_usd": self.max_cost_usd,
                "max_time_s": self.max_time_s,
                "max_attempts": self.max_attempts,
                "min_confidence": self.min_confidence,
                "epochs": self.epochs.to_dict(),
            },
            "research": {
                "max_tokens": self.research_max_tokens,
                "max_time_s": self.research_max_time_s,
                "max_papers": self.max_papers,
                "max_github_results": self.max_github_results,
            },
            "models": {
                "free": list(self.free_models),
                **{k: dict(v) for k, v in self.model_prices.items()},
            },
        }

    def to_toml(self) -> str:
        """Export to TOML string (for ``omega config show`` / template)."""
        d = self.to_dict()
        lines: list[str] = []
        lines.append("# Omega Budget Configuration")
        lines.append(f"# Source: {self._source_file or 'defaults'}")
        lines.append("")

        def _write_section(prefix: str, data: dict, depth: int = 0) -> None:
            indent = "  " * depth
            for k, v in data.items():
                if isinstance(v, dict):
                    if depth == 0:
                        lines.append(f"[{prefix}{k}]")
                        _write_section("", v, depth + 1)
                    else:
                        _write_section(f"{prefix}{k}.", v, depth + 1)
                elif isinstance(v, list):
                    lines.append(f"{indent}{k} = {json.dumps(v)}")
                elif isinstance(v, bool):
                    lines.append(f"{indent}{k} = {'true' if v else 'false'}")
                elif isinstance(v, float):
                    # Small floats (prices) keep full precision
                    if abs(v) < 0.001:
                        lines.append(f"{indent}{k} = {v!r}")
                    else:
                        lines.append(f"{indent}{k} = {v}")
                elif isinstance(v, int):
                    lines.append(f"{indent}{k} = {v}")
                else:
                    lines.append(f'{indent}{k} = "{v}"')
            if depth == 0:
                lines.append("")

        _write_section("", d)
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, d: dict[str, Any], source: str = "") -> BudgetConfig:
        """Create a BudgetConfig from a nested dict (e.g., parsed TOML)."""
        b = d.get("budget", {})
        epochs_data = b.get("epochs", {})
        r = d.get("research", {})
        m = d.get("models", {})

        return cls(
            max_tokens=b.get("max_tokens", 1_000_000),
            max_cost_usd=b.get("max_cost_usd", 0.50),
            max_time_s=b.get("max_time_s", 300.0),
            max_attempts=b.get("max_attempts", 50),
            min_confidence=b.get("min_confidence", 0.3),
            epochs=EpochConfig(
                max_epochs=epochs_data.get("max_epochs", 5),
                convergence_threshold=epochs_data.get("convergence_threshold", 0.1),
                window=epochs_data.get("window", 3),
            ),
            free_models=m.get("free", ["ollama/", "local/"]),
            model_prices={
                k: {
                    "input_per_token": v.get("input_per_token", 0),
                    "output_per_token": v.get("output_per_token", 0),
                }
                for k, v in m.items()
                if k != "free" and isinstance(v, dict)
            },
            research_max_tokens=r.get("max_tokens", 50_000),
            research_max_time_s=r.get("max_time_s", 60.0),
            max_papers=r.get("max_papers", 5),
            max_github_results=r.get("max_github_results", 10),
            _source_file=source,
        )

    def merge(self, overrides: dict[str, Any]) -> BudgetConfig:
        """Return a new BudgetConfig with *overrides* merged on top.

        Supports dot-notation keys: ``{"budget.max_tokens": 2000000}``
        or section keys: ``{"max_tokens": 2000000, "models.free": ["hf/"]}``.
        """
        base_dict = self.to_dict()
        for key, val in overrides.items():
            parts = key.split(".")
            target = base_dict
            for p in parts[:-1]:
                target = target.setdefault(p, {})
            target[parts[-1]] = val
        return BudgetConfig.from_dict(base_dict, source=self._source_file)


# ── Config loading ──────────────────────────────────────────────


def _find_config_file() -> str:
    """Search for an Omega config file in standard locations.

    Order: 1) OMEGA_CONFIG env var
           2) CWD/omega.toml
           3) CWD/.omega/config.toml
           4) ~/.omega/config.toml
    Returns empty string if none found.
    """
    env_path = os.environ.get("OMEGA_CONFIG", "")
    if env_path and Path(env_path).is_file():
        return env_path

    candidates = [
        Path.cwd() / "omega.toml",
        Path.cwd() / ".omega" / "config.toml",
        Path.home() / ".omega" / "config.toml",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)

    return ""


def _load_dotenv_files() -> None:
    """Load .env files into os.environ using python-dotenv.

    Loading order (later files override earlier ones):
        1. .env (CWD — project-level)
        2. .omega/.env (CWD — project-optional)
        3. ~/.omega/.env (user-level global)

    Silently no-ops if python-dotenv is not installed.
    """
    if load_dotenv is None:
        return

    candidates = [
        Path.cwd() / ".env",
        Path.cwd() / ".omega" / ".env",
        Path.home() / ".omega" / ".env",
    ]
    for path in candidates:
        if path.is_file():
            load_dotenv(str(path), override=False)


def _parse_file(path: str) -> dict[str, Any]:
    """Parse a config file (TOML) into a plain dict."""
    raw = Path(path).read_text(encoding="utf-8")
    return tomllib.loads(raw)


def _load_env_overrides() -> dict[str, Any]:
    """Load OMEGA_* environment variables as dot-path overrides.

    ``OMEGA_BUDGET_MAX_TOKENS=2000000`` -> ``{"budget.max_tokens": 2000000}``
    ``OMEGA_MODELS_FREE='["hf/"]'`` -> ``{"models.free": ["hf/"]}``
    """
    overrides: dict[str, Any] = {}
    prefix = "OMEGA_"
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        # Convert OMEGA_BUDGET__MAX_TOKENS → budget.max_tokens
        # Use double underscore (__) for dot-path separators
        raw_part = key[len(prefix) :].lower()
        dot_key = raw_part.replace("__", ".")  # __ → dot separator
        # Try to parse as JSON for lists/numbers
        try:
            parsed = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            parsed = val
        overrides[dot_key] = parsed
    return overrides


# ── Global config state (similar to hfpclawer's _config_cache) ──


_config_cache: BudgetConfig | None = None


def load_config(
    config_path: str = "",
    cli_overrides: dict[str, Any] | None = None,
    use_cache: bool = True,
) -> BudgetConfig:
    """Load and merge configuration from all sources.

    Args:
        config_path: Explicit path to config file. Empty = auto-discover.
        cli_overrides: Dict of dot-path overrides from CLI flags.
        use_cache: If True, cache and return the same config on repeated calls.

    Loading chain (later overrides earlier):
        1. BudgetConfig() defaults
        2. Config file (omega.toml / .omega/config.toml)
        3. OMEGA_* env vars
        4. CLI overrides
    """
    global _config_cache

    if use_cache and _config_cache is not None and not config_path and not cli_overrides:
        return _config_cache

    # 0. Load .env files (populates os.environ for subsequent steps)
    _load_dotenv_files()

    # 1. Defaults
    cfg = BudgetConfig()

    # 2. Config file
    path = config_path or _find_config_file()
    if path:
        try:
            file_data = _parse_file(path)
            cfg = BudgetConfig.from_dict(file_data, source=path)
        except Exception as exc:
            import warnings

            warnings.warn(f"Failed to load config file {path}: {exc}", stacklevel=2)

    # 3. Env vars
    env_overrides = _load_env_overrides()
    if env_overrides:
        cfg = cfg.merge(env_overrides)

    # 4. CLI flags
    if cli_overrides:
        cfg = cfg.merge(cli_overrides)

    if use_cache:
        _config_cache = cfg

    return cfg


def reset_config_cache() -> None:
    """Clear the global config cache (useful in tests)."""
    global _config_cache
    _config_cache = None


# ── Dot-path getter (compat + convenience) ──────────────────────


def get(key: str, default: Any = None, *, cfg: BudgetConfig | None = None) -> Any:
    """Dot-path access into a BudgetConfig.

    Args:
        key: Dot-separated path, e.g. ``"budget.max_tokens"`` or ``"models.free"``.
        default: Value returned if the path does not exist.
        cfg: BudgetConfig to query. If None, loads default config.

    Returns:
        The value at the given dot-path, or *default* if not found.
    """
    if cfg is None:
        cfg = load_config()

    d = cfg.to_dict()
    parts = key.split(".")
    current: Any = d
    try:
        for part in parts:
            if isinstance(current, dict):
                current = current[part]
            else:
                return default
        return current
    except (KeyError, TypeError):
        return default


# ── Backward-compat alias ───────────────────────────────────────

DEFAULT_BUDGET: dict[str, Any] = dict(BudgetConfig().to_dict())  # static snapshot

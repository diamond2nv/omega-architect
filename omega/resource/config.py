"""Default budget configuration with dot-path access.

Usage:
    >>> from omega.resource.config import DEFAULT_BUDGET, get
    >>> get("budget.max_tokens")
    1000000
    >>> get("models.free")
    ["ollama/", "local/"]
    >>> get("nonexistent.key", 42)
    42
"""

from __future__ import annotations

from typing import Any

DEFAULT_BUDGET: dict[str, Any] = {
    "budget": {
        "max_tokens": 1000000,  # 1M tokens total (prompt+completion)
        "max_cost_usd": 0.50,  # $0.50 per proof attempt (paid providers only)
        "max_time_s": 300.0,  # 5 min wall-clock per theorem
        "max_attempts": 50,  # max T2 compile attempts
        "min_confidence": 0.3,  # min proposer confidence to attempt
        "epochs": {
            "max_epochs": 5,  # max correction rounds
            "convergence_threshold": 0.1,  # min error reduction rate
            "window": 3,  # epochs to compare for convergence
        },
    },
    "models": {
        "free": ["ollama/", "local/"],  # prefixes for free models
        "deepseek/deepseek-chat": {
            "input_per_token": 2.8e-7,  # $0.28/M tokens
            "output_per_token": 4.2e-7,  # $0.42/M tokens
        },
        "anthropic/claude-sonnet-4": {
            "input_per_token": 3.0e-6,  # $3/M tokens
            "output_per_token": 1.5e-5,  # $15/M tokens
        },
        "openrouter/anthropic/claude-sonnet-4": {
            "input_per_token": 3.0e-6,
            "output_per_token": 1.5e-5,
        },
    },
}


def get(key: str, default: Any = None) -> Any:
    """Dot-path access into DEFAULT_BUDGET.

    Args:
        key: Dot-separated path, e.g. "budget.max_tokens" or "models.free".
        default: Value returned if the path does not exist.

    Returns:
        The value at the given dot-path, or *default* if the path is not found.
    """
    parts = key.split(".")
    current: Any = DEFAULT_BUDGET
    try:
        for part in parts:
            if isinstance(current, dict):
                current = current[part]
            else:
                return default
        return current
    except (KeyError, TypeError):
        return default

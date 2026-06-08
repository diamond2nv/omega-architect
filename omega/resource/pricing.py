#!/usr/bin/env python3
"""Pricing awareness and locale detection for Omega ModelRouter.

Derived from OpenSquilla's ``_compute_savings`` / ``prompt_hint_locale``
pattern (Apache-2.0).  Adapted for Omega's theorem-proving model pool.

Copyright 2025 OpenSquilla Authors
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

__all__ = [
    "lookup_price",
    "compute_savings",
    "prompt_hint_locale",
    "select_localized_hint",
]

# ── Pricing registry ───────────────────────────────────────────
# All prices in USD per million input/output tokens.
# Local models have $0 cost.

_MODEL_PRICING: dict[str, dict[str, float]] = {
    # DeepSeek API models (remote, paid)
    "deepseek-v4-flash": {"input_per_mtok": 0.14, "output_per_mtok": 0.28},
    "deepseek-v4-pro": {"input_per_mtok": 0.42, "output_per_mtok": 0.84},
    "deepseek-chat": {"input_per_mtok": 0.14, "output_per_mtok": 0.28},
    # Local models (free)
    "qwen3-coder:30b": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    "deepseek-r1:8b": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    "gemma4:26b": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    "qwen3.6:latest": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    "Goedel-LM/Goedel-Prover-V2-8B": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    "Goedel-LM/Goedel-Prover-V2-32B": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
    # Default local fallback
    "default": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
}

# ── Prompt hints for each policy level (bilingual) ─────────────

_PROMPT_HINTS: dict[str, dict[str, str]] = {
    "P0": {  # Simple — compress, no thinking
        "hint_en": "Answer directly, keep thinking short, avoid irrelevant expansion.",
        "hint_zh": "直接作答，缩短思考长度，避免无关展开。",
    },
    "P1": {  # Medium — standard prompt
        "hint_en": "Provide a thorough reasoning with clear step-by-step justification.",
        "hint_zh": "给出完整的推理过程，清晰的逐步证明。",
    },
    "P2": {  # Hard — full prompt with thinking
        "hint_en": "Think step by step. Use multiple approaches. Verify each step.",
        "hint_zh": "逐步推理。尝试多种方法。验证每一步的正确性。",
    },
}

# ── CJK ranges for locale detection ────────────────────────────

_CJK_RANGES = (
    ("\u4e00", "\u9fff"),
    ("\u3400", "\u4dbf"),
    ("\uf900", "\ufaff"),
)


# ── Public API ─────────────────────────────────────────────────


def lookup_price(
    model_id: str,
) -> dict[str, float]:
    """Look up pricing info for a model ID.

    Parameters
    ----------
    model_id : str
        Full model identifier (e.g. ``"deepseek-v4-flash"``,
        ``"Goedel-LM/Goedel-Prover-V2-8B"``).

    Returns
    -------
    dict
        ``{"input_per_mtok": float, "output_per_mtok": float}``.
        Returns $0/$0 for unknown models.
    """
    # Try exact match first
    if model_id in _MODEL_PRICING:
        return dict(_MODEL_PRICING[model_id])

    # Try prefix match (e.g. "deepseek/deepseek-v4-flash" → "deepseek-v4-flash")
    for known, price in _MODEL_PRICING.items():
        if known in model_id or model_id.endswith(known):
            return dict(price)

    return dict(_MODEL_PRICING["default"])


def compute_savings(
    selected_model: str,
    model_prices: list[tuple[str, float]],
) -> dict:
    """Compute per-turn cost savings relative to the most expensive model.

    Derived from OpenSquilla's ``_compute_savings`` (Apache-2.0).

    Parameters
    ----------
    selected_model : str
        The model actually selected by the router.
    model_prices : list of (model_id, input_price_per_m)
        All potentially-routable models with their input prices.

    Returns
    -------
    dict
        ``{"savings_pct": float, "max_price_per_m": float,
          "routed_price_per_m": float}``.
    """
    prices = [p for _m, p in model_prices]
    max_price = max(prices) if prices else 0.0
    routed_price = lookup_price(selected_model)["input_per_mtok"]
    pct = (
        0.0
        if max_price <= 0 or routed_price >= max_price
        else round((max_price - routed_price) / max_price * 100, 1)
    )
    return {
        "savings_pct": pct,
        "max_price_per_m": max_price,
        "routed_price_per_m": routed_price,
    }


def prompt_hint_locale(text: str | None) -> str:
    """Detect whether the input is primarily Chinese or English.

    Derived from OpenSquilla's ``prompt_hint_locale`` (Apache-2.0).

    Returns ``"zh"`` when the prompt contains 2+ CJK characters,
    ``"en"`` otherwise.
    """
    if not text:
        return "en"
    cjk_count = 0
    latin_count = 0
    for char in text:
        if any(start <= char <= end for start, end in _CJK_RANGES):
            cjk_count += 1
        elif char.isascii() and char.isalpha():
            latin_count += 1
    return "zh" if cjk_count >= 2 else "en"


def select_localized_hint(
    policy: str,
    text: str | None = None,
) -> str | None:
    """Select a bilingual prompt hint for a given policy level.

    Parameters
    ----------
    policy : str
        Policy level (``"P0"``, ``"P1"``, ``"P2"``).
    text : str or None
        Input text to detect locale; ``None`` defaults to English.

    Returns
    -------
    str or None
        The localized hint text, or ``None`` if the policy is unknown.
    """
    cfg = _PROMPT_HINTS.get(policy)
    if not cfg:
        return None
    if prompt_hint_locale(text) == "zh":
        return cfg.get("hint_zh") or cfg.get("hint_en")
    return cfg.get("hint_en") or cfg.get("hint_zh")

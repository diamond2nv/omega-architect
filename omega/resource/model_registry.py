#!/usr/bin/env python3
"""ModelRegistry — 全仓统一模型注册表。

单一真相来源包含：
- 所有模型定义（DeepSeek API、Ollama 本地、Goedel vLLM）
- API 名称解析（去 ``deepseek/`` 前缀）
- 定价（每百万 token）
- 能力标签（用于 Allocator 路由）
- 免费模型检测

所有消费者通过此模块解析模型名称，不再硬编码格式。用法::

    from omega.resource.model_registry import (
        MODELS, resolve_api_name, resolve_tier,
        resolve_pricing, get_capabilities, is_free_model,
    )

    # 解析 API 发送名
    api_name = resolve_api_name("deepseek/deepseek-v4-flash")
    # → "deepseek-v4-flash"  (即去掉 deepseek/ 前缀)

    # 检查是否免费
    free = is_free_model("ollama/gemma4:26b")  # → True
"""

from __future__ import annotations

from typing import Any


# ═══════════════════════════════════════════════════════════════
# 模型注册表（单一真相来源）
# ═══════════════════════════════════════════════════════════════

_ModelDef = dict[str, Any]

MODELS: dict[str, _ModelDef] = {
    # ── DeepSeek 远程 API ────────────────────────────────────
    "deepseek-v4-flash": {
        "api_name": "deepseek-v4-flash",
        "provider": "deepseek",
        "tier": "remote",
        "base_url": "https://api.deepseek.com",
        "pricing": {
            "input_per_mtok": 0.14,
            "output_per_mtok": 0.28,
        },
        "capabilities": ["continuation", "append", "bulk", "fast", "flash"],
        "default": True,
    },
    "deepseek-v4-pro": {
        "api_name": "deepseek-v4-pro",
        "provider": "deepseek",
        "tier": "remote",
        "base_url": "https://api.deepseek.com",
        "pricing": {
            "input_per_mtok": 0.14,
            "output_per_mtok": 0.28,
        },
        "capabilities": ["strategy", "blueprint", "hard", "proof", "deep_reasoning"],
        "default": False,
    },
    # Alias: old deepseek-chat → maps to v4-pro
    "deepseek-chat": {
        "api_name": "deepseek-chat",
        "provider": "deepseek",
        "tier": "remote",
        "base_url": "https://api.deepseek.com",
        "pricing": {
            "input_per_mtok": 0.14,
            "output_per_mtok": 0.28,
        },
        "capabilities": ["strategy", "blueprint", "hard", "proof"],
        "deprecated": True,
    },

    # ── Goedel-Prover-V2 本地 vLLM ───────────────────────────
    "goedel-v2-8b": {
        "api_name": "Goedel-LM/Goedel-Prover-V2-8B",
        "provider": "goedel",
        "tier": "local",
        "base_url": "http://localhost:8001/v1",
        "pricing": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
        "capabilities": ["lean_proof", "fast"],
    },

    # ── Ollama 本地模型 ──────────────────────────────────────
    "ollama/qwen3-coder:30b": {
        "api_name": "qwen3-coder:30b",
        "provider": "ollama",
        "tier": "local",
        "base_url": "http://172.26.160.1:11434",
        "pricing": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
        "capabilities": ["code", "lemma_search", "proof"],
        "default_local": True,
    },
    "ollama/gemma4:26b": {
        "api_name": "gemma4:26b",
        "provider": "ollama",
        "tier": "local",
        "base_url": "http://172.26.160.1:11434",
        "pricing": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
        "capabilities": ["fast", "easy", "routine"],
    },
    "ollama/deepseek-r1:8b": {
        "api_name": "deepseek-r1:8b",
        "provider": "ollama",
        "tier": "local",
        "base_url": "http://172.26.160.1:11434",
        "pricing": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
        "capabilities": ["reasoning", "exploration"],
    },
    "ollama/qwen3.6:latest": {
        "api_name": "qwen3.6:latest",
        "provider": "ollama",
        "tier": "local",
        "base_url": "http://172.26.160.1:11434",
        "pricing": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
        "capabilities": ["deep_reasoning"],
    },
}

# ── 前缀路由表 ───────────────────────────────────────────────
# 用于 resolve_generate_fn 的分派
PREFIX_ROUTES: dict[str, str] = {
    "deepseek/": "deepseek",
    "goedel/": "goedel",
    "ollama/": "ollama",
    "local/": "local",
}


# ═══════════════════════════════════════════════════════════════
# 解析函数
# ═══════════════════════════════════════════════════════════════


def _strip_prefix(model_id: str) -> str:
    """去掉常见前缀，返回裸模型名。

    Example::

        >>> _strip_prefix("deepseek/deepseek-v4-flash")
        'deepseek-v4-flash'
        >>> _strip_prefix("goedel/goedel-v2-8b")
        'goedel-v2-8b'
        >>> _strip_prefix("ollama/qwen3-coder:30b")
        'qwen3-coder:30b'
        >>> _strip_prefix("deepseek-v4-pro")  # no prefix — no change
        'deepseek-v4-pro'
    """
    for prefix in PREFIX_ROUTES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def _lookup(model_id: str) -> _ModelDef | None:
    """查找模型定义，自动尝试前缀剥离。

    查找顺序:
    1. 原 model_id 直接匹配
    2. 前缀剥离后匹配
    3. 对 ``deepseek/deepseek-v4-flash`` 格式（双 deepseek）特殊处理
    """
    # 1. Exact match
    if model_id in MODELS:
        return MODELS[model_id]

    # 2. Strip prefix and try
    stripped = _strip_prefix(model_id)
    if stripped in MODELS:
        return MODELS[stripped]

    # 3. Handle deepseek/deepseek-* double prefix
    #    e.g. "deepseek/deepseek-v4-flash" → strip → "deepseek-v4-flash" already tried above
    #    This handles the case where the prefix IS the name
    if model_id.startswith("deepseek/"):
        # Try direct: "deepseek/deepseek-v4-flash" → split → "deepseek-v4-flash"
        after_prefix = model_id[len("deepseek/"):]
        if after_prefix in MODELS:
            return MODELS[after_prefix]

    return None


def resolve_api_name(model_id: str) -> str:
    """返回发送给 API 的模型名称（无 ``deepseek/`` 等前缀）。

    Examples::

        >>> resolve_api_name("deepseek/deepseek-v4-flash")
        'deepseek-v4-flash'
        >>> resolve_api_name("deepseek-v4-pro")
        'deepseek-v4-pro'
        >>> resolve_api_name("ollama/qwen3-coder:30b")
        'qwen3-coder:30b'
        >>> resolve_api_name("goedel/goedel-v2-8b")
        'Goedel-LM/Goedel-Prover-V2-8B'
    """
    entry = _lookup(model_id)
    if entry:
        return entry["api_name"]
    # Fallback: just strip prefix
    return _strip_prefix(model_id)


def resolve_tier(model_id: str) -> str:
    """返回 'local' 或 'remote'。"""
    entry = _lookup(model_id)
    if entry:
        return entry["tier"]
    # Heuristic: Ollama/Goedel = local, DeepSeek = remote
    mid = model_id.lower()
    if any(mid.startswith(p) for p in ("ollama/", "local/", "goedel/")):
        return "local"
    return "remote"


def resolve_pricing(model_id: str) -> dict[str, float]:
    """返回 ``{input_per_token, output_per_token}`` 定价。"""
    entry = _lookup(model_id)
    if entry and "pricing" in entry:
        p = entry["pricing"]
        return {
            "input_per_token": p.get("input_per_mtok", 0.0) / 1_000_000,
            "output_per_token": p.get("output_per_mtok", 0.0) / 1_000_000,
        }
    return {"input_per_token": 0.0, "output_per_token": 0.0}


def resolve_per_mtok_pricing(model_id: str) -> dict[str, float]:
    """返回每百万 token 定价 ``{input_per_mtok, output_per_mtok}``。"""
    entry = _lookup(model_id)
    if entry and "pricing" in entry:
        return dict(entry["pricing"])
    return {"input_per_mtok": 0.0, "output_per_mtok": 0.0}


def get_capabilities(model_id: str) -> list[str]:
    """返回模型能力标签列表。"""
    entry = _lookup(model_id)
    if entry:
        return list(entry.get("capabilities", []))
    return ["general"]


def get_base_url(model_id: str) -> str:
    """返回 API base URL。"""
    entry = _lookup(model_id)
    if entry:
        return entry.get("base_url", "")
    return ""


def is_free_model(model_id: str) -> bool:
    """检查是否是免费（本地）模型。

    本地模型（Ollama、Goedel）返回 True，远程 API 返回 False。
    """
    return resolve_tier(model_id) == "local"


def is_deepseek_model(model_id: str) -> bool:
    """检查是否是 DeepSeek API 模型。"""
    entry = _lookup(model_id)
    if entry:
        return entry["provider"] == "deepseek"
    return model_id.lower().startswith("deepseek/")


def resolve_provider(model_id: str) -> str:
    """返回 provider 名称：'deepseek', 'ollama', 'goedel'。"""
    entry = _lookup(model_id)
    if entry:
        return entry["provider"]
    mid = model_id.lower()
    if mid.startswith("deepseek/"):
        return "deepseek"
    if mid.startswith("goedel/"):
        return "goedel"
    if mid.startswith(("ollama/", "local/")):
        return "ollama"
    return "unknown"


def list_models(tier: str | None = None, provider: str | None = None,
                capability: str | None = None) -> list[str]:
    """列出符合条件的模型 ID。

    Args:
        tier: 'local' 或 'remote'
        provider: 'deepseek', 'ollama', 'goedel'
        capability: 能力标签（如 'flash', 'proof', 'blueprint'）

    Returns:
        匹配的 model_id 列表（注册表原始 key）
    """
    results = []
    for mid, entry in MODELS.items():
        if tier and entry.get("tier") != tier:
            continue
        if provider and entry.get("provider") != provider:
            continue
        if capability and capability not in entry.get("capabilities", []):
            continue
        results.append(mid)
    return results


# ═══════════════════════════════════════════════════════════════
# 兼容函数（替换旧的硬编码判断）
# ═══════════════════════════════════════════════════════════════

# 旧版 BudgetTracker/BudgetConfig 用的免费模型前缀（保持向后兼容）
DEFAULT_FREE_PREFIXES: list[str] = ["ollama/", "local/", "goedel/"]


def model_in_free_list(model_id: str, free_prefixes: list[str] | None = None) -> bool:
    """检查 model_id 是否匹配免费前缀列表。

    这是 BudgetTracker.is_free_model 的底层函数，保持向后兼容。
    """
    prefixes = free_prefixes or DEFAULT_FREE_PREFIXES
    return any(model_id.startswith(p) for p in prefixes)

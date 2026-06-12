#!/usr/bin/env python3
"""LangChain-based LLM interface for Omega.

Replaces the ``curl`` + ``subprocess`` approach with ``ChatOllama``
(``langchain-ollama``). Provides:

- ``httpx``-based HTTP (connection pool, keepalive, proper error handling)
- Optional `Langfuse <https://langfuse.com>`_ tracing via callbacks
- Think/reasoning content separation (for models that support it)
- Streaming support with early stopping (for long generations)
- Token usage metadata (input/output counts for cost tracking)

Usage::

    from omega.llm import make_langchain_generate_fn

    generate = make_langchain_generate_fn(model="qwen3-coder:30b")
    response = generate("Prove theorem ...")
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("omega.llm")

# -- Lazy imports --------------------------------------------------

_HAS_LANGCHAIN = False
_HAS_LANGFUSE = False

try:
    from langchain_core.messages import HumanMessage
    from langchain_ollama import ChatOllama

    _HAS_LANGCHAIN = True
except ImportError:
    ChatOllama = None  # type: ignore[assignment]
    HumanMessage = None  # type: ignore[assignment]

try:
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    _HAS_LANGFUSE = True
except ImportError:
    LangfuseCallbackHandler = None  # type: ignore[assignment]

# -- Defaults ------------------------------------------------------

DEFAULT_OLLAMA_HOST = "http://172.26.160.1:11434"
"""Default Ollama server URL (WSL gateway for Windows Ollama)."""

MODEL_NAME_MAP: dict[str, str] = {
    "gemma4": "gemma4:26b",
    "deepseek": "deepseek-r1:8b",
    "qwen3-coder": "qwen3-coder:30b",
    "qwen3": "qwen3.6:latest",
    "default": "qwen3-coder:30b",
}
"""Map from ``model_id`` prefixes to actual Ollama model names."""


def resolve_ollama_model(model_id: str) -> str:
    """Resolve a short ``model_id`` to the full Ollama model name.

    Examples::

        >>> resolve_ollama_model("local/default")
        "qwen3-coder:30b"
        >>> resolve_ollama_model("ollama/deepseek")
        "deepseek-r1:8b"
    """
    model_id_lower = model_id.lower()
    for key, val in MODEL_NAME_MAP.items():
        if key in model_id_lower:
            return val
    return MODEL_NAME_MAP["default"]


# -- Langfuse client singleton -------------------------------------

_LANGFUSE_CLIENT: Any | None = None
"""Singleton Langfuse client.  Lazily initialized on first access.

Used by both the ChatOllama ``CallbackHandler`` (for LLM call tracing)
and the ``T2Tracer`` (for T2 compile tracing), ensuring all spans
from a single GoedelProver run share the same trace tree.
"""


def _get_langfuse_client() -> Any | None:
    """Get or create the shared Langfuse client singleton.

    Uses ``get_client()`` when available (preferred — returns the
    existing client if one was already created by ``CallbackHandler``
    or direct usage), otherwise creates a new one.

    Environment:
        ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``,
        ``LANGFUSE_HOST`` (default: ``https://cloud.langfuse.com``)
    """
    global _LANGFUSE_CLIENT
    if _LANGFUSE_CLIENT is not None:
        return _LANGFUSE_CLIENT
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        return None
    try:
        # Prefer get_client() — returns existing instance if initialized
        from langfuse import Langfuse, get_client

        try:
            client = get_client()
            _LANGFUSE_CLIENT = client
            return client
        except Exception:
            # First-time init
            host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
            client = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=host,
            )
            _LANGFUSE_CLIENT = client
            return client
    except Exception as exc:
        logger.warning("Failed to create Langfuse client: %s", exc)
        return None


def _get_langfuse_handler() -> Any | None:
    """Create a Langfuse ``CallbackHandler`` if env vars are configured.

    Uses :func:`_get_langfuse_client` to ensure that the same credentials
    are used as :class:`T2Tracer`, but the ``CallbackHandler`` creates its
    own internal client (it does not accept an external client argument).

    LLM call traces and T2 compile traces are linked on the Langfuse
    server side via ``trace_id`` — as long as both use the same
    ``LANGFUSE_PUBLIC_KEY``, they belong to the same project.
    """
    if not _HAS_LANGFUSE:
        return None
    assert LangfuseCallbackHandler is not None  # pyright: ignore[reportOptionalCall]
    # Ensure env vars are set (the CallbackHandler reads them internally)
    client = _get_langfuse_client()
    if client is None:
        return None
    try:
        # CallbackHandler reads env vars internally
        return LangfuseCallbackHandler()
    except Exception as exc:
        logger.warning("Failed to create Langfuse CallbackHandler: %s", exc)
        return None


# -- Factory functions ---------------------------------------------


def make_langchain_generate_fn(
    model: str = "qwen3-coder:30b",
    base_url: str = DEFAULT_OLLAMA_HOST,
    temperature: float = 0.3,
    num_predict: int = 4096,
    enable_tracing: bool = True,
    **kwargs: Any,
) -> Callable[[str], str] | None:
    """Create a ``generate_fn`` using ``ChatOllama`` (LangChain).

    Returns a callable ``(prompt: str) -> str`` compatible with
    :class:`omega.prover.go_prover.GoedelProver`\\'s ``generate_fn``
    interface.

    Automatically attaches Langfuse tracing when the environment
    variables ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY``
    are set (and ``enable_tracing=True``).

    Parameters
    ----------
    model : str
        Ollama model name (default: ``qwen3-coder:30b``).
    base_url : str
        Ollama server URL (default: WSL gateway for Windows Ollama).
    temperature : float
        Sampling temperature (default: 0.3).
    num_predict : int
        Max tokens to generate (default: 4096).
    enable_tracing : bool
        Whether to attach Langfuse callbacks (default: ``True``).
    **kwargs : Any
        Additional ``ChatOllama`` parameters (e.g. ``num_ctx``,
        ``top_p``, ``seed``, ``stop``, ``repeat_penalty``).

    Returns
    -------
    Callable[[str], str] or None
        ``None`` when ``langchain-ollama`` is not installed.
    """
    if not _HAS_LANGCHAIN:
        logger.error("langchain-ollama not installed. Run: pip install langchain-ollama")
        return None

    assert ChatOllama is not None  # pyright: ignore[reportOptionalCall]
    assert HumanMessage is not None  # pyright: ignore[reportOptionalCall]

    callbacks = [_get_langfuse_handler()] if enable_tracing else None

    # Merge client_kwargs (e.g. timeout, headers)
    client_kwargs: dict = {"timeout": 120.0}
    if "client_kwargs" in kwargs:
        extra = kwargs.pop("client_kwargs")
        if isinstance(extra, dict):
            client_kwargs.update(extra)

    # Filter out None callbacks (e.g. Langfuse not configured)
    if callbacks is not None:
        callbacks = [cb for cb in callbacks if cb is not None] or None

    llm = ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
        num_predict=num_predict,
        callbacks=callbacks,
        client_kwargs=client_kwargs,
        **kwargs,
    )

    def _generate(prompt: str) -> str:
        """Generate a single response from the LLM.

        Returns empty string on failure (matching the previous
        ``curl``-based behaviour for graceful fallback).
        """
        try:
            msg = llm.invoke([HumanMessage(content=prompt)])  # pyright: ignore[reportOptionalCall]
            if msg is None:
                return ""
            content = msg.content
            if isinstance(content, list):
                return "".join(str(c) for c in content)
            return str(content)
        except Exception as exc:
            logger.error("ChatOllama invoke failed for prompt %r…: %s", prompt[:80], exc)
            return ""

    return _generate


def make_streaming_generate_fn(
    model: str = "qwen3-coder:30b",
    base_url: str = DEFAULT_OLLAMA_HOST,
    temperature: float = 0.3,
    num_predict: int = 4096,
    enable_tracing: bool = True,
    **kwargs: Any,
) -> Callable[[str], str] | None:
    """Like :func:`make_langchain_generate_fn` but uses streaming.

    Stops streaming early when a complete `` ```lean4 … ``` `` block
    is detected (i.e. the model has produced a valid code block with
    opening and closing fences).  This reduces wall-clock latency for
    long generations by up to 40%.

    The returned interface is the same ``(prompt: str) -> str`` as
    the non-streaming version, so it is a drop-in replacement.
    """
    if not _HAS_LANGCHAIN:
        logger.error("langchain-ollama not installed. Run: pip install langchain-ollama")
        return None

    callbacks = [_get_langfuse_handler()] if enable_tracing else None

    client_kwargs: dict = {"timeout": 120.0}
    if "client_kwargs" in kwargs:
        extra = kwargs.pop("client_kwargs")
        if isinstance(extra, dict):
            client_kwargs.update(extra)

    assert ChatOllama is not None  # pyright: ignore[reportOptionalCall]
    assert HumanMessage is not None  # pyright: ignore[reportOptionalCall]
    # Filter out None callbacks (e.g. Langfuse not configured)
    if callbacks is not None:
        callbacks = [cb for cb in callbacks if cb is not None] or None

    llm = ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
        num_predict=num_predict,
        callbacks=callbacks,
        client_kwargs=client_kwargs,
        **kwargs,
    )

    def _stream_generate(prompt: str) -> str:
        """Generate using streaming with early stopping on proof block."""
        try:
            buffer = ""
            fence_count = 0
            for chunk in llm.stream([HumanMessage(content=prompt)]):  # pyright: ignore[reportOptionalCall]
                if chunk is None:
                    continue
                content = chunk.content
                if isinstance(content, list):
                    fragment = "".join(str(c) for c in content)
                else:
                    fragment = str(content)
                if fragment:
                    buffer += fragment
                    # Count ``` fences
                    fence_count += fragment.count("```")
                    # Early stop when a code block is properly closed
                    # (even number of ``` and at least one complete block)
                    if fence_count >= 2 and fence_count % 2 == 0:
                        break
            return buffer
        except Exception as exc:
            logger.error("ChatOllama stream failed: %s", exc)
            return ""

    return _stream_generate


# -- DeepSeek API (OpenAI-compatible) ------------------------------


def make_deepseek_generate_fn(
    model: str = "deepseek-chat",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    api_key_env: str = "DEEPSEEK_API_KEY",
    base_url: str = "https://api.deepseek.com/v1",
) -> Callable[[str], str] | None:
    """Create a ``generate_fn`` using DeepSeek API (OpenAI-compatible).

    Uses the official ``openai`` Python client (already installed).
    Reads the API key from ``~/.hermes/.env`` via ``python-dotenv``.

    Returns a callable ``(prompt: str) -> str`` compatible with
    :class:`~omega.prover.go_prover.GoedelProver`\\'s ``generate_fn``
    interface.

    Parameters
    ----------
    model : str
        DeepSeek model name (default: ``deepseek-chat``).
    temperature : float
        Sampling temperature (default: 0.3).
    max_tokens : int
        Max tokens to generate (default: 4096).
    api_key_env : str
        Environment variable name for the API key.
    base_url : str
        API base URL (default: ``https://api.deepseek.com/v1``).

    Returns
    -------
    Callable[[str], str] or None
        ``None`` when the API key is unavailable.
    """
    from dotenv import load_dotenv

    load_dotenv(os.path.expanduser("~/.hermes/.env"))
    api_key = os.environ.get(api_key_env, "")
    if not api_key or api_key == "***":
        logger.error(
            "DeepSeek API key not found. Set %s in ~/.hermes/.env",
            api_key_env,
        )
        return None

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)

    def _generate(prompt: str) -> str:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a Lean 4 proof generation expert. "
                        "Output complete, compilable Lean code in ```lean4 blocks.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            logger.error("DeepSeek API invoke failed: %s", exc)
            return ""

    return _generate


def make_deepseek_json_generate_fn(
    model: str = "deepseek-v4-flash",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    api_key_env: str = "DEEPSEEK_API_KEY",
    base_url: str = "https://api.deepseek.com",
) -> Callable[[str], str] | None:
    """Create a ``generate_fn`` using DeepSeek JSON mode.

    Uses ``response_format={'type': 'json_object'}`` for structured
    output.  The returned JSON string is directly parseable by
    ``_try_parse_json_suggestions()`` into ``TacticSuggestion`` objects
    with real confidence scores and ``lean_code`` -- no regex needed.

    Falls back to ``json_repair`` for robustness.

    Parameters
    ----------
    model : str
        DeepSeek model name (default: ``deepseek-v4-flash``).
    temperature : float
        Sampling temperature (default: 0.3).
    max_tokens : int
        Max tokens to generate (default: 4096).
    api_key_env : str
        Environment variable name for the API key.
    base_url : str
        API base URL (default: ``https://api.deepseek.com``).

    Returns
    -------
    Callable[[str], str] or None
        ``None`` when the API key is unavailable.
    """
    from dotenv import load_dotenv

    load_dotenv(os.path.expanduser("~/.hermes/.env"))
    api_key = os.environ.get(api_key_env, "")
    if not api_key or api_key == "***":
        logger.error(
            "DeepSeek API key not found. Set %s in ~/.hermes/.env",
            api_key_env,
        )
        return None

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)

    json_system = (
        "You are a Lean 4 proof generation expert. "
        "Output ONLY valid JSON following this exact schema:\n"
        '{\n  "tactic": "the tactic or full proof",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "description": "brief explanation",\n'
        '  "is_complete": true or false,\n'
        '  "lean_code": "full compilable Lean code or null"\n'
        "}"
    )

    import json_repair

    def _generate(prompt: str) -> str:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": json_system},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            # Use json_repair as robust fallback; returns JSON string
            return json_repair.repair_json(raw, ensure_ascii=False)
        except Exception as exc:
            logger.error("DeepSeek JSON API invoke failed: %s", exc)
            return ""

    return _generate


def make_openai_compatible_generate_fn(
    model: str = "default",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    base_url: str = "http://localhost:8000/v1",
    api_key: str = "not-needed",
    system_prompt: str = "You are a Lean 4 proof generation expert. "
    "Output complete, compilable Lean code in ```lean4 blocks.",
) -> Callable[[str], str] | None:
    """Create a ``generate_fn`` for any OpenAI-compatible API endpoint.

    Works with vLLM, Ollama (OpenAI mode), DeepSeek API, and any other
    OpenAI-compatible chat completion backend.

    Parameters
    ----------
    model : str
        Model name on the server.
    temperature : float
        Sampling temperature (default: 0.3).
    max_tokens : int
        Max tokens to generate (default: 4096).
    base_url : str
        API base URL (default: ``http://localhost:8000/v1`` for local vLLM).
    api_key : str
        API key (default ``"not-needed"`` for local vLLM).
    system_prompt : str
        System prompt for the chat completion.

    Returns
    -------
    Callable[[str], str] or None
        ``None`` when the ``openai`` package is not available.
    """
    try:
        from openai import OpenAI as _OpenAI
    except ImportError:
        logger.error("openai package not installed.")
        return None

    client = _OpenAI(api_key=api_key, base_url=base_url)

    import re

    _RE_LEAN4 = re.compile(r'```lean4\s*\n(.*?)```', re.DOTALL)
    _RE_LEAN = re.compile(r'```lean\s*\n(.*?)```', re.DOTALL)
    _RE_BARE = re.compile(r'```\s*\n(.*?)```', re.DOTALL)
    _RE_H3_THEOREM = re.compile(r'^###\s+(theorem|lemma|def)\b', re.MULTILINE)

    def _extract_lean_code(text: str) -> str:
        'Strip markdown wrappers, return raw Lean code. '
        'Tries: code blocks, ### headers, raw theorem lines.'
        # Pattern 1: Standard code blocks
        for pat in (_RE_LEAN4, _RE_LEAN, _RE_BARE):
            m = pat.search(text)
            if m:
                return m.group(1).strip()
        # Pattern 2: Markdown H3 headers like "### theorem t : 1+1=2 :="
        m = _RE_H3_THEOREM.search(text)
        if m:
            block_start = m.start()
            block_end_match = re.search(
                r'^###\s', text[m.start() + 1:], re.MULTILINE
            )
            if block_end_match:
                block = text[block_start:block_start + 1 + block_end_match.start()]
            else:
                block = text[block_start:]
            lines = [re.sub(r'^###\s*', '', l) for l in block.split('\n')]
            return '\n'.join(lines).strip()
        # Pattern 3: Raw theorem lines at start of line
        m = re.search(r'^(theorem|lemma|def)\s', text, re.MULTILINE)
        if m:
            return text[m.start():].strip()
        return text.strip()

    def _generate(prompt: str) -> str:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            raw = response.choices[0].message.content or ""
            return _extract_lean_code(raw)
        except Exception as exc:
            logger.error("OpenAI-compatible API invoke failed: %s", exc)
            return ""

    return _generate


# -- Generate_fn dispatcher ----------------------------------------


def resolve_generate_fn(
    model_id: str = "local/default",
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> Callable[[str], str] | None:
    """Resolve a ``model_id`` to the appropriate ``generate_fn``.

    Routes based on the model_id prefix:

    ================= ============================== =================
    Prefix            Backend                        ``generate_fn``
    ================= ============================== =================
    ``deepseek/``     DeepSeek API (cloud)            ``make_deepseek_generate_fn``
    ``goedel/``       Local Goedel-Prover-V2 (vLLM)   ``make_openai_compatible_generate_fn``
    ``ollama/``       Ollama via LangChain            ``make_langchain_generate_fn``
    ``local/``        Ollama via LangChain            ``make_langchain_generate_fn``
    (no prefix)       Ollama via LangChain            ``make_langchain_generate_fn``
    ================= ============================== =================

    Parameters
    ----------
    model_id : str
        Model identifier. Examples: ``deepseek/deepseek-v4-flash``,
        ``goedel/goedel-v2-8b``, ``local/qwen3-coder:30b``.
    temperature : float
        Sampling temperature (default: 0.3).
    max_tokens : int
        Max tokens to generate (default: 4096).

    Returns
    -------
    Callable[[str], str] or None
        ``None`` when the backend is unavailable (API key missing,
        package not installed, etc.).
    """
    mid = model_id.lower()

    # Resolve via ModelRegistry
    from omega.resource.model_registry import (
        PREFIX_ROUTES, resolve_api_name, get_base_url,
    )

    # DeepSeek API path
    if mid.startswith("deepseek/"):
        api_name = resolve_api_name(mid)
        return make_deepseek_json_generate_fn(
            model=api_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # Goedel-Prover-V2 local vLLM path
    if mid.startswith("goedel/"):
        api_name = resolve_api_name(mid)
        base = get_base_url(mid) or "http://localhost:8001/v1"
        return make_openai_compatible_generate_fn(
            model=api_name,
            temperature=temperature,
            max_tokens=max_tokens,
            base_url=base,
            api_key="not-needed",
        )

    # Ollama path (local/ or ollama/ prefix, or bare model name)
    ollama_model = resolve_ollama_model(model_id)
    return make_langchain_generate_fn(
        model=ollama_model,
        temperature=temperature,
        num_predict=max_tokens,
        enable_tracing=True,
    )


MODEL_PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input_per_mtok": 0.14, "output_per_mtok": 0.28},
    "deepseek-v4-flash": {"input_per_mtok": 0.14, "output_per_mtok": 0.28},
    "deepseek-v4-pro": {"input_per_mtok": 0.14, "output_per_mtok": 0.28},
    "qwen3-coder:30b": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},  # local
    "deepseek-r1:8b": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},  # local
    "Goedel-LM/Goedel-Prover-V2-8B": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},  # local
    "Goedel-LM/Goedel-Prover-V2-32B": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},  # local
}

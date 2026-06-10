"""Message history compressor for Inner Loop.

Prevents unbounded token growth in multi-round tool_calls conversations.
Strategy: keep recent reasoning, discard old reasoning, keep all tool results.

Design decision (from critique #1):
- Every 3 rounds: drop reasoning_content from rounds older than last 2
- Always keep: tool_call results, compile errors, system prompt
- Target: keep context ≤ 8K tokens for most rounds
"""

from __future__ import annotations

from typing import Any


def compress_messages(messages: list[dict], keep_last_n: int = 4) -> list[dict]:
    """Compress message history to control token growth.
    
    Strategy:
    - System prompt: always keep (first message)
    - Last `keep_last_n` messages: keep full content (including reasoning)
    - Older messages: keep content + tool_calls, strip reasoning_content
    - Tool result messages: always keep (they carry compile errors / search results)
    
    Parameters
    ----------
    messages : list[dict]
        Full message history (OpenAI format).
    keep_last_n : int
        Number of recent messages to keep full (default 4).
    
    Returns
    -------
    list[dict]
        Compressed messages.
    """
    if not messages:
        return messages
    
    compressed: list[Any] = []
    
    # Always keep system prompt as-is
    compressed.append(messages[0])
    
    # Determine which messages to keep full
    recent_start = max(1, len(messages) - keep_last_n)
    
    for i in range(1, len(messages)):
        msg = dict(messages[i])  # copy
        role = msg.get("role", "")
        
        # Tool messages always kept as-is
        if role == "tool":
            compressed.append(msg)
            continue
        
        # Recent messages: keep full
        if i >= recent_start:
            compressed.append(msg)
            continue
        
        # Older messages: strip reasoning_content
        if "reasoning_content" in msg:
            del msg["reasoning_content"]
        compressed.append(msg)
    
    return compressed


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate for message list.
    
    Used for budget tracking, not exact counting.
    ~4 chars per token for English/Lean code.
    """
    total_chars = 0
    for msg in messages:
        for key in ("content", "reasoning_content"):
            val = msg.get(key) or ""
            total_chars += len(val)
        tc = msg.get("tool_calls")
        if tc:
            for t in tc:
                total_chars += len(t.function.arguments) if hasattr(t.function, "arguments") else 0
    return total_chars // 4

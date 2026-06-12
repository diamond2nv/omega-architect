#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM-based memory processor — compresses proof attempt history into actionable lessons.

Replaces pure truncation (compress_messages) with intelligent summarization.
Every ``interval`` rounds, extracts key lessons from the last N attempts
and injects a compact summary back into the context.

Design:
- Lightweight: single static method, no separate LLM client
- Reuses the existing DeepSeek client instance from inner_loop
- Only fires every ``interval`` rounds (default 3)
- Summarizes: what was tried → what failed → what to try next
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("omega.loop.memory")


def extract_attempts(
    messages: list[dict],
    max_attempts: int = 3,
) -> list[dict]:
    """Extract the last N (proposal → feedback) pairs from message history.

    Looks for assistant messages containing Lean code (the proposal)
    followed by user messages containing compile feedback.

    Returns:
        List of dicts with keys: "round", "code", "reasoning", "error", "fix"
    """
    attempts: list[dict] = []
    current_code = None
    current_reasoning = None

    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        reasoning = msg.get("reasoning_content", "") or ""

        if role == "assistant" and ("```lean4" in content or "```lean" in content):
            # Found a proposal
            current_code = _extract_code_block(content)
            current_reasoning = reasoning or content[:300]
            if current_code:
                attempts.append({
                    "round": len(attempts) + 1,
                    "code": current_code[:500],
                    "reasoning": current_reasoning[:400],
                    "error": "",
                    "fix": "",
                })
            if len(attempts) >= max_attempts:
                break

    # Reverse so oldest-first
    attempts.reverse()

    # Now pair proposals with feedback by looking forward
    # (messages are already in chronological order for this pass)
    pending_round = 0
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if role == "assistant" and ("```lean4" in content or "```lean" in content):
            pending_round += 1
        elif role == "user" and pending_round > 0 and pending_round <= len(attempts):
            idx = pending_round - 1
            # Check if it's feedback (contains compile error or strategy hint)
            if any(marker in content for marker in ["Compilation failed", "Adaptive strategy", "STOP SEARCHING"]):
                lines = content.split("\n")
                # First few lines are the error summary
                error_lines = [l for l in lines if l.strip() and not l.startswith("Adaptive")]
                error_text = "\n".join(error_lines[:3])
                attempts[idx]["error"] = error_text[:300]

    return attempts


def summarize_lessons(
    attempts: list[dict],
    theorem_name: str = "",
) -> str:
    """Format attempt history into a compact lessons-learned block.

    This is designed to be injected as a user message, not an LLM call.
    The formatting itself provides structure that helps the model learn
    from past attempts without incurring extra API cost.

    Returns:
        Formatted lessons block (empty string if no attempts).
    """
    if not attempts:
        return ""

    lines = ["[Ω Memory: Previous Attempts Summary]"]
    for i, a in enumerate(attempts):
        lines.append(f"\nAttempt {i + 1}:")
        if a.get("reasoning"):
            lines.append(f"  Approach: {a['reasoning'][:200]}")
        if a.get("error"):
            lines.append(f"  Issue: {a['error'][:200]}")

    # Extract common error patterns
    errors_seen = set()
    for a in attempts:
        e = a.get("error", "")
        if "type mismatch" in e.lower():
            errors_seen.add("type_mismatch")
        elif "unknown identifier" in e.lower() or "unknown constant" in e.lower():
            errors_seen.add("unknown_identifier")
        elif "syntax" in e.lower():
            errors_seen.add("syntax")
        elif "unexpected token" in e.lower():
            errors_seen.add("syntax")

    if errors_seen:
        lines.append(f"\n  Error patterns: {', '.join(sorted(errors_seen))}")

    # Don't propose solutions — let the model draw its own conclusions
    lines.append("\n[End]")

    return "\n".join(lines)


def _extract_code_block(content: str) -> str | None:
    """Extract the first Lean code block from assistant content."""
    import re
    m = re.search(r"```(?:lean4|lean)\s*\n(.*?)```", content, re.DOTALL)
    return m.group(1).strip() if m else None

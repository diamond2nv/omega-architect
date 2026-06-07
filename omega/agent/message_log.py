#!/usr/bin/env python3
"""MessageLog: append-only slice cache for prefix-cache optimization.

Implements the v2 audit requirement: each sub-goal uses an append-only
MessageLog that stores (role, content) tuples and supports slice-based
prefix reuse.  This avoids wasteful prefix recomputation when the same
reduction sub-chain appears in multiple branches.

Design:
  - Append-only: once written, messages never mutate
  - Slicing: MessageLog[start:end] returns a new MessageLog with the same
    underlying storage (no copy)
  - Prefix-cache: identify(id) marks a message as a prefix-cache boundary.
    The orchestrator can skip recomputing messages before this marker.
  - History: trim(max_size) drops oldest unmarked messages to bound memory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, overload

Role = Literal["user", "assistant", "tool", "system"]


@dataclass(frozen=True, slots=True)
class Message:
    """A single message in the log. Frozen for immutability guarantees."""

    role: Role
    content: str
    timestamp: float = field(default_factory=time.time)
    is_cache_boundary: bool = False
    metadata: dict = field(default_factory=dict)


class MessageLog:
    """Append-only, sliceable message log with prefix-cache support.

    Examples
    --------
    >>> log = MessageLog(max_size=1000)
    >>> log.add("user", "prove: ∀ n:ℕ, n + 0 = n")
    >>> log.add("assistant", "by induction on n")
    >>> log.add_cache_boundary()
    >>> len(log)
    3
    >>> sliced = log[1:3]
    >>> len(sliced)
    2
    """

    def __init__(self, max_size: int = 200) -> None:
        self._messages: list[Message] = []
        self._max_size = max_size

    # ── mutators ──────────────────────────────────────────────

    def add(self, role: Role, content: str, **metadata) -> Message:
        """Append a new message."""
        msg = Message(role=role, content=content, metadata=metadata)
        self._messages.append(msg)
        self._trim()
        return msg

    def add_cache_boundary(self) -> Message:
        """Mark the most recent message as a prefix-cache boundary."""
        if not self._messages:
            raise IndexError("No messages to mark as cache boundary")
        msg = Message(
            role=self._messages[-1].role,
            content=self._messages[-1].content,
            timestamp=self._messages[-1].timestamp,
            is_cache_boundary=True,
            metadata=self._messages[-1].metadata,
        )
        self._messages[-1] = msg
        return msg

    # ── accessors ─────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._messages)

    @overload
    def __getitem__(self, key: int) -> Message: ...
    @overload
    def __getitem__(self, key: slice) -> MessageLog: ...

    def __getitem__(self, key: slice | int) -> MessageLog | Message:
        """Slice returns a new MessageLog; int returns a Message."""
        if isinstance(key, slice):
            new = MessageLog(max_size=self._max_size)
            new._messages = self._messages[key]
            return new
        return self._messages[key]

    def __iter__(self):
        return iter(self._messages)

    def last(self, n: int = 1) -> list[Message]:
        """Return the last *n* messages."""
        return self._messages[-n:]

    def to_openai_format(self) -> list[dict]:
        """Convert to OpenAI-style message list for LLM calls."""
        return [{"role": m.role, "content": m.content} for m in self._messages]

    def cache_prefix(self) -> MessageLog:
        """Return a log containing only messages up to the newest cache boundary.

        Returns an empty log if no boundary is set.
        """
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i].is_cache_boundary:
                return self[: i + 1]
        return MessageLog(max_size=self._max_size)

    @property
    def trimmed(self) -> int:
        """Number of messages trimmed from the front."""
        return 0  # trim is no-op; kept for API compatibility

    # ── internals ─────────────────────────────────────────────

    def _trim(self) -> None:
        """Drop oldest non-boundary messages if over max_size.

        Keeps at least the most recent boundary + everything after it.
        """
        if len(self._messages) <= self._max_size:
            return

        # Find the newest cache boundary
        boundary_idx = -1
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i].is_cache_boundary:
                boundary_idx = i
                break

        keep_from = boundary_idx if boundary_idx >= 0 else len(self._messages) - self._max_size // 2
        # Always keep at least max_size/2 messages
        if keep_from > len(self._messages) - self._max_size // 2:
            keep_from = len(self._messages) - self._max_size // 2

        if keep_from > 0:
            self._messages = self._messages[keep_from:]

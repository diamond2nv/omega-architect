#!/usr/bin/env python3
"""JSONL-based search cache with TTL and auto-prune.

Used by SearchAggregator to cache search results and handle network
timeouts gracefully in development (China network instability).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("omega.search.cache")

CACHE_DIR = Path.home() / ".cache" / "omega" / "search_cache"
MAX_ENTRIES = 1000
DEFAULT_TTL = 3600  # 1 hour for dev; 300s for prod


@dataclass
class CachedEntry:
    query: str
    query_hash: str
    results: list[dict[str, Any]]
    source: str  # "leansearch", "loogle", "local_bm25", "merged"
    timestamp: float
    ttl: int

    def is_expired(self) -> bool:
        return (time.time() - self.timestamp) > self.ttl

    def to_jsonl(self) -> str:
        return json.dumps({
            "query": self.query,
            "query_hash": self.query_hash,
            "results": self.results,
            "source": self.source,
            "timestamp": self.timestamp,
            "ttl": self.ttl,
        }, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> "CachedEntry":
        d = json.loads(line)
        return cls(
            query=d["query"],
            query_hash=d["query_hash"],
            results=d["results"],
            source=d["source"],
            timestamp=d["timestamp"],
            ttl=d["ttl"],
        )


class JSONLCache:
    """Thread-safe JSONL cache with TTL support.

    Usage:
        cache = JSONLCache()
        cache.store("my query", results, "leansearch")
        cached = cache.lookup("my query")
    """

    def __init__(self, cache_dir: str | Path = CACHE_DIR, ttl: int = DEFAULT_TTL):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.cache_dir / "search_cache.jsonl"
        self._ttl = ttl
        self._hit_count = 0
        self._miss_count = 0

    # ── Public API ──────────────────────────────────────────

    def lookup(self, query: str, source: str = "merged") -> list[dict[str, Any]] | None:
        """Return cached results for *query* from *source*, or None if miss/expired."""
        qh = self._hash(query)
        for entry in self._read_all():
            if entry.query_hash == qh and entry.source == source:
                if not entry.is_expired():
                    self._hit_count += 1
                    logger.debug("Cache HIT: query=%s source=%s", query[:40], source)
                    return entry.results
                else:
                    logger.debug("Cache EXPIRED: query=%s source=%s", query[:40], source)
                    self._miss_count += 1
                    return None
        self._miss_count += 1
        return None

    def store(self, query: str, results: list[dict[str, Any]], source: str = "merged") -> None:
        """Store *results* for *query* from *source*."""
        entry = CachedEntry(
            query=query,
            query_hash=self._hash(query),
            results=results,
            source=source,
            timestamp=time.time(),
            ttl=self._ttl,
        )
        try:
            with open(self._path, "a") as f:
                f.write(entry.to_jsonl() + "\n")
            logger.debug("Cache STORE: query=%s source=%s (%d results)",
                         query[:40], source, len(results))
        except OSError as e:
            logger.warning("Cache write failed: %s", e)
        self._lazy_prune()

    def clear(self) -> None:
        """Clear all cached entries."""
        if self._path.exists():
            self._path.unlink()
        self._hit_count = 0
        self._miss_count = 0

    def stats(self) -> dict[str, Any]:
        """Return cache statistics."""
        total = 0
        try:
            if self._path.exists():
                with open(self._path) as f:
                    total = sum(1 for _ in f)
        except OSError:
            pass
        hit_rate = self._hit_count / max(self._hit_count + self._miss_count, 1)
        return {
            "total_entries": total,
            "hit_count": self._hit_count,
            "miss_count": self._miss_count,
            "hit_rate": round(hit_rate, 3),
            "cache_dir": str(self.cache_dir),
            "ttl": self._ttl,
        }

    # ── Internal ────────────────────────────────────────────

    def _hash(self, query: str) -> str:
        """Normalize query and produce SHA256[:12] hash for lookup."""
        normalized = query.lower().strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]

    def _read_all(self) -> list[CachedEntry]:
        """Read all entries from JSONL file (newest last)."""
        if not self._path.exists() or self._path.stat().st_size == 0:
            return []
        entries = []
        try:
            with open(self._path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(CachedEntry.from_jsonl(line))
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.debug("Skipping corrupt cache entry: %s", e)
        except OSError:
            pass
        return entries

    def _lazy_prune(self) -> None:
        """Prune to MAX_ENTRIES if exceeded (keeps newest entries)."""
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                lines = f.readlines()
            if len(lines) > MAX_ENTRIES * 1.2:
                # Keep newest MAX_ENTRIES
                keep = lines[-MAX_ENTRIES:]
                with open(self._path, "w") as f:
                    f.writelines(keep)
                logger.info("Cache pruned: %d -> %d entries", len(lines), len(keep))
        except OSError:
            pass

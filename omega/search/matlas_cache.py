#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Matlas API local cache — JSONL-backed + in-memory LRU.

Three-tier:
  L1: In-memory dict (fast, per-session)
  L2: JSONL disk file (persistent across sessions)
  L3: Matlas API remote (fallback on cache miss)

Cache entry format (JSONL):
  {"qkey": "sha256:12chars", "query": "...", "response": [...], "ts": 1234567890.0, "hits": 1}
"""

import json
import os
import time
import hashlib
from pathlib import Path
from collections import OrderedDict

# ── Config ──────────────────────────────────────────────────────
CACHE_DIR = Path.home() / ".hermes" / "cache"
CACHE_FILE = CACHE_DIR / "matlas_cache.jsonl"
MAX_MEMORY = 500          # Max in-memory entries
MAX_DISK = 5000           # Max lines in JSONL (prune oldest on growth)
TTL_SECONDS = 86400 * 30  # 30 days


def _qkey(query: str) -> str:
    """Normalized cache key: sha256 prefix."""
    norm = query.lower().strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


class MatlasCache:
    """Thread-safe-ish local cache for Matlas API responses."""

    def __init__(self):
        self._mem: OrderedDict[str, dict] = OrderedDict()
        self._load_disk()

    # ── Public API ────────────────────────────────────────────

    def get(self, query: str) -> list[dict] | None:
        """Look up cached response. Returns parsed list or None."""
        key = _qkey(query)
        entry = self._mem.get(key)
        if entry is not None:
            entry["hits"] += 1
            self._mem.move_to_end(key)
            return entry["response"]
        return None

    def set(self, query: str, response: list[dict]):
        """Store response in cache (memory + disk)."""
        key = _qkey(query)
        entry = {
            "qkey": key,
            "query": query[:500],
            "response": response,
            "ts": time.time(),
            "hits": 1,
        }
        # Memory
        self._mem[key] = entry
        self._mem.move_to_end(key)
        if len(self._mem) > MAX_MEMORY:
            self._mem.popitem(last=False)
        # Disk (append)
        self._append_disk(entry)

    def stats(self) -> dict:
        """Return cache statistics."""
        return {
            "memory_entries": len(self._mem),
            "disk_file": str(CACHE_FILE),
            "disk_exists": CACHE_FILE.exists(),
        }

    # ── Disk I/O ──────────────────────────────────────────────

    def _load_disk(self):
        """Load last N entries from JSONL into memory."""
        if not CACHE_FILE.exists():
            return
        try:
            with open(CACHE_FILE) as f:
                lines = f.readlines()
            # Load newest entries (reverse, dedup by key)
            seen = set()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = entry.get("qkey", "")
                if key and key not in seen:
                    seen.add(key)
                    self._mem[key] = entry
                    if len(self._mem) >= MAX_MEMORY:
                        break
        except OSError:
            pass

    def _append_disk(self, entry: dict):
        """Append one entry to JSONL, prune if too large."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(CACHE_FILE, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # Prune: if file too large, rewrite with newest MAX_DISK entries
            if CACHE_FILE.stat().st_size > 10 * 1024 * 1024:  # 10 MB
                self._prune_disk()
        except OSError:
            pass

    def _prune_disk(self):
        """Rewrite JSONL with only the newest MAX_DISK entries."""
        try:
            with open(CACHE_FILE) as f:
                all_entries = [json.loads(l) for l in f if l.strip()]
            # Dedup by qkey, keep newest
            seen = {}
            for e in all_entries:
                seen[e.get("qkey", "")] = e
            # Keep top MAX_DISK by timestamp
            sorted_entries = sorted(seen.values(), key=lambda x: x.get("ts", 0), reverse=True)[:MAX_DISK]
            with open(CACHE_FILE, "w") as f:
                for e in sorted_entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except (OSError, json.JSONDecodeError):
            pass


# ── Singleton ───────────────────────────────────────────────────
_cache = MatlasCache()


def cached_matlas_search(query: str, top_k: int = 3) -> list[dict]:
    """Search Matlas via cache. Hits cache → return. Miss → API → cache → return.

    Parameters
    ----------
    query : str
        Natural language or theorem-like query.
    top_k : int
        Max results to return.

    Returns
    -------
    list[dict]
        List of matched result dicts, each with keys: entity_name, statement,
        journal, title, year, doi, type. May be empty on failure.
    """
    # 1. Check cache
    cached = _cache.get(query)
    if cached is not None:
        return cached[:top_k]

    # 2. Cache miss — call Matlas API
    import urllib.request
    payload = json.dumps({"query": query, "num_results": 10}).encode()
    req = urllib.request.Request(
        "https://matlas.ai/api/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        results = json.loads(resp.read())
    except Exception:
        return []

    if not results or not isinstance(results, list):
        return []

    # 3. Store in cache
    _cache.set(query, results)

    return results[:top_k]


def format_matlas_results(results: list[dict], top_k: int = 3) -> str:
    """Format Matlas results into a readable prompt prefix.

    Returns empty string if no results.
    """
    if not results:
        return ""
    lines = ["Relevant mathematical results (from Matlas):"]
    count = 0
    for r in results:
        if count >= top_k:
            break
        stmt = r.get("statement", "").strip()
        if len(stmt) < 10:
            continue
        src = r.get("entity_name", "")
        journal = r.get("journal", "") or r.get("title", "")
        year = r.get("year", "")
        if journal:
            src += f" ({journal}"
            if year:
                src += f", {year}"
            src += ")"
        lines.append(f"  • {stmt[:300]}")
        count += 1
    return "\n".join(lines) if count > 0 else ""


def cache_stats() -> dict:
    """Return cache statistics for monitoring."""
    return _cache.stats()

#!/usr/bin/env python3
"""ProofCache — SQLite-backed LLM output cache for theorem proving.

Caches LLM responses keyed by (theorem_header, model_id, temperature),
avoiding repeated API calls for the same theorem across runs.

Usage::

    from omega.prover.cache import ProofCache

    cache = ProofCache()
    result = cache.lookup(theorem_header="theorem t : True :=", model_id="deepseek/v4-flash")
    if result is None:
        llm_output = generate_fn(prompt)
        cache.store(theorem_header="...", model_id="...", prompt=prompt, llm_output=llm_output)
    else:
        llm_output = result
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("omega.cache")

# Default cache path
_DEFAULT_CACHE_DIR = Path.home() / ".omega" / "cache"
_DEFAULT_DB_PATH = _DEFAULT_CACHE_DIR / "proof_cache.db"

# ── Schema ─────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proof_cache (
    cache_key     TEXT PRIMARY KEY,
    theorem_header TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    temperature   REAL NOT NULL DEFAULT 0.3,
    prompt_hash   TEXT NOT NULL,
    llm_output    TEXT NOT NULL,
    n_tokens_in   INTEGER NOT NULL DEFAULT 0,
    n_tokens_out  INTEGER NOT NULL DEFAULT 0,
    compiled      INTEGER NOT NULL DEFAULT 0,
    elapsed_ms    INTEGER NOT NULL DEFAULT 0,
    cached_at     TEXT NOT NULL DEFAULT (datetime('now')),
    hit_count     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_cache_theorem ON proof_cache(theorem_header);
CREATE INDEX IF NOT EXISTS idx_cache_model ON proof_cache(model_id);
CREATE INDEX IF NOT EXISTS idx_cache_cached_at ON proof_cache(cached_at);
"""

_CLEANUP_SQL = """
DELETE FROM proof_cache
WHERE cached_at < datetime('now', ? || ' days')
"""


# ── Cache result ───────────────────────────────────────────────


@dataclass
class CacheEntry:
    """A single cache entry."""
    cache_key: str
    theorem_header: str
    model_id: str
    temperature: float
    prompt_hash: str
    llm_output: str
    n_tokens_in: int = 0
    n_tokens_out: int = 0
    compiled: bool = False
    elapsed_ms: int = 0
    cached_at: str = ""
    hit_count: int = 1


# ── Key computation ────────────────────────────────────────────


def _compute_key(theorem_header: str, model_id: str, temperature: float) -> str:
    """Compute a deterministic cache key.

    Uses SHA-256 of the concatenated ``theorem_header | model_id | temperature``.
    """
    data = f"{theorem_header}||{model_id}||{temperature}".encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hash_prompt(prompt: str) -> str:
    """Compute a hash of the full prompt (for validation)."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


# ── ProofCache ─────────────────────────────────────────────────


class ProofCache:
    """SQLite-backed cache for LLM proof generation outputs.

    Parameters
    ----------
    db_path : str or Path
        Path to the SQLite database file (default: ``~/.omega/cache/proof_cache.db``).
    auto_cleanup_days : int or None
        Auto-clean entries older than this many days on open.  ``None`` (default)
        disables auto-cleanup.
    max_entries : int or None
        Maximum number of entries before eviction (LRU).  ``None`` (default)
        disables eviction.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        auto_cleanup_days: int | None = None,
        max_entries: int | None = None,
    ):
        self._db_path = Path(db_path or _DEFAULT_DB_PATH)
        self._auto_cleanup_days = auto_cleanup_days
        self._max_entries = max_entries
        self._lock = threading.Lock()

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

        if self._auto_cleanup_days:
            self._cleanup()

        logger.info("ProofCache ready: %s (%d entries)", self._db_path, self.size)

    # ── Public API ───────────────────────────────────────────

    def lookup(
        self,
        theorem_header: str,
        model_id: str,
        temperature: float = 0.3,
    ) -> str | None:
        """Look up a cached LLM output.

        Parameters
        ----------
        theorem_header : str
            The Lean theorem header (cache key component).
        model_id : str
            Model identifier (cache key component).
        temperature : float
            Sampling temperature (cache key component, default 0.3).

        Returns
        -------
        str or None
            The cached LLM output, or ``None`` if not found.
        """
        key = _compute_key(theorem_header, model_id, temperature)
        with self._lock:
            row = self._conn.execute(
                "SELECT llm_output FROM proof_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                # Increment hit count
                self._conn.execute(
                    "UPDATE proof_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
                    (key,),
                )
                self._conn.commit()
                logger.debug("Cache HIT: %s… (key=%s)", theorem_header[:40], key[:12])
                return row[0]
            logger.debug("Cache MISS: %s… (key=%s)", theorem_header[:40], key[:12])
            return None

    def store(
        self,
        theorem_header: str,
        model_id: str,
        prompt: str = "",
        llm_output: str = "",
        temperature: float = 0.3,
        n_tokens_in: int = 0,
        n_tokens_out: int = 0,
        compiled: bool = False,
        elapsed_ms: int = 0,
    ) -> str:
        """Store an LLM output in the cache.

        Parameters
        ----------
        theorem_header : str
            The Lean theorem header.
        model_id : str
            Model identifier.
        prompt : str
            The full prompt sent to the LLM (hashed for validation).
        llm_output : str
            The LLM output to cache.
        temperature : float
            Sampling temperature (default 0.3).
        n_tokens_in : int
            Input token count (default 0).
        n_tokens_out : int
            Output token count (default 0).
        compiled : bool
            Whether this output compiled successfully (default False).
        elapsed_ms : int
            LLM call elapsed time in ms (default 0).

        Returns
        -------
        str
            The cache key that was stored.
        """
        if not llm_output:
            return ""

        key = _compute_key(theorem_header, model_id, temperature)
        prompt_hash = _hash_prompt(prompt) if prompt else ""
        now = datetime.now(UTC).isoformat()

        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO proof_cache
                   (cache_key, theorem_header, model_id, temperature, prompt_hash,
                    llm_output, n_tokens_in, n_tokens_out, compiled, elapsed_ms, cached_at, hit_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    key, theorem_header[:500], model_id, temperature, prompt_hash,
                    llm_output, n_tokens_in, n_tokens_out,
                    1 if compiled else 0, elapsed_ms, now,
                ),
            )
            self._conn.commit()

            # Eviction
            if self._max_entries is not None:
                self._evict_lru()

        logger.debug("Cache STORE: %s… (key=%s, %d chars)", theorem_header[:40], key[:12], len(llm_output))
        return key

    def lookup_or_generate(
        self,
        theorem_header: str,
        model_id: str,
        generate_fn: Callable[[str], str],
        prompt: str = "",
        temperature: float = 0.3,
    ) -> str:
        """Look up cached output; if missing, call ``generate_fn`` and cache the result.

        This is the primary integration point for GoedelProver.
        """
        cached = self.lookup(theorem_header, model_id, temperature)
        if cached is not None:
            return cached

        if not generate_fn:
            return ""

        output = generate_fn(prompt)
        if output:
            self.store(
                theorem_header=theorem_header,
                model_id=model_id,
                prompt=prompt,
                llm_output=output,
                temperature=temperature,
            )
        return output

    def wrap_generate_fn(
        self,
        generate_fn: Callable[[str], str] | None,
        theorem_header: str,
        model_id: str,
        temperature: float = 0.3,
    ) -> Callable[[str], str]:
        """Wrap a ``generate_fn`` with cache lookup.

        Returns a new callable that:
        1. Checks the cache first (by ``theorem_header`` + ``model_id`` + ``temperature``)
        2. On miss: calls ``generate_fn``, caches the result, returns it
        3. On hit: returns cached result directly

        Parameters
        ----------
        generate_fn : Callable[[str], str] or None
            The original generate function.  If ``None``, returns an empty-returning
            wrapper.
        theorem_header : str
            The theorem being proved (cache key component).
        model_id : str
            Model identifier (cache key component).
        temperature : float
            Sampling temperature (default 0.3).

        Returns
        -------
        Callable[[str], str]
            Cache-aware generate function with the same signature.
        """

        def _cached_generate(prompt: str) -> str:
            cached = self.lookup(theorem_header, model_id, temperature)
            if cached is not None:
                return cached
            if generate_fn is None:
                return ""
            output = generate_fn(prompt)
            if output:
                self.store(
                    theorem_header=theorem_header,
                    model_id=model_id,
                    prompt=prompt,
                    llm_output=output,
                    temperature=temperature,
                )
            return output

        return _cached_generate

    # ── Statistics ─────────────────────────────────────────────

    @property
    def size(self) -> int:
        """Number of entries in the cache."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM proof_cache").fetchone()
            return row[0] if row else 0

    @property
    def hit_rate(self) -> float:
        """Cache hit rate (0.0 — 1.0)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(hit_count - 1) * 1.0 / SUM(hit_count) FROM proof_cache"
            ).fetchone()
            if row and row[0] is not None:
                return row[0]
            return 0.0

    def stats(self) -> dict[str, Any]:
        """Return cache statistics as a dict.

        Returns
        -------
        dict
            Keys: ``entries``, ``hit_rate``, ``db_size_mb``, ``oldest``, ``newest``
        """
        with self._lock:
            entries = self._conn.execute("SELECT COUNT(*) FROM proof_cache").fetchone()[0]
            total_hits = self._conn.execute(
                "SELECT COALESCE(SUM(hit_count), 0) FROM proof_cache"
            ).fetchone()[0]
            cached_hits = self._conn.execute(
                "SELECT COALESCE(SUM(hit_count - 1), 0) FROM proof_cache"
            ).fetchone()[0]
            oldest = self._conn.execute(
                "SELECT MIN(cached_at) FROM proof_cache"
            ).fetchone()[0]
            newest = self._conn.execute(
                "SELECT MAX(cached_at) FROM proof_cache"
            ).fetchone()[0]

        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0

        return {
            "entries": entries,
            "total_hits": total_hits,
            "cached_hits": cached_hits,
            "hit_rate": round(cached_hits / max(1, total_hits), 3),
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "oldest": oldest or "N/A",
            "newest": newest or "N/A",
        }

    # ── Maintenance ────────────────────────────────────────────

    def clear(self) -> int:
        """Clear all entries from the cache.

        Returns
        -------
        int
            Number of entries deleted.
        """
        with self._lock:
            count = self._conn.execute("DELETE FROM proof_cache").rowcount
            self._conn.commit()
            logger.info("ProofCache cleared: %d entries deleted", count)
            return count

    def _cleanup(self) -> int:
        """Remove entries older than ``auto_cleanup_days`` days.

        Returns
        -------
        int
            Number of entries deleted.
        """
        if not self._auto_cleanup_days:
            return 0
        days = str(self._auto_cleanup_days)
        with self._lock:
            count = self._conn.execute(
                _CLEANUP_SQL, (days,)
            ).rowcount
            self._conn.commit()
            if count > 0:
                logger.info("ProofCache cleanup: %d entries older than %s days deleted", count, days)
            return count

    def _evict_lru(self) -> int:
        """Evict oldest entries when over ``max_entries``.

        Returns
        -------
        int
            Number of entries evicted.
        """
        if self._max_entries is None:
            return 0
        with self._lock:
            current = self._conn.execute("SELECT COUNT(*) FROM proof_cache").fetchone()[0]
            if current <= self._max_entries:
                return 0
            to_evict = current - self._max_entries
            # Delete oldest entries (LRU strategy)
            self._conn.execute(
                "DELETE FROM proof_cache WHERE cache_key IN ("
                "SELECT cache_key FROM proof_cache ORDER BY cached_at ASC LIMIT ?"
                ")", (to_evict,)
            )
            self._conn.commit()
            logger.info("ProofCache eviction: %d entries removed (max=%d)", to_evict, self._max_entries)
            return to_evict

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> ProofCache:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

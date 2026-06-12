#!/usr/bin/env python3
"""Async Multi-Source Search Aggregator (P0).

Fires multiple search sources concurrently, merges results with dedup,
and caches everything via JSONLCache.

Usage:
    from omega.search.aggregator import SearchAggregator
    agg = SearchAggregator()
    result = await agg.search("commutativity of addition on ℕ")
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from omega.search.cache import JSONLCache
from omega.search.sources.sources import (
    LeanSearchSource,
    LoogleSource,
    LocalBM25Source,
    SearchSource,
    SourceResult,
)

logger = logging.getLogger("omega.search.aggregator")


@dataclass
class AggregatedResult:
    """Merged result from multiple search sources."""
    query: str
    results: list[dict[str, Any]]  # Deduplicated, scored
    source_counts: dict[str, int]   # source_name → result_count
    total_elapsed_ms: int
    sources_used: list[str]         # Which sources contributed
    sources_failed: list[str]       # Which sources timed out / errored
    from_cache: bool = False


class SearchAggregator:
    """Async multi-source search with cache, timeout, and merge."""

    def __init__(
        self,
        cache: JSONLCache | None = None,
        timeout: float = 10.0,
        cache_only: bool = False,
    ):
        self.cache = cache or JSONLCache()
        self.timeout = timeout
        self.cache_only = cache_only  # dev mode: skip remote
        self._sources: list[SearchSource] = [
            LeanSearchSource(),
            LoogleSource(),
            LocalBM25Source(),
        ]

    # ── Main API ───────────────────────────────────────────────

    async def search(
        self,
        query: str,
        mode: str = "hybrid",
        force_refresh: bool = False,
    ) -> AggregatedResult:
        """Search all sources, aggregate results.

        Args:
            query: Search query string.
            mode: "hybrid" (all sources), "remote" (leansearch+loogle),
                  "local" (BM25 only).
            force_refresh: Skip cache and force network query.

        Returns:
            AggregatedResult with deduplicated, scored results.
        """
        import time
        t0 = time.time()

        # 1. Check cache first (unless forced refresh)
        if not force_refresh:
            cached = self.cache.lookup(query, source="merged")
            if cached is not None:
                elapsed = int((time.time() - t0) * 1000)
                return AggregatedResult(
                    query=query, results=cached,
                    source_counts={}, total_elapsed_ms=elapsed,
                    sources_used=["cache"], sources_failed=[],
                    from_cache=True,
                )

        # 2. Select sources based on mode
        sources = self._select_sources(mode)

        # 3. Fire all sources concurrently
        tasks = [self._safe_search(src, query) for src in sources]
        results_list: list[SourceResult] = await asyncio.gather(*tasks)

        # 4. Collect successes and failures
        successes = [r for r in results_list if r.success]
        failures = [r for r in results_list if not r.success]

        # 5. If all remote sources failed, try cache fallback
        remote_ok = any(r.source in ("leansearch", "loogle") and r.success for r in successes)
        if not remote_ok and not self.cache_only:
            cached = self.cache.lookup(query, source="merged")
            if cached is not None:
                elapsed = int((time.time() - t0) * 1000)
                return AggregatedResult(
                    query=query, results=cached,
                    source_counts={}, total_elapsed_ms=elapsed,
                    sources_used=["cache"], sources_failed=[f.source for f in failures],
                    from_cache=True,
                )

        # 6. Merge and deduplicate results
        merged = self._merge(successes)
        elapsed = int((time.time() - t0) * 1000)

        # 7. Cache merged result
        self.cache.store(query, merged, source="merged")

        return AggregatedResult(
            query=query,
            results=merged,
            source_counts={r.source: len(r.results) for r in successes},
            total_elapsed_ms=elapsed,
            sources_used=[r.source for r in successes],
            sources_failed=[r.source for r in failures],
        )

    # ── Source Selection ───────────────────────────────────────

    def _select_sources(self, mode: str) -> list[SearchSource]:
        if self.cache_only:
            return [s for s in self._sources if isinstance(s, LocalBM25Source)]
        if mode == "remote":
            return [s for s in self._sources if not isinstance(s, LocalBM25Source)]
        if mode == "local":
            return [s for s in self._sources if isinstance(s, LocalBM25Source)]
        return self._sources  # hybrid = all

    # ── Safe Search ────────────────────────────────────────────

    async def _safe_search(self, source: SearchSource, query: str) -> SourceResult:
        """Run source.search with timeout, return error result on failure."""
        try:
            return await asyncio.wait_for(
                source.search(query), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            return SourceResult(
                source=source.name, query=query,
                results=[], elapsed_ms=int(self.timeout * 1000),
                error="timeout", success=False,
            )
        except Exception as e:
            logger.warning("Source %s failed: %s", source.name, e)
            return SourceResult(
                source=source.name, query=query,
                results=[], elapsed_ms=0,
                error=str(e), success=False,
            )

    # ── Merge & Dedup ──────────────────────────────────────────

    def _merge(self, results: list[SourceResult]) -> list[dict[str, Any]]:
        """Merge results from multiple sources, dedup by name."""
        seen_names: set[str] = set()
        merged: list[dict[str, Any]] = []
        # Priority order: leansearch → loogle → local
        for src_name in ("leansearch", "loogle", "local_bm25"):
            for r in results:
                if r.source != src_name:
                    continue
                for item in r.results:
                    name = item.get("name", "")
                    if name and name not in seen_names:
                        seen_names.add(name)
                        item["_source"] = src_name
                        merged.append(item)
        return merged[:10]  # Top 10

    # ── Cache Control ──────────────────────────────────────────

    def set_cache_only(self, enabled: bool = True) -> None:
        """Enable/disable cache-only mode (useful for development)."""
        self.cache_only = enabled

    def stats(self) -> dict[str, Any]:
        """Return aggregator and cache stats."""
        return {
            "cache": self.cache.stats(),
            "sources": [s.name for s in self._sources],
            "cache_only": self.cache_only,
            "timeout": self.timeout,
        }

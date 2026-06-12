#!/usr/bin/env python3
"""Search source interface and concrete implementations for P0 Aggregator.

Defines:
- SearchSource: abstract interface
- LeanSearchSource: async leansearch.net client
- LoogleSource: async loogle client
- LocalBM25Source: BM25 over wiki + paper store
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("omega.search.sources")


@dataclass
class SourceResult:
    """Result from a single search source."""
    source: str
    query: str
    results: list[dict[str, Any]]  # [{name, type, url, snippet}, ...]
    elapsed_ms: int
    error: str | None = None
    success: bool = True


class SearchSource(abc.ABC):
    """Abstract search source interface."""

    name: str = "base"
    timeout: float = 10.0

    @abc.abstractmethod
    async def search(self, query: str) -> SourceResult:
        """Execute search, return results. Raise asyncio.TimeoutError if exceeded."""
        ...

    def health(self) -> bool:
        """Check if this source is available. Override in subclasses."""
        return True


class LeanSearchSource(SearchSource):
    """Async client for leansearch.net (semantic search by natural language)."""

    name = "leansearch"
    timeout = 10.0

    def __init__(self, endpoint: str = "https://leansearch.net/api/search"):
        self._endpoint = endpoint
        self._healthy = True

    async def search(self, query: str) -> SourceResult:
        import time
        import aiohttp

        t0 = time.time()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._endpoint,
                    json={"query": query, "limit": 5},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        return SourceResult(
                            source=self.name, query=query,
                            results=[], elapsed_ms=0,
                            error=f"HTTP {resp.status}", success=False,
                        )
                    data = await resp.json()
                    elapsed = int((time.time() - t0) * 1000)
                    results = [
                        {"name": r.get("name", ""), "type": r.get("type", "lemma"),
                         "url": r.get("url", ""), "snippet": r.get("snippet", "")}
                        for r in data.get("results", [])
                    ]
                    return SourceResult(
                        source=self.name, query=query,
                        results=results, elapsed_ms=elapsed,
                    )
        except asyncio.TimeoutError:
            return SourceResult(
                source=self.name, query=query,
                results=[], elapsed_ms=0,
                error="timeout", success=False,
            )
        except Exception as e:
            logger.warning("leansearch error: %s", e)
            self._healthy = False
            return SourceResult(
                source=self.name, query=query,
                results=[], elapsed_ms=0,
                error=str(e), success=False,
            )

    def health(self) -> bool:
        return self._healthy


class LoogleSource(SearchSource):
    """Async client for loogle.lean-lang.org (search by type signature)."""

    name = "loogle"
    timeout = 10.0

    def __init__(self, endpoint: str = "https://loogle.lean-lang.org/api/search"):
        self._endpoint = endpoint
        self._healthy = True

    async def search(self, query: str) -> SourceResult:
        import time
        import aiohttp

        t0 = time.time()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._endpoint,
                    params={"q": query, "limit": 5},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        return SourceResult(
                            source=self.name, query=query,
                            results=[], elapsed_ms=0,
                            error=f"HTTP {resp.status}", success=False,
                        )
                    data = await resp.json()
                    elapsed = int((time.time() - t0) * 1000)
                    results = [
                        {"name": r.get("name", ""), "type": r.get("type", "theorem"),
                         "url": r.get("url", ""), "snippet": r.get("snippet", "")}
                        for r in data.get("results", [])
                    ]
                    return SourceResult(
                        source=self.name, query=query,
                        results=results, elapsed_ms=elapsed,
                    )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("loogle error: %s", e)
            self._healthy = False
            return SourceResult(
                source=self.name, query=query,
                results=[], elapsed_ms=0,
                error=str(e), success=False,
            )

    def health(self) -> bool:
        return self._healthy


class LocalBM25Source(SearchSource):
    """BM25 search over local wiki + paper store.

    Uses simple TF-based scoring over indexed markdown files.
    No external dependencies.
    """

    name = "local_bm25"
    timeout = 0.5  # local, fast

    def __init__(self, wiki_dir: str | None = None):
        import os
        self._wiki_dir = wiki_dir or os.path.expanduser(
            "~/Gitlab/Agentic4Sci/llm-wiki/wiki")
        self._index: dict[str, str] = {}  # filename → content
        self._built = False

    def _ensure_index(self):
        if self._built:
            return
        from pathlib import Path
        wiki = Path(self._wiki_dir)
        if wiki.exists():
            for f in wiki.rglob("*.md"):
                try:
                    self._index[f.name] = f.read_text()
                except Exception:
                    pass
        self._built = True
        logger.info("LocalBM25Source: indexed %d files from %s",
                     len(self._index), self._wiki_dir)

    async def search(self, query: str) -> SourceResult:
        import time
        t0 = time.time()
        self._ensure_index()

        if not self._index:
            return SourceResult(
                source=self.name, query=query,
                results=[], elapsed_ms=0,
                error="no index", success=True,
            )

        # Simple TF scoring: count query term frequency per doc
        terms = query.lower().split()
        scored: list[tuple[float, str, str]] = []
        for fname, content in self._index.items():
            lower = content.lower()
            score = sum(lower.count(t) for t in terms)
            if score > 0:
                # Extract first meaningful line as snippet
                snippet = ""
                for line in content.split("\n")[:5]:
                    stripped = line.strip().strip("#").strip()
                    if stripped:
                        snippet = stripped[:120]
                        break
                scored.append((score, fname, snippet))

        scored.sort(key=lambda x: -x[0])
        elapsed = int((time.time() - t0) * 1000)

        results = [
            {"name": fname.replace(".md", ""), "type": "wiki_page",
             "url": f"file://{fname}", "snippet": snippet}
            for score, fname, snippet in scored[:5]
        ]
        return SourceResult(
            source=self.name, query=query,
            results=results, elapsed_ms=elapsed,
        )

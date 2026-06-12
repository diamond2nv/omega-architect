---
plan_id: search-async-v1
title: "P0: Async Multi-Source Search Aggregator"
version: 0.1
date: 2026-06-12
author: omega-agent
status: draft
depends_on: [omega/search/lean_search.py, omega/search/matlas_cache.py, omega/research/sources/wiki.py]
implements: P0
estimation: 2 days
---

# P0: Async Multi-Source Search Aggregator

## Motivation

Current `lean_search.py` calls lean-lsp-mcp synchronously — one subprocess spawn per query, 1-2s per call, no fallback on network failure. In China, leansearch.net frequently times out, causing the entire proof round to stall.

## Design

### Architecture

```
omega/search/
├── __init__.py            # Exports SearchAggregator
├── aggregator.py          # ← NEW: AsyncSearchAggregator
├── sources/
│   ├── __init__.py
│   ├── leansearch.py      # ← NEW: async leansearch.net client
│   ├── loogle.py          # ← NEW: async loogle client
│   ├── local_bm25.py      # ← NEW: local BM25 index over wiki
│   └── local_embedding.py # ← NEW: vector search (via QMD/SQLite)
├── cache.py               # ← NEW: JSONL cache layer
└── lean_search.py         # Legacy, kept for backward compat
```

### AsyncSearchAggregator

```python
class SearchAggregator:
    """Async, multi-source, cached search aggregator."""

    sources: list[SearchSource]  # leansearch, loogle, local BM25, local embedding
    cache: JSONLCache            # ~/.cache/omega/search_cache/
    timeout: float = 10.0        # per-source timeout

    async def search(self, query: str, mode: str = "hybrid") -> AggregatedResult:
        """Search all sources concurrently, aggregate results."""

    async def search_sources(self, query: str) -> list[SourceResult]:
        """Fire all sources concurrently, gather with timeout."""

    def _merge(self, results: list[SourceResult]) -> AggregatedResult:
        """Dedup by lemma name/URL, score fusion, top-k ranking."""
```

### JSONL Cache Layer

```python
class JSONLCache:
    """Thread-safe JSONL cache with TTL."""

    path: Path  # ~/.cache/omega/search_cache/
    ttl: int = 3600  # dev mode; 300s for prod

    def lookup(self, query: str) -> AggregatedResult | None:
        """sha256(normalize(query)) → cached result if within TTL."""

    def store(self, query: str, result: AggregatedResult):
        """Append to JSONL, auto-prune when >1000 entries."""

    def stats(self) -> dict:
        """Hit rate, entry count, oldest entry age."""
```

### Source Interface

```python
class SearchSource(ABC):
    name: str
    timeout: float

    @abstractmethod
    async def search(self, query: str) -> SourceResult:
        """Search and return results. Raise TimeoutError after timeout."""

    @abstractmethod
    def health(self) -> bool:
        """Is this source available?"""
```

### Fallback Chain

| Scenario | Behavior |
|----------|----------|
| All sources succeed | Return merged top-10 |
| 1-2 remote sources timeout | Return local + successful remote |
| All remote timeout, cache hit | Return cached + local results |
| All remote timeout, no cache | Return local BM25 only, flag "unavailable" |
| Dev mode (CACHE_ONLY=1) | Skip remote, use cache + local |

### Integration with Current System

```python
# In omega/search/__init__.py
from .aggregator import SearchAggregator

# Global instance (lazy init)
_aggregator: SearchAggregator | None = None

def get_aggregator() -> SearchAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = SearchAggregator()
    return _aggregator

# Used by inner_loop MCP dispatch:
# Old: mcp_result = mcp.call_tool("lean_leansearch", ...)
# New: result = await get_aggregator().search(query)
```

## Existing Code Integration

| Existing Module | Integration |
|----------------|-------------|
| `omega/search/lean_search.py` | Wraps lean-lsp-mcp subprocess; kept as `LeanSearchSource` backend |
| `omega/search/matlas_cache.py` | Legacy JSONL cache; replaced by new `cache.py` (more structured) |
| `omega/research/sources/wiki.py` | wiki search → `LocalBM25Source` backend |
| `omega/research/sources/paperstore.py` | paper store → index into BM25 |

## Implementation Steps

```
Day 1:
  1. Create cache.py (JSONL cache with TTL, lookup/store/stats/prune)
  2. Create aggregator.py (async gather, timeout, merge logic)
  3. Create sources/leansearch.py (async HTTP client for leansearch.net)

Day 2:
  4. Create sources/loogle.py (async HTTP client for loogle)
  5. Create sources/local_bm25.py (index wiki + paper store)
  6. Integrate with inner_loop MCP dispatch (replace sync MCP calls)
  7. Write tests
```

"""Classification cache — persistent JSONL cache for error→classification pairs.

Structure
---------
Cache directory (default: ~/.cache/omega/judge_cache/)
  └── <prefix>/            # 2-char prefix for sharding
       └── <cache_key>.json

Each file contains the serialized classification result.
Sharding by 2-char prefix avoids directory blowup with thousands of entries.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class ClassificationCache:
    """Persistent cache for error classification results.

    Stores results in individual JSON files, sharded by cache_key prefix
    to avoid directory blowup.

    Each entry:
    - key: sha256(error_msg)[:12]:sha256(theorem_header)[:12]
    - value: dict with classification result
    - metadata: created_at timestamp, hit_count
    """

    def __init__(self, cache_dir: str, ttl_days: int = 90) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_days * 86400
        self._hit_counts: dict[str, int] = {}

    def _path_for(self, cache_key: str) -> Path:
        prefix = cache_key[:2]
        prefix_dir = self._cache_dir / prefix
        prefix_dir.mkdir(exist_ok=True)
        return prefix_dir / f"{cache_key}.json"

    def lookup(self, cache_key: str) -> dict | None:
        """Look up a cached classification by key.

        Returns the raw dict, or None if not found / expired.
        """
        path = self._path_for(cache_key)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        # Check TTL
        created_at = data.get("_meta", {}).get("created_at", 0)
        if time.time() - created_at > self._ttl_seconds:
            path.unlink(missing_ok=True)
            return None

        # Update hit count
        self._hit_counts[cache_key] = self._hit_counts.get(cache_key, 0) + 1

        return data.get("result")

    def store(self, cache_key: str, result: dict) -> None:
        """Store a classification result."""
        path = self._path_for(cache_key)
        data = {
            "_meta": {
                "created_at": time.time(),
                "key": cache_key,
                "version": 1,
            },
            "result": result,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def invalidate(self, cache_key: str) -> bool:
        """Remove a cached entry. Returns True if it existed."""
        path = self._path_for(cache_key)
        if path.exists():
            path.unlink()
            return True
        return False

    def clear_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.time()
        count = 0
        for prefix_dir in self._cache_dir.iterdir():
            if not prefix_dir.is_dir() or len(prefix_dir.name) != 2:
                continue
            for f in prefix_dir.iterdir():
                if f.suffix != ".json":
                    continue
                try:
                    data = json.loads(f.read_text())
                    created_at = data.get("_meta", {}).get("created_at", 0)
                    if now - created_at > self._ttl_seconds:
                        f.unlink()
                        count += 1
                except (json.JSONDecodeError, OSError):
                    count += 1
        return count

    @property
    def size(self) -> int:
        """Approximate number of cached entries."""
        count = 0
        for prefix_dir in self._cache_dir.iterdir():
            if prefix_dir.is_dir() and len(prefix_dir.name) == 2:
                count += sum(1 for _ in prefix_dir.glob("*.json"))
        return count

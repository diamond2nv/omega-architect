#!/usr/bin/env python3
"""Cross-session ProofCache sync via git (NAS-backed).

P7 of the Ω-Architect roadmap.

Strategy: export the local SQLite ProofCache to a newline-delimited JSON file,
commit it to a ``cache-branch`` on the ``local`` NAS git remote, and pull
from NAS on startup to merge remote entries.

Usage::

    from omega.prover.cache_sync import ProofCacheSync

    syncer = ProofCacheSync()
    syncer.pull()   # Pull remote cache → local SQLite
    syncer.push()   # Push local cache → remote git
    syncer.sync()   # Both

Designed as a background operation — call ``push()`` after each benchmark run
and ``pull()`` on startup.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from omega.prover.cache import ProofCache

logger = logging.getLogger("omega.prover.cache_sync")

# Default paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_DIR = Path.home() / ".omega" / "cache"
_CACHE_DB = _CACHE_DIR / "proof_cache.db"
_EXPORT_FILE = _CACHE_DIR / "proof_cache_export.jsonl"
_CACHE_BRANCH = "cache-branch"
_GIT_REMOTE = "local"
_GIT_PUSH_URL = "ssh://git@192.168.0.25:222/My_Hermes_Team/omega-architect.git"


# ── Export / Import ─────────────────────────────────────────────


def export_cache_to_jsonl(cache: ProofCache | None = None,
                          output_path: str | Path | None = None) -> int:
    """Export ProofCache entries to newline-delimited JSON.

    Parameters
    ----------
    cache : ProofCache, optional
        Cache instance. Creates default if None.
    output_path : str or Path, optional
        Output JSONL path.

    Returns
    -------
    int
        Number of entries exported.
    """
    if cache is None:
        cache = ProofCache()
    if output_path is None:
        output_path = _EXPORT_FILE

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Read all entries from SQLite
    with cache._lock:
        rows = cache._conn.execute(
            "SELECT cache_key, theorem_header, model_id, temperature, "
            "prompt_hash, llm_output, n_tokens_in, n_tokens_out, "
            "compiled, elapsed_ms, cached_at, hit_count "
            "FROM proof_cache"
        ).fetchall()

    count = 0
    with open(output_path, "w") as f:
        for row in rows:
            entry = {
                "cache_key": row[0],
                "theorem_header": row[1],
                "model_id": row[2],
                "temperature": row[3],
                "prompt_hash": row[4],
                "llm_output": row[5],
                "n_tokens_in": row[6],
                "n_tokens_out": row[7],
                "compiled": bool(row[8]),
                "elapsed_ms": row[9],
                "cached_at": row[10],
                "hit_count": row[11],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            count += 1

    logger.info("Exported %d cache entries to %s", count, output_path)
    return count


def import_jsonl_to_cache(jsonl_path: str | Path | None = None,
                          cache: ProofCache | None = None,
                          merge_mode: str = "newer") -> int:
    """Import cache entries from JSONL into SQLite.

    Parameters
    ----------
    jsonl_path : str or Path, optional
        Path to JSONL file.
    cache : ProofCache, optional
        Cache instance.
    merge_mode : str
        ``"newer"`` (default): skip if local entry is newer.
        ``"overwrite"``: always replace.

    Returns
    -------
    int
        Number of entries imported.
    """
    if cache is None:
        cache = ProofCache()
    if jsonl_path is None:
        jsonl_path = _EXPORT_FILE

    jsonl_path = Path(jsonl_path)
    if not jsonl_path.exists():
        logger.info("No export file at %s — nothing to import", jsonl_path)
        return 0

    count = 0
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            cache_key = entry.get("cache_key", "")
            if not cache_key:
                continue

            if merge_mode == "newer":
                # Check if local entry is newer
                local = cache._conn.execute(
                    "SELECT cached_at FROM proof_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
                if local and local[0] >= entry.get("cached_at", ""):
                    continue

            # UPSERT
            cache._conn.execute(
                """INSERT OR REPLACE INTO proof_cache
                   (cache_key, theorem_header, model_id, temperature, prompt_hash,
                    llm_output, n_tokens_in, n_tokens_out, compiled, elapsed_ms,
                    cached_at, hit_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cache_key,
                    entry.get("theorem_header", "")[:500],
                    entry.get("model_id", "unknown"),
                    entry.get("temperature", 0.3),
                    entry.get("prompt_hash", ""),
                    entry.get("llm_output", ""),
                    entry.get("n_tokens_in", 0),
                    entry.get("n_tokens_out", 0),
                    1 if entry.get("compiled", False) else 0,
                    entry.get("elapsed_ms", 0),
                    entry.get("cached_at", ""),
                    entry.get("hit_count", 1),
                ),
            )
            count += 1

    cache._conn.commit()
    logger.info("Imported %d entries from %s (mode=%s)", count, jsonl_path, merge_mode)
    return count


# ── Git sync ────────────────────────────────────────────────────


def _run_git(args: list[str], cwd: str | Path | None = None) -> str:
    """Run a git command and return stdout."""
    if cwd is None:
        cwd = _PROJECT_ROOT
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning("git %s failed: %s", " ".join(args), result.stderr.strip())
    return result.stdout.strip()


class ProofCacheSync:
    """Sync ProofCache to NAS via git.

    Operations are non-blocking (best-effort). Failures are logged
    but don't raise exceptions (cache is always usable locally).

    Parameters
    ----------
    project_root : str or Path
        Git repo root (default: auto-detected).
    cache : ProofCache, optional
    remote : str
        Git remote name (default: ``local``).
    branch : str
        Branch for cache data (default: ``cache-branch``).
    """

    def __init__(
        self,
        project_root: str | Path | None = None,
        cache: ProofCache | None = None,
        remote: str = _GIT_REMOTE,
        branch: str = _CACHE_BRANCH,
    ):
        self.project_root = Path(project_root or _PROJECT_ROOT)
        self.cache = cache or ProofCache()
        self.remote = remote
        self.branch = branch

    def pull(self) -> dict[str, Any]:
        """Pull remote cache entries into local SQLite.

        Steps:
        1. Fetch ``cache-branch`` from NAS
        2. Check out ``cache-branch`` temporarily
        3. Read ``proof_cache_export.jsonl``
        4. Import entries with ``newer`` merge mode

        Returns
        -------
        dict
            ``{"imported": int, "error": str or None}``
        """
        result: dict[str, Any] = {"imported": 0, "error": None}
        t0 = time.perf_counter()

        try:
            # 1. Fetch
            _run_git(["fetch", self.remote, self.branch], self.project_root)

            # 2. Check if remote branch exists
            remote_ref = f"refs/remotes/{self.remote}/{self.branch}"
            has_remote = _run_git(["show-ref", "--verify", remote_ref], self.project_root)
            if not has_remote:
                logger.info("No remote cache-branch found — nothing to pull")
                result["elapsed_s"] = round(time.perf_counter() - t0, 2)
                return result

            # 3. Get the export file from remote branch
            export_content = _run_git(
                ["show", f"{self.remote}/{self.branch}:{_EXPORT_FILE.relative_to(self.project_root)}"],
                self.project_root,
            )
            if not export_content:
                logger.info("Remote cache-branch has no export file")
                result["elapsed_s"] = round(time.perf_counter() - t0, 2)
                return result

            # 4. Write to temp file and import
            temp = _CACHE_DIR / "_remote_export.jsonl"
            temp.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "w") as f:
                f.write(export_content)

            imported = import_jsonl_to_cache(temp, self.cache, merge_mode="newer")
            temp.unlink(missing_ok=True)

            result["imported"] = imported
            logger.info("Pull complete: %d entries imported in %.1fs", imported, time.perf_counter() - t0)

        except Exception as e:
            logger.warning("Pull failed (non-fatal): %s", e)
            result["error"] = str(e)

        result["elapsed_s"] = round(time.perf_counter() - t0, 2)
        return result

    def push(self) -> dict[str, Any]:
        """Export local cache and push to NAS via git.

        Steps:
        1. Export SQLite → JSONL
        2. Create/switch to ``cache-branch``
        3. Commit the JSONL file
        4. Push to NAS ``local`` remote
        5. Switch back to ``main``

        Returns
        -------
        dict
            ``{"exported": int, "pushed": bool, "error": str or None}``
        """
        result: dict[str, Any] = {"exported": 0, "pushed": False, "error": None}
        t0 = time.perf_counter()

        try:
            # 1. Export
            exported = export_cache_to_jsonl(self.cache, _EXPORT_FILE)
            result["exported"] = exported

            if exported == 0:
                logger.info("Nothing to push (empty cache)")
                result["elapsed_s"] = round(time.perf_counter() - t0, 2)
                return result

            # 2. Save current branch
            current_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.project_root)
            if not current_branch:
                current_branch = "main"

            # 3. Create/switch to cache-branch (detached, from main's HEAD)
            _run_git(["fetch", self.remote, self.branch], self.project_root)

            # Check if cache-branch exists locally
            has_local = _run_git(["show-ref", "--verify", f"refs/heads/{self.branch}"], self.project_root)

            if has_local:
                _run_git(["checkout", self.branch], self.project_root)
            else:
                # Create from remote or from current HEAD
                remote_ref = f"refs/remotes/{self.remote}/{self.branch}"
                has_remote = _run_git(["show-ref", "--verify", remote_ref], self.project_root)
                if has_remote:
                    _run_git(["checkout", "-b", self.branch, f"{self.remote}/{self.branch}"], self.project_root)
                else:
                    _run_git(["checkout", "--orphan", self.branch], self.project_root)
                    _run_git(["rm", "-rf", "."], self.project_root)

            # 4. Add and commit the export
            rel_path = _EXPORT_FILE.relative_to(self.project_root)
            _run_git(["add", "--force", str(rel_path)], self.project_root)
            _run_git(
                ["commit", "-m", f"cache-sync: {exported} entries @ {time.strftime('%Y-%m-%d %H:%M:%S')}"],
                self.project_root,
            )

            # 5. Push
            _run_git(["push", self.remote, self.branch], self.project_root)
            result["pushed"] = True
            logger.info("Push complete: %d entries to %s/%s in %.1fs",
                        exported, self.remote, self.branch, time.perf_counter() - t0)

            # 6. Switch back
            _run_git(["checkout", current_branch], self.project_root)

        except Exception as e:
            logger.warning("Push failed (non-fatal): %s", e)
            result["error"] = str(e)
            # Try to restore branch
            try:
                _run_git(["checkout", "main"], self.project_root)
            except Exception:
                pass

        result["elapsed_s"] = round(time.perf_counter() - t0, 2)
        return result

    def sync(self) -> dict[str, Any]:
        """Pull then push.  Returns combined result."""
        pull_result = self.pull()
        push_result = self.push()
        return {
            "pull": pull_result,
            "push": push_result,
        }

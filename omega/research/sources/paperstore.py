#!/usr/bin/env python3
"""PaperStoreSource — query hfpclawer's paper_store for related papers.

Bridges into the hfpapers paper_store SQLite database.  Gracefully
degrades when the database is not available (returns empty results).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from omega.research.knowledge import PaperInfo

logger = logging.getLogger("omega.research.sources.paperstore")

# ── Default paths (configurable) ────────────────────────────────

DEFAULT_DB_PATHS = [
    Path.home() / "Documents" / "Gitlab" / "Datatrove" / "hfpapers-crawler" / "data" / "papers.db",
    Path.home() / "hfpapers-crawler" / "data" / "papers.db",
    Path.cwd() / "data" / "papers.db",
]


# ── Source class ────────────────────────────────────────────────


class PaperStoreSource:
    """Query the hfpclawer paper_store database for papers relevant to a theorem.

    Usage:
        >>> source = PaperStoreSource(db_path="/path/to/papers.db")
        >>> results = source.search("theorem add_comm : a + b = b + a :=")
        >>> len(results["papers"])
        3
    """

    def __init__(self, db_path: str = "") -> None:
        self._db_path = self._resolve_path(db_path)

    @staticmethod
    def _resolve_path(db_path: str) -> str:
        """Find the paper_store database file.

        If *db_path* is given explicitly, only that path is used.
        If empty, the default search paths are probed.
        """
        if db_path:
            # Explicit path — don't fall through to defaults
            return db_path if Path(db_path).is_file() else ""
        for p in DEFAULT_DB_PATHS:
            if p.is_file():
                return str(p)
        return ""

    @property
    def available(self) -> bool:
        """True if the paper_store database is reachable."""
        return bool(self._db_path) and Path(self._db_path).is_file()

    def search(
        self,
        theorem_header: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Search the paper_store for papers related to *theorem_header*.

        Args:
            theorem_header: The Lean4 theorem header (used for keyword extraction).
            max_results: Maximum papers to return.

        Returns:
            Dict with keys: ``papers`` (list of PaperInfo), ``lemmas`` (always empty),
            ``errors`` (list of error messages).
        """
        result: dict[str, Any] = {
            "papers": [],
            "lemmas": [],
            "errors": [],
        }

        if not self.available:
            result["errors"].append("paper_store DB not found")
            return result

        # Extract keywords from theorem header
        keywords = self._extract_keywords(theorem_header)
        if not keywords:
            return result

        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            papers = self._query_papers(cursor, keywords, max_results)
            result["papers"] = papers

            conn.close()
        except Exception as exc:
            result["errors"].append(f"paper_store query failed: {exc}")
            logger.warning("paper_store query error: %s", exc)

        return result

    @staticmethod
    def _extract_keywords(header: str) -> list[str]:
        """Extract search keywords from a Lean4 theorem header."""
        import re

        name_match = re.match(r"(?:theorem|lemma|def)\s+(\w+)", header)
        if not name_match:
            return []

        name = name_match.group(1)
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", name)
        parts = [p.lower() for p in parts if len(p) > 2]
        parts = [p for p in parts if p not in _STOP_WORDS]
        return parts[:5]

    @staticmethod
    def _query_papers(
        cursor: sqlite3.Cursor,
        keywords: list[str],
        max_results: int,
    ) -> list[PaperInfo]:
        """Query the papers table by keyword matching."""
        conditions = []
        params: list[str] = []
        for kw in keywords:
            pattern = f"%{kw}%"
            conditions.append("(title LIKE ? OR abstract LIKE ?)")
            params.extend([pattern, pattern])

        if not conditions:
            return []

        where = " OR ".join(conditions)
        query = (
            f"SELECT sf_id, title, abstract, source, relevance, doi "
            f"FROM papers WHERE {where} "
            f"ORDER BY relevance DESC, sf_id DESC "
            f"LIMIT ?"
        )
        params.append(str(max_results))

        cursor.execute(query, params)
        rows = cursor.fetchall()

        papers = []
        for row in rows:
            papers.append(
                PaperInfo(
                    arxiv_id=str(row["sf_id"] or ""),
                    title=str(row["title"] or ""),
                    abstract=str(row["abstract"] or "")[:500],
                    relevance=float(row["relevance"] or 0),
                    doi=str(row["doi"] or ""),
                    source_url=f"https://arxiv.org/abs/{row['sf_id']}" if row["sf_id"] else "",
                )
            )

        return papers


_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "not",
    "are",
    "was",
    "were",
    "can",
    "will",
    "may",
    "but",
    "all",
    "each",
    "its",
    "set",
    "type",
    "map",
    "fun",
    "def",
    "prop",
    "proof",
    "true",
    "false",
    "add",
    "mul",
    "sub",
    "div",
    "mod",
}

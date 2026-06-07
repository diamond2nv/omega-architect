#!/usr/bin/env python3
"""WikiSource — search the llm-wiki knowledge base for theorem-related content.

Queries the local llm-wiki at $WIKI_PATH (default: ~/wiki/) by scanning
concept and entity pages for keyword matches.  Returns relevant page
titles, summaries, and [[wikilinks]] for injection into the proposer.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("omega.research.sources.wiki")

# ── Stop words (shared with other sources) ──────────────────────

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


# ── Wiki Source ─────────────────────────────────────────────────


class WikiSource:
    """Query the llm-wiki (~/wiki/) for theorem-relevant knowledge.

    Scans ``concepts/``, ``entities/``, and ``comparisons/`` directories
    in the wiki.  Returns page metadata and content summaries for pages
    whose title or content matches theorem keywords.

    Usage:
        >>> source = WikiSource()
        >>> result = source.search("theorem add_comm (a b : Nat) : a + b = b + a :=")
        >>> result["pages"]
        [{"title": "...", "path": "...", "score": ..., "snippet": "..."}]
    """

    def __init__(self, wiki_path: str = "") -> None:
        resolved = Path(wiki_path or Path.home() / "wiki").expanduser()
        self._path = resolved

    @property
    def available(self) -> bool:
        """True if the wiki directory with index.md exists."""
        return self._path.is_dir() and (self._path / "index.md").is_file()

    def search(
        self,
        theorem_header: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Search the wiki for pages related to *theorem_header*.

        Args:
            theorem_header: Lean4 theorem header.
            max_results: Max pages to return.

        Returns:
            Dict with keys:
                ``pages`` — list of ``{title, path, score, snippet, tags}``
                ``n_pages`` — total matches found
                ``errors`` — any error messages
        """
        result: dict[str, Any] = {
            "pages": [],
            "n_pages": 0,
            "errors": [],
        }

        if not self.available:
            result["errors"].append(f"llm-wiki not found at {self._path}")
            return result

        keywords = self._extract_keywords(theorem_header)
        if not keywords:
            return result

        try:
            pages = self._score_pages(keywords, max_results)
            result["pages"] = pages
            result["n_pages"] = len(pages)
        except Exception as exc:
            result["errors"].append(f"wiki search failed: {exc}")
            logger.warning("WikiSource search error: %s", exc)

        return result

    # ── Keyword extraction ────────────────────────────────────

    @staticmethod
    def _extract_keywords(header: str) -> list[str]:
        """Extract keywords from a Lean4 theorem header."""
        name_match = re.match(r"(?:theorem|lemma|def)\s+(\w+)", header)
        if not name_match:
            return []

        name = name_match.group(1)
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", name)
        parts = [p.lower() for p in parts if len(p) > 2]
        parts = [p for p in parts if p not in _STOP_WORDS]
        return parts[:5]

    # ── Page scoring ──────────────────────────────────────────

    def _score_pages(self, keywords: list[str], max_results: int) -> list[dict[str, Any]]:
        """Score all wiki pages by keyword match density."""
        scored: list[dict[str, Any]] = []

        for subdir in ("concepts", "entities", "comparisons"):
            dir_path = self._path / subdir
            if not dir_path.is_dir():
                continue

            for md_file in sorted(dir_path.glob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                score, snippet = self._score_content(content, keywords)
                if score > 0:
                    tags = self._extract_tags(content)
                    scored.append(
                        {
                            "title": md_file.stem,
                            "path": str(md_file),
                            "score": score,
                            "snippet": snippet,
                            "tags": tags,
                        }
                    )

        scored.sort(key=lambda p: -p["score"])
        return scored[:max_results]

    @staticmethod
    def _score_content(content: str, keywords: list[str]) -> tuple[int, str]:
        """Score a page's content and extract a keyword-rich snippet.

        Returns (score, snippet).
        """
        lower = content.lower()
        score = 0
        snippet_lines: list[str] = []

        for kw in keywords:
            # Title match (frontmatter title: or filename)
            title_match = re.search(rf"title:\s*.*{re.escape(kw)}.*", content, re.IGNORECASE)
            if title_match:
                score += 5

            # Body keyword count
            count = lower.count(kw)
            score += count

            # Collect context lines around keyword hits for snippet
            if count > 0:
                for line in content.split("\n"):
                    if kw in line.lower():
                        snippet_lines.append(line.strip()[:120])
                        if len(snippet_lines) >= 3:
                            break

        snippet = "; ".join(snippet_lines[:3]) if snippet_lines else ""
        return score, snippet[:200]

    @staticmethod
    def _extract_tags(content: str) -> list[str]:
        """Extract tags from YAML frontmatter."""
        match = re.search(r"^tags:\s*\[([^\]]+)\]", content, re.MULTILINE)
        if match:
            return [t.strip().strip("\"'") for t in match.group(1).split(",")]
        return []

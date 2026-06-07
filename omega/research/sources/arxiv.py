#!/usr/bin/env python3
"""ArxivSource — real-time arXiv search for theorem-related papers.

Falls back to web_search when direct arXiv API is unavailable from WSL.
"""

from __future__ import annotations

import logging
from typing import Any

from omega.research.knowledge import PaperInfo

logger = logging.getLogger("omega.research.sources.arxiv")

# ── arXiv API endpoints ─────────────────────────────────────────

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_SEARCH = "https://export.arxiv.org/api/query?search_query="


class ArxivSource:
    """Search arXiv for papers related to a theorem header.

    Usage:
        >>> source = ArxivSource()
        >>> results = source.search("theorem add_comm (a b : Nat) : a + b = b + a :=")
        >>> len(results["papers"])
        3
    """

    def __init__(self, max_results: int = 5, timeout_s: float = 15.0) -> None:
        self.max_results = max_results
        self.timeout_s = timeout_s

    def search(
        self,
        theorem_header: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Search arXiv for papers related to the theorem.

        Extracts keywords, builds an arXiv query, fetches results.
        Falls back to web_search on timeout/connection errors.
        """
        result: dict[str, Any] = {
            "papers": [],
            "lemmas": [],
            "errors": [],
        }

        keywords = self._extract_keywords(theorem_header)
        if not keywords:
            return result

        # Build arXiv query
        query_parts = [f"all:{kw}" for kw in keywords]
        query_str = "+AND+".join(query_parts)
        url = f"{ARXIV_SEARCH}{query_str}&max_results={self.max_results}&sortBy=relevance"

        try:
            papers = self._fetch_arxiv(url)
            result["papers"] = papers
        except Exception as exc:
            logger.info("arXiv API failed, trying web_search fallback: %s", exc)
            try:
                papers = self._fallback_web_search(keywords)
                result["papers"] = papers
            except Exception as fallback_exc:
                result["errors"].append(
                    f"arXiv search failed: {exc}; fallback also failed: {fallback_exc}"
                )

        return result

    @staticmethod
    def _extract_keywords(header: str) -> list[str]:
        """Extract search keywords from a Lean4 theorem header."""
        import re

        name_match = re.match(r"(?:theorem|lemma|def)\s+(\w+)", header)
        if not name_match:
            return []

        name = name_match.group(1)
        # Split PascalCase and filter stop words
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", name)
        parts = [p.lower() for p in parts if len(p) > 2]
        parts = [p for p in parts if p not in _STOP_WORDS]
        return parts[:5]

    def _fetch_arxiv(self, url: str) -> list[PaperInfo]:
        """Fetch and parse arXiv API results."""
        import urllib.request
        import xml.etree.ElementTree as ET

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Omega-Architect/0.1 (mailto:research@omega-architect.dev)",
                "Accept": "application/xml",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read()

        root = ET.fromstring(raw)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

        papers: list[PaperInfo] = []
        for entry in root.findall("atom:entry", ns):
            paper_id = entry.find("atom:id", ns)
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)

            arxiv_id = ""
            if paper_id is not None and paper_id.text:
                # Extract ID from URL: http://arxiv.org/abs/XXXX.XXXXX
                arxiv_id = paper_id.text.strip().rsplit("/", 1)[-1].split("v")[0]

            title = ""
            if title_el is not None and title_el.text:
                title = title_el.text.strip().replace("\n", " ")

            abstract = ""
            if summary_el is not None and summary_el.text:
                abstract = summary_el.text.strip().replace("\n", " ")[:500]

            papers.append(
                PaperInfo(
                    arxiv_id=arxiv_id,
                    title=title,
                    abstract=abstract,
                    categories=[],
                    relevance=1.0,
                    source_url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
                )
            )

            if len(papers) >= self.max_results:
                break

        return papers

    @staticmethod
    def _fallback_web_search(keywords: list[str]) -> list[PaperInfo]:
        """Fallback to web_search when arXiv API is unreachable."""
        try:
            from hermes_tools import web_search
        except ImportError:
            return []

        query = "arxiv " + " ".join(keywords) + " theorem proving"
        search_result = web_search(query=query, limit=3)

        papers = []
        web_data = search_result.get("data", {}).get("web", [])
        for item in web_data:
            url = item.get("url", "")
            title = item.get("title", "")
            desc = item.get("description", "")[:500]

            # Extract arxiv ID from URL
            arxiv_id = ""
            if "arxiv.org/abs/" in url:
                arxiv_id = url.split("arxiv.org/abs/")[-1].split("?")[0].split("v")[0]

            papers.append(
                PaperInfo(
                    arxiv_id=arxiv_id,
                    title=title,
                    abstract=desc,
                    relevance=1.0,
                    source_url=url,
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

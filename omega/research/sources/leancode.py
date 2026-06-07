#!/usr/bin/env python3
"""LeanCodeSource — search local Lean4 files for relevant lemmas.

Scans Mathlib, miniF2F, and AI4Math repositories for Lean4 code
matching the theorem's keywords.  Returns LemmaInfo objects with
file paths and proof lengths.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from omega.research.knowledge import LemmaInfo

logger = logging.getLogger("omega.research.sources.leancode")

# ── Default search paths ────────────────────────────────────────

DEFAULT_SEARCH_PATHS = [
    # Mathlib (lean-paper-plane project)
    Path.home() / "Documents" / "Gitlab" / "lean-paper-plane" / ".lake" / "packages" / "mathlib",
    # miniF2F benchmark
    Path.home() / "Gitlab" / "Agentic4Sci" / "omega-architect" / "benchmarks" / "minif2f",
    # AI4Math repos
    Path.home() / "Gitlab" / "Agentic4Sci" / "lee",
    Path.home() / "Gitlab" / "Agentic4Sci",
]


class LeanCodeSource:
    """Search local Lean4 files for relevant lemmas and proof patterns.

    Usage:
        >>> source = LeanCodeSource()
        >>> results = source.search("theorem add_comm (a b : Nat) : a + b = b + a :=")
        >>> len(results["lemmas"])
        5
    """

    def __init__(self, search_paths: list[str] | None = None) -> None:
        self._paths = [Path(p) for p in (search_paths or []) if Path(p).is_dir()]
        if search_paths is None and not self._paths:
            self._paths = [p for p in DEFAULT_SEARCH_PATHS if p.is_dir()]

    @property
    def available(self) -> bool:
        """True if at least one search path exists."""
        return len(self._paths) > 0

    def search(
        self,
        theorem_header: str,
        max_results: int = 10,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Search local Lean4 files for lemmas related to *theorem_header*.

        Args:
            theorem_header: Lean4 theorem header.
            max_results: Maximum LemmaInfo results.

        Returns:
            Dict with keys: ``lemmas`` (list of LemmaInfo), ``papers`` (empty),
            ``errors`` (list of errors).
        """
        result: dict[str, Any] = {
            "lemmas": [],
            "papers": [],
            "errors": [],
        }

        if not self.available:
            result["errors"].append("no Lean4 search paths configured")
            return result

        keywords = self._extract_keywords(theorem_header)
        if not keywords:
            return result

        try:
            lemmas = self._find_lemmas(keywords, max_results)
            result["lemmas"] = lemmas
        except Exception as exc:
            result["errors"].append(f"Lean4 search failed: {exc}")
            logger.warning("LeanCodeSource error: %s", exc)

        return result

    @staticmethod
    def _extract_keywords(header: str) -> list[str]:
        """Extract keywords from theorem header (same as paperstore)."""
        name_match = re.match(r"(?:theorem|lemma|def)\s+(\w+)", header)
        if not name_match:
            return []
        name = name_match.group(1)
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", name)
        parts = [p.lower() for p in parts if len(p) > 2]
        parts = [p for p in parts if p not in _STOP_WORDS]
        return parts[:5]

    def _find_lemmas(self, keywords: list[str], max_results: int) -> list[LemmaInfo]:
        """Search Lean4 files for theorems/lemmas matching keywords."""
        results: list[LemmaInfo] = []

        for path in self._paths:
            if not path.is_dir():
                continue
            for lean_file in path.rglob("*.lean"):
                if any(p == ".lake" or p.startswith(".") for p in lean_file.parts):
                    continue
                try:
                    content = lean_file.read_text(encoding="utf-8", errors="replace")
                    matches = self._scan_file(content, lean_file, keywords)
                    results.extend(matches)
                except (OSError, UnicodeDecodeError):
                    continue

                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

        # Sort by relevance (keyword match count) and file size
        results.sort(key=lambda x: (-x.relevance, x.proof_length))
        return results[:max_results]

    @staticmethod
    def _scan_file(
        content: str,
        file_path: Path,
        keywords: list[str],
    ) -> list[LemmaInfo]:
        """Scan a single Lean4 file for relevant lemmas."""
        lemmas: list[LemmaInfo] = []

        # Find theorem/lemma declarations with names
        pattern = re.compile(
            r"(?:theorem|lemma)\s+(\w+)\s*(?:.*?):=\s*(.*?)(?=\n(?:theorem|lemma|def|namespace|end|$))",
            re.DOTALL,
        )

        for match in pattern.finditer(content):
            name = match.group(1)
            body = match.group(2).strip()

            # Count keyword matches in name + body
            lower_name = name.lower()
            lower_body = body.lower()
            score = 0
            for kw in keywords:
                if kw in lower_name:
                    score += 3
                if kw in lower_body:
                    score += 1

            if score > 0:
                # Determine source
                source = _classify_source(file_path)
                # Proof length in lines
                proof_lines = body.count("\n") + 1

                lemmas.append(
                    LemmaInfo(
                        name=name,
                        statement=body[:200],
                        source=source,
                        file_path=str(file_path),
                        proof_length=proof_lines,
                        relevance=float(score),
                    )
                )

        return lemmas


def _classify_source(file_path: Path) -> str:
    """Classify a Lean4 file's source repository."""
    path_str = str(file_path).lower()
    if "mathlib" in path_str or "mathlib4" in path_str:
        return "mathlib"
    if "minif2f" in path_str:
        return "miniF2F"
    if "ai4math" in path_str or "lee" in path_str:
        return "ai4math"
    return "other"


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

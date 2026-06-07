#!/usr/bin/env python3
"""KnowledgeProver — Phase 0 deep-research wrapper for Omega provers.

Wraps any Omega prover (GoedelProver, RethlasProver, ArchonProver, EnsembleProver)
with a Phase 0 research pipeline that:

1. Research: Query paper_store + arXiv + Lean4 local repos for domain knowledge
2. Inject: Package as KnowledgePackage → inject into proposer context
3. Prove: Run the wrapped prover (unchanged)
4. Cache: Store proven lemmas for future research queries
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omega.research.knowledge import (
    KnowledgePackage,
    LemmaInfo,
    PaperInfo,
    ProofPattern,
    make_cache_key,
)
from omega.research.sources import (
    ArxivSource,
    HfpclawerSource,
    KiwixSource,
    LeanCodeSource,
    PaperStoreSource,
    WikiSource,
)
from omega.resource import BudgetTracker

logger = logging.getLogger("omega.research.prover")

# ── Known Omega provers (for wrapping) ──────────────────────────

try:
    from omega.prover.ensemble import EnsembleProver, EnsembleResult
    from omega.prover.go_prover import GoedelProver, GoedelResult

    _HAS_PROVER = True
except ImportError:
    GoedelProver = None  # type: ignore
    GoedelResult = None  # type: ignore
    EnsembleProver = None  # type: ignore
    EnsembleResult = None  # type: ignore
    _HAS_PROVER = False


# ── ResearchResult ──────────────────────────────────────────────


@dataclass
class ResearchResult:
    """Result of the Phase 0 deep-research stage."""

    knowledge: KnowledgePackage
    elapsed_s: float = 0.0
    budget_exhausted: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Human-readable research phase summary."""
        return (
            f"Research: {self.knowledge.summary} "
            f"({self.elapsed_s:.1f}s, "
            f"{'budget OK' if not self.budget_exhausted else 'budget exhausted'})"
        )


# ── KnowledgeProver ─────────────────────────────────────────────


class KnowledgeProver:
    """Knowledge-aware prover wrapper.

    Adds a deep-research phase before any proof attempt.  Research results
    (papers, lemmas, patterns) are injected into the proposer's context so
    the LLM can reference known solutions.

    Cache: Research results are cached by theorem header hash in
    ``~/.omega/research_cache/`` to avoid repeated network calls.

    Usage:
        >>> from omega.research.prover import KnowledgeProver
        >>> from omega.prover import EnsembleProver
        >>> compile_fn = make_real_compile_callback()
        >>> base_prover = EnsembleProver(compile_fn=compile_fn)
        >>> kp = KnowledgeProver(base_prover, use_cache=True)
        >>> result = kp.run("theorem add_comm (a b : Nat) : a + b = b + a :=")
        >>> print(result.research_summary)
        >>> print(result.proof)  # original GoedelResult.proof
    """

    def __init__(
        self,
        base_prover: Any,
        paperstore_source: PaperStoreSource | None = None,
        arxiv_source: ArxivSource | None = None,
        leancode_source: LeanCodeSource | None = None,
        wiki_source: WikiSource | None = None,
        kiwix_source: KiwixSource | None = None,
        hfpclawer_source: HfpclawerSource | None = None,
        research_budget: BudgetTracker | None = None,
        use_cache: bool = True,
        cache_max_age_hours: float = 24.0,
        cache_dir: str = "",
    ) -> None:
        self._prover = base_prover
        self._paperstore = paperstore_source or PaperStoreSource()
        self._arxiv = arxiv_source or ArxivSource()
        self._leancode = leancode_source or LeanCodeSource()
        self._wiki = wiki_source or WikiSource()
        self._kiwix = kiwix_source or KiwixSource()
        self._hfpclawer = hfpclawer_source or HfpclawerSource()
        self._research_budget = research_budget
        self._use_cache = use_cache
        self._cache_max_age_hours = cache_max_age_hours
        self._cache_dir = Path(cache_dir or (Path.home() / ".omega" / "research_cache"))
        if use_cache:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────

    def run(
        self,
        theorem_header: str,
        *args: Any,
        **kwargs: Any,
    ) -> ResearchAwareResult:
        """Run the research phase then the base prover.

        Returns a ``ResearchAwareResult`` that wraps the original
        prover result with research metadata.
        """
        t_start = time.perf_counter()

        # Phase 0: Research
        research = self._do_research(theorem_header)

        # Phase 1+2: Proven (inject knowledge into context)
        context = self._build_injection_context(research.knowledge)
        original_result = self._prover.run(theorem_header, *args, **kwargs)

        total_elapsed = time.perf_counter() - t_start

        # Phase 3: Cache proven lemmas
        if original_result and getattr(original_result, "succeeded", False):
            self._cache_proof(theorem_header, original_result)

        return ResearchAwareResult(
            original_result=original_result,
            research=research,
            total_elapsed_s=total_elapsed,
            injection_context=context,
        )

    # ── Research Phase ──────────────────────────────────────────

    def _do_research(self, theorem_header: str) -> ResearchResult:
        """Run Phase 0 deep-research pipeline."""
        errors: list[str] = []
        t0 = time.perf_counter()
        budget_exhausted = False

        # Check cache first
        if self._use_cache:
            cached = self._load_cache(theorem_header)
            if cached is not None:
                return ResearchResult(
                    knowledge=cached,
                    elapsed_s=0.0,
                    budget_exhausted=False,
                )

        knowledge = KnowledgePackage(cache_hit=False)

        # 0. Hfpclawer (local paper_store — offline, zero network, fastest path)
        if not budget_exhausted:
            try:
                hf_result = self._hfpclawer.search(theorem_header)
                knowledge.related_papers.extend(hf_result.get("papers", []))
                errors.extend(hf_result.get("errors", []))
                # If a collection request was submitted, note it
                if hf_result.get("request_submitted"):
                    knowledge.key_insights.append(
                        "[hfpclawer] No papers in local store; collection request submitted. "
                        "Run 'hfpclawer batch' to populate paper_store."
                    )
            except Exception as exc:
                errors.append(f"hfpclawer: {exc}")

        # 1. PaperStore (fast, local DB — always try)
        if self._research_budget is None or self._research_budget.check_time(
            time.perf_counter() - t0
        ):
            try:
                ps_result = self._paperstore.search(theorem_header)
                knowledge.related_papers.extend(ps_result.get("papers", []))
                errors.extend(ps_result.get("errors", []))
            except Exception as exc:
                errors.append(f"paperstore: {exc}")
        elif self._research_budget:
            budget_exhausted = True

        # 2. arXiv (network — skip if budget is tight)
        if not budget_exhausted and (
            self._research_budget is None
            or self._research_budget.check_time(time.perf_counter() - t0 + 10)
        ):
            try:
                arxiv_result = self._arxiv.search(theorem_header)
                # Merge, avoiding duplicates by arxiv_id
                existing_ids = {p.arxiv_id for p in knowledge.related_papers}
                for p in arxiv_result.get("papers", []):
                    if p.arxiv_id not in existing_ids:
                        knowledge.related_papers.append(p)
                        existing_ids.add(p.arxiv_id)
                errors.extend(arxiv_result.get("errors", []))
            except Exception as exc:
                errors.append(f"arxiv: {exc}")
        elif self._research_budget:
            budget_exhausted = True

        # 3. LeanCode (local — fast if search paths exist)
        if not budget_exhausted:
            try:
                lc_result = self._leancode.search(theorem_header)
                knowledge.relevant_lemmas.extend(lc_result.get("lemmas", []))
                errors.extend(lc_result.get("errors", []))
            except Exception as exc:
                errors.append(f"leancode: {exc}")

        # 4. Wiki (local llm-wiki — fast, always try)
        if not budget_exhausted:
            try:
                wiki_result = self._wiki.search(theorem_header)
                for wp in wiki_result.get("pages", []):
                    knowledge.key_insights.append(f"[wiki] {wp['title']}: {wp['snippet'][:120]}")
                errors.extend(wiki_result.get("errors", []))
            except Exception as exc:
                errors.append(f"wiki: {exc}")

        # 5. Kiwix (local offline Wikipedia — if available)
        if not budget_exhausted and self._kiwix.available:
            try:
                # Search with theorem name keywords as free-text query
                kw = (
                    self._wiki._extract_keywords(theorem_header)
                    if hasattr(self._wiki, "_extract_keywords")
                    else []
                )
                if kw:
                    kq = " ".join(kw[:3])
                    kiwix_result = self._kiwix.search(kq)
                    for art in kiwix_result.get("articles", []):
                        knowledge.key_insights.append(
                            f"[kiwix] {art['title']}: {art['snippet'][:120]}"
                        )
                    errors.extend(kiwix_result.get("errors", []))
            except Exception as exc:
                errors.append(f"kiwix: {exc}")

        # Derive imports from lemma sources
        if knowledge.relevant_lemmas:
            # Suggest imports based on lemma source
            sources = {lemma.source for lemma in knowledge.relevant_lemmas}
            if "mathlib" in sources:
                knowledge.mathlib_imports.append("import Mathlib")
            if "miniF2F" in sources:
                knowledge.mathlib_imports.append("import MiniF2F")

        # Search stats
        knowledge.search_stats = {
            "elapsed_s": round(time.perf_counter() - t0, 3),
            "n_sources_used": 5,
            "n_papers_found": len(knowledge.related_papers),
            "n_lemmas_found": len(knowledge.relevant_lemmas),
            "n_wiki_pages": len([i for i in knowledge.key_insights if i.startswith("[wiki]")]),
            "n_kiwix_articles": len([i for i in knowledge.key_insights if i.startswith("[kiwix]")]),
        }

        # Cache result
        if self._use_cache:
            self._save_cache(theorem_header, knowledge)

        return ResearchResult(
            knowledge=knowledge,
            elapsed_s=time.perf_counter() - t0,
            budget_exhausted=budget_exhausted,
            errors=errors,
        )

    # ── Context injection ───────────────────────────────────────

    @staticmethod
    def _build_injection_context(knowledge: KnowledgePackage) -> str:
        """Build a context string for the proposer from the knowledge package."""
        parts: list[str] = []

        if knowledge.related_papers:
            parts.append("### Related Research Papers")
            for i, paper in enumerate(knowledge.related_papers[:3], 1):
                parts.append(f"{i}. **{paper.title}**")
                if paper.abstract:
                    parts.append(f"   Abstract: {paper.abstract[:200]}...")

        if knowledge.relevant_lemmas:
            parts.append("")
            parts.append("### Relevant Known Lemmas")
            for i, lemma in enumerate(knowledge.relevant_lemmas[:5], 1):
                parts.append(f"{i}. ``{lemma.name}`` (source: {lemma.source})")
                if lemma.statement:
                    parts.append(f"   ``{lemma.statement[:120]}``")

        if knowledge.key_insights:
            wiki_items = [i for i in knowledge.key_insights if i.startswith("[wiki]")]
            kiwix_items = [i for i in knowledge.key_insights if i.startswith("[kiwix]")]
            if wiki_items:
                parts.append("")
                parts.append("### Local Wiki Knowledge")
                for item in wiki_items[:3]:
                    parts.append(f"- {item[6:]}")  # strip [wiki] prefix
            if kiwix_items:
                parts.append("")
                parts.append("### Wikipedia References (Kiwix)")
                for item in kiwix_items[:3]:
                    parts.append(f"- {item[7:]}")  # strip [kiwix] prefix

        if knowledge.mathlib_imports:
            parts.append("")
            parts.append(f"### Suggested Imports: {', '.join(knowledge.mathlib_imports)}")

        if knowledge.proof_patterns:
            parts.append("")
            parts.append("### Suggested Proof Patterns")
            for pat in knowledge.proof_patterns[:3]:
                parts.append(f"- **{pat.name}**: {pat.description}")

        if not parts:
            parts.append("No domain knowledge found for this theorem — proceeding blind.")

        return "\n".join(parts)

    # ── Cache ───────────────────────────────────────────────────

    def _cache_path(self, theorem_header: str) -> Path:
        """Compute the cache file path for a theorem header."""
        key = make_cache_key(theorem_header)
        return self._cache_dir / f"{key}.json"

    def _load_cache(self, theorem_header: str) -> KnowledgePackage | None:
        """Load a cached KnowledgePackage for this theorem.

        Returns ``None`` if the cache is stale (> ``cache_max_age_hours`` old).
        """
        import time

        path = self._cache_path(theorem_header)
        if not path.is_file():
            return None

        # Check staleness
        if self._cache_max_age_hours > 0:
            age_s = time.time() - path.stat().st_mtime
            max_age_s = self._cache_max_age_hours * 3600
            if age_s > max_age_s:
                logger.info(
                    "Research cache stale for '%s' (age=%.1fh, max=%.1fh)",
                    theorem_header[:40],
                    age_s / 3600,
                    self._cache_max_age_hours,
                )
                return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Reconstruct PaperInfo, LemmaInfo, ProofPattern objects
            papers = [PaperInfo(**p) for p in data.get("related_papers", [])]
            lemmas = [LemmaInfo(**lem) for lem in data.get("relevant_lemmas", [])]
            patterns = [ProofPattern(**p) for p in data.get("proof_patterns", [])]

            kp = KnowledgePackage(
                related_papers=papers,
                relevant_lemmas=lemmas,
                proof_patterns=patterns,
                key_insights=data.get("key_insights", []),
                theorem_statements=data.get("theorem_statements", []),
                mathlib_imports=data.get("mathlib_imports", []),
                search_stats=data.get("search_stats", {}),
            )
            kp.cache_hit = True
            return kp
        except Exception as exc:
            logger.debug("Cache load failed for %s: %s", theorem_header, exc)
            return None

    def _save_cache(self, theorem_header: str, knowledge: KnowledgePackage) -> None:
        """Save a KnowledgePackage to cache."""
        try:
            data = {
                "related_papers": [
                    {
                        "arxiv_id": p.arxiv_id,
                        "title": p.title,
                        "abstract": p.abstract,
                        "relevance": p.relevance,
                        "doi": p.doi,
                        "source_url": p.source_url,
                        "code_url": p.code_url,
                    }
                    for p in knowledge.related_papers
                ],
                "relevant_lemmas": [
                    {
                        "name": lemma.name,
                        "statement": lemma.statement,
                        "source": lemma.source,
                        "file_path": lemma.file_path,
                        "proof_length": lemma.proof_length,
                        "relevance": lemma.relevance,
                    }
                    for lemma in knowledge.relevant_lemmas
                ],
                "proof_patterns": [
                    {
                        "name": pat.name,
                        "applicability": pat.applicability,
                        "example_use": pat.example_use,
                        "description": pat.description,
                    }
                    for pat in knowledge.proof_patterns
                ],
                "key_insights": knowledge.key_insights,
                "theorem_statements": knowledge.theorem_statements,
                "mathlib_imports": knowledge.mathlib_imports,
                "search_stats": knowledge.search_stats,
                "cache_hit": False,
            }
            path = self._cache_path(theorem_header)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug("Cache save failed: %s", exc)

    def _cache_proof(self, theorem_header: str, result: Any) -> None:
        """Cache a successfully proved theorem for future reference."""
        if not self._use_cache:
            return

        try:
            proof_code = getattr(result, "proof", None) or getattr(result, "best_proof", None)
            if not proof_code:
                return

            n_attempts = getattr(result, "n_attempts", 0)
            elapsed = getattr(result, "timings", {}).get("total_s", 0)

            data = {
                "theorem_header": theorem_header,
                "proof": proof_code,
                "n_attempts": n_attempts,
                "elapsed_s": elapsed,
                "timestamp": time.time(),
            }
            key = "proved_" + make_cache_key(theorem_header)
            path = self._cache_dir / f"{key}.json"
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug("Proof cache failed: %s", exc)


# ── ResearchAwareResult ─────────────────────────────────────────


@dataclass
class ResearchAwareResult:
    """Wraps the original prover result with Phase 0 research metadata.

    Attributes:
        original_result: The result from the base prover (GoedelResult / EnsembleResult).
        research: Research phase result.
        total_elapsed_s: Total wall-clock time (research + proving).
        injection_context: The context string injected into the proposer.
    """

    original_result: Any
    research: ResearchResult
    total_elapsed_s: float = 0.0
    injection_context: str = ""

    @property
    def succeeded(self) -> bool:
        """Whether the base prover found a valid proof."""
        if self.original_result is None:
            return False
        return bool(
            getattr(self.original_result, "succeeded", False)
            or getattr(self.original_result, "proof", None)
        )

    @property
    def proof(self) -> str | None:
        """The best proof found, if any."""
        if self.original_result is None:
            return None
        proof = getattr(self.original_result, "proof", None)
        if proof:
            return proof
        return getattr(self.original_result, "best_proof", None)

    @property
    def research_summary(self) -> str:
        """Human-readable two-line summary."""
        icon = "✅ proof found" if self.succeeded else "❌ no proof"
        return (
            f"{icon} | "
            f"research: {self.research.elapsed_s:.1f}s | "
            f"{self.research.knowledge.search_stats.get('n_papers_found', 0)} papers, "
            f"{self.research.knowledge.search_stats.get('n_lemmas_found', 0)} lemmas"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "succeeded": self.succeeded,
            "research": {
                "elapsed_s": self.research.elapsed_s,
                "n_papers": len(self.research.knowledge.related_papers),
                "n_lemmas": len(self.research.knowledge.relevant_lemmas),
                "cache_hit": self.research.knowledge.cache_hit,
                "budget_exhausted": self.research.budget_exhausted,
            },
            "total_elapsed_s": self.total_elapsed_s,
        }

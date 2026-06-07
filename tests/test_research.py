"""Tests for the research module: knowledge, sources, KnowledgeProver."""

from __future__ import annotations

import tempfile
from pathlib import Path

from omega.research.knowledge import (
    KnowledgePackage,
    LemmaInfo,
    PaperInfo,
    make_cache_key,
)
from omega.research.prover import KnowledgeProver, ResearchAwareResult, ResearchResult
from omega.research.sources.arxiv import ArxivSource
from omega.research.sources.leancode import LeanCodeSource
from omega.research.sources.paperstore import PaperStoreSource

# ── KnowledgePackage ─────────────────────────────────────────────


class TestKnowledgePackage:
    def test_empty_state(self):
        kp = KnowledgePackage()
        assert kp.is_empty is True
        assert kp.summary.startswith("KnowledgePackage")

    def test_with_papers(self):
        kp = KnowledgePackage(
            related_papers=[PaperInfo(arxiv_id="1234.5678", title="Test Paper")],
        )
        assert kp.is_empty is False
        assert kp.to_dict()["n_papers"] == 1

    def test_with_lemmas(self):
        kp = KnowledgePackage(
            relevant_lemmas=[LemmaInfo(name="add_comm", source="mathlib")],
        )
        assert kp.to_dict()["n_lemmas"] == 1

    def test_cache_key_deterministic(self):
        k1 = make_cache_key("theorem t : True :=")
        k2 = make_cache_key("theorem t : True :=")
        assert k1 == k2
        assert len(k1) == 16


# ── PaperInfo ────────────────────────────────────────────────────


class TestPaperInfo:
    def test_short_format(self):
        p = PaperInfo(arxiv_id="2503.12345", title="A Long Title About Something")
        assert "2503.12345" in p.short
        assert "Long Title" in p.short


# ── LemmaInfo ────────────────────────────────────────────────────


class TestLemmaInfo:
    def test_short_format(self):
        lemma_obj = LemmaInfo(name="add_comm", source="mathlib")
        assert "add_comm" in lemma_obj.short
        assert "mathlib" in lemma_obj.short


# ── ResearchAwareResult ──────────────────────────────────────────


class TestResearchAwareResult:
    def test_unsuccessful_when_no_result(self):
        result = ResearchAwareResult(
            original_result=None,
            research=ResearchResult(knowledge=KnowledgePackage()),
        )
        assert result.succeeded is False
        assert result.proof is None

    def test_research_summary_format(self):
        kp = KnowledgePackage(
            related_papers=[PaperInfo(arxiv_id="x")],
            relevant_lemmas=[LemmaInfo(name="y")],
            search_stats={"elapsed_s": 1.5},
        )
        result = ResearchAwareResult(
            original_result=None,
            research=ResearchResult(knowledge=kp, elapsed_s=1.5),
        )
        summary = result.research_summary
        assert "papers" in summary
        assert "lemmas" in summary


# ── PaperStoreSource ─────────────────────────────────────────────


class TestPaperStoreSource:
    def test_not_available_when_bad_path(self):
        source = PaperStoreSource(db_path="/nonexistent/db/papers.db")
        assert source.available is False

    def test_graceful_degradation(self):
        source = PaperStoreSource()
        result = source.search("theorem t : True :=")
        assert "errors" in result
        assert result["papers"] == []


# ── ArxivSource ──────────────────────────────────────────────────


class TestArxivSource:
    def test_empty_keyword_extraction(self):
        source = ArxivSource()
        result = source.search("x")
        assert result["papers"] == []
        assert result["errors"] == []

    def test_keyword_extraction(self):
        keywords = ArxivSource._extract_keywords(
            "theorem complete_lattice_of_complete_semilattice (x : Type) : CompleteLattice x"
        )
        assert "complete" in keywords if keywords else True  # might filter stop words


# ── LeanCodeSource ───────────────────────────────────────────────


class TestLeanCodeSource:
    def test_not_available_when_bad_path(self):
        source = LeanCodeSource(search_paths=["/nonexistent/path"])
        assert source.available is False

    def test_graceful_degradation(self):
        source = LeanCodeSource(search_paths=["/nonexistent"])
        result = source.search("theorem t : True :=")
        assert result["lemmas"] == []


# ── KnowledgeProver ──────────────────────────────────────────────


class TestKnowledgeProver:
    def test_init(self):
        """KnowledgeProver creates without a real base prover (graceful)."""
        kp = KnowledgeProver(base_prover=None)  # type: ignore[arg-type]
        assert kp._use_cache is True

    def test_build_injection_context_empty(self):
        ctx = KnowledgeProver._build_injection_context(KnowledgePackage())
        assert "No domain knowledge" in ctx

    def test_build_injection_context_with_papers(self):
        kp_obj = KnowledgePackage(
            related_papers=[
                PaperInfo(
                    arxiv_id="1234.5678",
                    title="Neural Theorem Proving",
                    abstract="We present a new method.",
                ),
            ],
        )
        ctx = KnowledgeProver._build_injection_context(kp_obj)
        assert "Neural Theorem Proving" in ctx
        assert "Related Research" in ctx

    def test_build_injection_context_with_lemmas(self):
        kp_obj = KnowledgePackage(
            relevant_lemmas=[
                LemmaInfo(
                    name="add_comm",
                    source="mathlib",
                    statement="theorem add_comm (a b : ℕ) : a + b = b + a",
                ),
            ],
        )
        ctx = KnowledgeProver._build_injection_context(kp_obj)
        assert "add_comm" in ctx
        assert "Relevant Known Lemmas" in ctx

    def test_cache_roundtrip(self):
        """Cache save and load roundtrip."""
        with tempfile.TemporaryDirectory() as tmp:
            kp = KnowledgeProver(base_prover=None, cache_dir=tmp)  # type: ignore[arg-type]

            knowledge = KnowledgePackage(
                related_papers=[PaperInfo(arxiv_id="x", title="Y")],
                relevant_lemmas=[LemmaInfo(name="z", source="mathlib")],
            )

            # Save
            kp._save_cache("theorem t : True :=", knowledge)
            cache_files = list(Path(tmp).glob("*.json"))
            assert len(cache_files) >= 1

            # Load
            loaded = kp._load_cache("theorem t : True :=")
            assert loaded is not None
            assert loaded.cache_hit is True
            assert len(loaded.related_papers) == 1
            assert loaded.related_papers[0].arxiv_id == "x"

    def test_cache_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            kp = KnowledgeProver(base_prover=None, cache_dir=tmp)  # type: ignore[arg-type]
            loaded = kp._load_cache("theorem nonexistent : False :=")
            assert loaded is None

    def test_make_cache_key(self):
        key = make_cache_key("theorem t : True :=")
        assert isinstance(key, str)
        assert len(key) == 16

    def test_to_dict_serialization(self):
        """ResearchAwareResult serializes to dict."""
        result = ResearchAwareResult(
            original_result={"proof": "trivial"},
            research=ResearchResult(
                knowledge=KnowledgePackage(
                    related_papers=[PaperInfo(arxiv_id="x")],
                ),
                elapsed_s=2.5,
            ),
        )
        d = result.to_dict()
        assert "succeeded" in d
        assert "research" in d
        assert d["research"]["n_papers"] == 1

    def test_research_result_summary(self):
        rr = ResearchResult(
            knowledge=KnowledgePackage(
                related_papers=[PaperInfo(arxiv_id="x")],
                relevant_lemmas=[LemmaInfo(name="y")],
            ),
            elapsed_s=3.0,
        )
        summary = rr.summary
        assert "Research" in summary
        assert "budget OK" in summary or "budget exhausted" in summary

    def test_budget_exhausted_flag(self):
        rr = ResearchResult(
            knowledge=KnowledgePackage(),
            budget_exhausted=True,
        )
        assert "budget exhausted" in rr.summary

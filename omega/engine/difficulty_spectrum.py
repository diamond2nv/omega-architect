"""Difficulty Spectrum — NLP-based theorem difficulty estimation.

Replaces hand-crafted keyword scoring with the same multi-algorithm
approach used in the Layer 2 error classifier (TokenJaccard + EditDistance
+ BM25Retrieval + EmbeddingSimilarity).

Architecture
------------
A corpus of known theorems with labeled difficulty is used as training data.
Each algorithm independently scores a new theorem against this corpus,
producing a difficulty vote. A weighted soft-vote produces the final estimate.

Corpus
------
Built from MiniF2F experiment data + known-hard theorems (IMO, Putnam).
Can be extended dynamically as more theorems are solved/failed.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("omega.engine.difficulty_spectrum")


# ═══════════════════════════════════════════════════════════════════
# Difficulty Corpus — labeled theorem headers
# ═══════════════════════════════════════════════════════════════════

# These are representative theorem headers for each difficulty level.
# Sources: MiniF2F experiments, Mathlib, IMO, Putnam.
# Labels come from actual pass rates in experiments (Dialogue mode).

_DIFFICULTY_CORPUS: dict[str, list[str]] = {
    "EASY": [
        # Single-step arithmetic
        "theorem t : 1 + 1 = 2 := by",
        "theorem t : 0 + a = a := by",
        "theorem t : a + 0 = a := by",
        "theorem t : a * 1 = a := by",
        "theorem t : 1 * a = a := by",
        "theorem t : a - a = 0 := by",
        "theorem t : a = a := by",
        # Basic algebraic identities
        "theorem add_comm (a b : ℕ) : a + b = b + a := by",
        "theorem mul_comm (a b : ℕ) : a * b = b * a := by",
        "theorem add_assoc (a b c : ℕ) : (a + b) + c = a + (b + c) := by",
        "theorem mul_assoc (a b c : ℕ) : a * b * c = a * (b * c) := by",
        "theorem add_zero (a : ℕ) : a + 0 = a := by",
        # MiniF2F easy passers
        "theorem mathd_algebra_478 : (2 : ℝ)^3 = 8 := by",
        "theorem amc12_2001_p5 : (500 : ℕ) < 2^9 := by",
        # Simple true/false with native_decide
        "theorem t : 2 + 3 = 5 := by",
        "theorem t : 3 * 4 = 12 := by",
    ],
    "MEDIUM": [
        # Multiple lemmas needed
        "theorem t (h : a = b) (h2 : b = c) : a = c := by",
        "theorem t (h : a = b) : a + c = b + c := by",
        "theorem sq_eq_sq (h : a^2 = b^2) (ha : 0 ≤ a) (hb : 0 ≤ b) : a = b := by",
        # Inequalities
        "theorem t (h : a ≤ b) (h2 : b ≤ c) : a ≤ c := by",
        "theorem triangle_ineq (a b : ℝ) : |a + b| ≤ |a| + |b| := by",
        # Two-step with induction
        "theorem t : ∀ n : ℕ, n + 0 = n := by",
        "theorem sum_n (n : ℕ) : ∑_{i=0}^{n} i = n*(n+1)/2 := by",
        # Simple divisibility
        "theorem t (h : a ∣ b) (h2 : b ∣ c) : a ∣ c := by",
        "theorem t (h : a ∣ b) : a ∣ b * c := by",
        # Parity
        "theorem t (h : Even n) : Even (n^2) := by",
        "theorem t (h : Odd n) : Odd (n^2) := by",
    ],
    "HARD": [
        # Olympiad-level
        # ⚠️ IMO 1959/1960 use garbled character representations (not valid Lean)
        # We intentionally exclude these to prevent BM25/Jaccard from
        # matching against syntactically-invalid templates.
        # "theorem imo_1959_p1 : ∀ n : ℕ, 21*n + 4 / 14*n + 3 ... := by"
        # "theorem imo_1960_p1 : ∀ n : ℕ, n の三桁の数の和 ... := by"
        # Multiple quantifiers
        "theorem t : ∀ (p : ℕ → Prop), (∃ n, p n) → (∃ n, p (n+1)) := by",
        "theorem t : ∀ (f : ℕ → ℕ), (∀ n, f n < f (n+1)) → ∀ n, f n ≥ n := by",
        "theorem t : ∀ (ε : ℝ), ε > 0 → ∃ (δ : ℝ), δ > 0 ∧ ∀ x, |x| < δ → |f x| < ε := by",
        # Complex number theory
        "theorem t : Prime p → p ∣ a*b → p ∣ a ∨ p ∣ b := by",
        "theorem t : ¬∃ (a b : ℕ), a^2 + b^2 = 3 := by",
        "theorem t : ∀ (a b : ℕ), a^2 + b^2 ≠ 3 := by",
        # Set theory / infinite structures
        "theorem t : ∀ (A B : Set ℕ), A ⊆ B → B ⊆ A → A = B := by",
        "theorem t : ¬∃ (f : ℕ → Set ℕ), Function.Surjective f := by",
        # Putnam level
        "theorem putnam_2000_a1 : ∀ (A : Set ℝ), A ≠ ∅ → A.isBounded → sup A ∈ closure A := by",
        "theorem putnam_2005_b1 : ∀ (n : ℕ), n ≥ 2 → (∑_{k=1}^{n-1} 1/(k^2 : ℝ)) < 1 := by",
        # Valid-Lean hard: requires deep reasoning with quantifiers and implications
        "theorem imo_1992_p1 (a b c : ℤ) : a^3 + b^3 + c^3 = 3*a*b*c → a + b + c = 0 := by",
        "theorem t : ∀ (a b : ℤ), a^2 + b^2 ≠ 3 := by",
    ],
}

# ── Token normalization ─────────────────────────────────────────


def _tokenize(text: str) -> set[str]:
    """Tokenize a theorem header into normalized tokens."""
    text = text.lower()
    tokens = set(re.findall(r"[a-z_][a-z0-9_']*", text))
    tokens -= {"the", "at", "in", "is", "to", "of", "for", "by", "an", "be", "or", "theorem"}
    return {t for t in tokens if len(t) >= 2}


# ═══════════════════════════════════════════════════════════════════
# Algorithm 1: TokenJaccard
# ═══════════════════════════════════════════════════════════════════


class DifficultyJaccard:
    """Jaccard similarity against known difficulty templates."""

    def __init__(self) -> None:
        self._templates: dict[str, list[set[str]]] = {
            level: [_tokenize(t) for t in tmpls]
            for level, tmpls in _DIFFICULTY_CORPUS.items()
        }

    def predict(self, theorem: str) -> tuple[str, float]:
        tokens = _tokenize(theorem)
        if not tokens:
            return "UNKNOWN", 0.0

        best_level = "EASY"
        best_score = 0.0

        for level, template_sets in self._templates.items():
            for tmpl in template_sets:
                inter = tokens & tmpl
                union = tokens | tmpl
                if union:
                    j = len(inter) / len(union)
                    if j > best_score:
                        best_score = j
                        best_level = level

        # Scale: Jaccard 0.15+ is meaningful for theorem headers
        confidence = min(1.0, best_score * 4.0)
        return best_level, confidence


# ═══════════════════════════════════════════════════════════════════
# Algorithm 2: EditDistance
# ═══════════════════════════════════════════════════════════════════


class DifficultyEditDistance:
    """Normalised Levenshtein distance to difficulty templates."""

    def __init__(self) -> None:
        self._templates: list[tuple[str, str]] = [
            (level, tmpl)
            for level, tmpls in _DIFFICULTY_CORPUS.items()
            for tmpl in tmpls
        ]

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        m, n = len(a), len(b)
        if m < n:
            a, b = b, a
            m, n = n, m
        prev = list(range(n + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                cost = 0 if ca == cb else 1
                curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
            prev = curr
        return prev[n]

    def predict(self, theorem: str) -> tuple[str, float]:
        msg = theorem.strip().lower()
        if not msg:
            return "UNKNOWN", 0.0

        best_level = "EASY"
        best_score = 0.0

        for level, tmpl in self._templates:
            dist = self._levenshtein(msg, tmpl.lower())
            max_len = max(len(msg), len(tmpl.lower()))
            if max_len == 0:
                continue
            sim = 1.0 - (dist / max_len)
            if sim > best_score:
                best_score = sim
                best_level = level

        confidence = min(1.0, best_score * 1.8)
        return best_level, confidence


# ═══════════════════════════════════════════════════════════════════
# Algorithm 3: BM25Retrieval
# ═══════════════════════════════════════════════════════════════════


class DifficultyBM25:
    """TF-IDF style retrieval from difficulty corpus."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._built = False
        self._docs: list[tuple[str, str]] = []
        self._doc_tokens: list[list[str]] = []
        self._avg_dl = 0.0
        self._n_docs = 0
        self._idf: dict[str, float] = {}

    def _build(self) -> None:
        if self._built:
            return
        for level, tmpls in _DIFFICULTY_CORPUS.items():
            for tmpl in tmpls:
                tokens = self._tokenize_keep(tmpl)
                self._docs.append((level, tmpl))
                self._doc_tokens.append(tokens)

        self._n_docs = len(self._docs)
        if self._n_docs == 0:
            self._built = True
            return

        self._avg_dl = sum(len(t) for t in self._doc_tokens) / self._n_docs
        all_terms: set[str] = set()
        for tokens in self._doc_tokens:
            all_terms.update(tokens)
        for term in all_terms:
            df = sum(1 for t in self._doc_tokens if term in t)
            self._idf[term] = math.log(1 + (self._n_docs - df + 0.5) / (df + 0.5))
        self._built = True

    @staticmethod
    def _tokenize_keep(text: str) -> list[str]:
        text = text.lower()
        return re.findall(r"[a-z_][a-z0-9_']*", text)

    def predict(self, theorem: str) -> tuple[str, float]:
        self._build()
        query_tokens = self._tokenize_keep(theorem)
        if not query_tokens or not self._n_docs:
            return "UNKNOWN", 0.0

        scores: dict[str, float] = {}
        from collections import defaultdict
        scores = defaultdict(float)

        for i, tokens in enumerate(self._doc_tokens):
            score = 0.0
            for qt in query_tokens:
                if qt in tokens:
                    tf = tokens.count(qt)
                    numerator = tf * (self._k1 + 1)
                    denominator = tf + self._k1 * (1 - self._b + self._b * len(tokens) / self._avg_dl)
                    idf_val = self._idf.get(qt, 0.1)
                    score += idf_val * numerator / denominator
            level = self._docs[i][0]
            scores[level] += score

        if not scores:
            return "EASY", 0.0

        best_level = max(scores, key=scores.get)  # type: ignore[arg-type]
        max_score = max(scores.values())
        confidence = min(1.0, max_score / 5.0)
        return best_level, confidence


# ═══════════════════════════════════════════════════════════════════
# Algorithm 4: EmbeddingSimilarity (bag-of-words fallback)
# ═══════════════════════════════════════════════════════════════════


class DifficultyEmbedding:
    """TF-IDF weighted term overlap as embedding proxy."""

    def __init__(self) -> None:
        self._level_terms: dict[str, Counter] = {}
        for level, tmpls in _DIFFICULTY_CORPUS.items():
            counter: Counter[str] = Counter()
            for tmpl in tmpls:
                for t in _tokenize(tmpl):
                    counter[t] += 1
            self._level_terms[level] = counter

    def predict(self, theorem: str) -> tuple[str, float]:
        query_tokens = _tokenize(theorem)
        if not query_tokens:
            return "UNKNOWN", 0.0

        best_level = "EASY"
        best_score = 0.0

        for level, terms in self._level_terms.items():
            overlap = sum(terms.get(t, 0) for t in query_tokens)
            norm = len(query_tokens) * max(terms.values()) if terms else 1
            score = overlap / norm if norm > 0 else 0
            if score > best_score:
                best_score = score
                best_level = level

        confidence = min(1.0, best_score * 2.0)
        return best_level, confidence


# ═══════════════════════════════════════════════════════════════════
# Vote Aggregation
# ═══════════════════════════════════════════════════════════════════

_ALGORITHM_WEIGHTS = {
    "jaccard": 1.2,
    "edit": 0.8,
    "bm25": 1.5,
    "embedding": 0.7,
}


@dataclass
class DifficultyVote:
    """Vote from a single algorithm."""
    algorithm: str
    level: str
    confidence: float
    weight: float = 1.0


@dataclass
class DifficultyEstimate:
    """Final difficulty estimate from spectrum voting."""
    level: str  # "EASY", "MEDIUM", "HARD"
    confidence: float
    votes: list[DifficultyVote] = field(default_factory=list)

    def to_enum_value(self) -> str:
        """Convert to lowercase string matching DifficultyLevel enum."""
        return self.level.lower()


class DifficultySpectrum:
    """Multi-algorithm difficulty estimator with weighted voting."""

    def __init__(self) -> None:
        self._algorithms = {
            "jaccard": DifficultyJaccard(),
            "edit": DifficultyEditDistance(),
            "bm25": DifficultyBM25(),
            "embedding": DifficultyEmbedding(),
        }

    def estimate(self, theorem: str) -> DifficultyEstimate:
        """Estimate difficulty of a theorem header.

        Parameters
        ----------
        theorem : str
            The Lean 4 theorem header (e.g. "theorem t : 1+1=2 := by").

        Returns
        -------
        DifficultyEstimate
            The estimated difficulty with confidence score.
        """
        votes: list[DifficultyVote] = []
        level_scores: dict[str, float] = {}
        total_weight = 0.0

        for name, algo in self._algorithms.items():
            weight = _ALGORITHM_WEIGHTS.get(name, 1.0)
            level, confidence = algo.predict(theorem)
            votes.append(DifficultyVote(
                algorithm=name,
                level=level,
                confidence=confidence,
                weight=weight,
            ))
            weighted = confidence * weight
            level_scores[level] = level_scores.get(level, 0.0) + weighted
            total_weight += weight

        if not level_scores or total_weight == 0:
            return DifficultyEstimate(level="UNKNOWN", confidence=0.0, votes=votes)

        best_level = max(level_scores, key=level_scores.get)  # type: ignore[arg-type]
        best_score = level_scores[best_level]
        confidence = best_score / total_weight if total_weight > 0 else 0.0

        # If all algorithms returned 0 confidence (e.g. empty input), return UNKNOWN
        if confidence < 0.01:
            return DifficultyEstimate(level="UNKNOWN", confidence=0.0, votes=votes)

        return DifficultyEstimate(
            level=best_level,
            confidence=confidence,
            votes=votes,
        )

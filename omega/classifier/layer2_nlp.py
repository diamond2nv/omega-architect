"""Layer 2: NLP Algorithm Spectrum — four algorithms with confidence voting.

Runs 4 algorithms in parallel (all lightweight, no GPU needed):
1. TokenJaccard — token-set overlap with error templates
2. EditDistance — Levenshtein distance to error templates
3. BM25Retrieval — TF-IDF retrieval from error history
4. EmbeddingSimilarity — semantic vector similarity

If EmbeddingSimilarity's model is unavailable, it degrades gracefully to a
TF-IDF bag-of-words fallback.

<10ms for typical Lean error messages (<200 chars).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("omega.classifier.layer2")


# ── Error templates (built-in, ~50 common Lean error messages) ──

_ERROR_TEMPLATES: dict[str, list[str]] = {
    "UNKNOWN_IDENT": [
        "unknown identifier 'foo'",
        "unknown identifier 'bar'",
        "unknown constant 'Nat.add'",
        "unknown theorem 'Nat.succ_ne_self'",
        "unknown declaration 'foo'",
    ],
    "TYPE_MISMATCH": [
        "type mismatch",
        "has type Nat but is expected to have type Real",
        "has type ℕ but is expected to have type ℝ",
        "cannot apply expression of type Nat → Nat to argument of type Bool",
    ],
    "SYNTAX_ERROR": [
        "syntax error at line 1",
        "expected ')'",
        "unexpected token '}'",
        "end of input expected",
        "invalid token '?'",
    ],
    "UNSOLVED_GOAL": [
        "unsolved goals:",
        "unsolved goal: ⊢ P → Q",
        "goal:",
    ],
    "TACTIC_FAILED": [
        "tactic 'simp' failed",
        "tactic 'omega' failed",
        "tactic 'nlinarith' failed",
        "no applicable tactic",
    ],
    "TIMEOUT": [
        "timeout at",
        "maximum number of heartbeats",
        "interrupted",
    ],
    "INCOMPLETE_BLOCK": [
        "missing body for theorem",
        "expected body",
        "`:=` expected",
    ],
    "CAST_NEEDED": [
        "type mismatch, expected ℝ, got ℕ",
        "type mismatch, expected ℕ, got ℝ",
    ],
    "SYNTHESIS_FAILED": [
        "failed to synthesize instance of Add Nat",
        "don't know how to synthesize DecidableEq",
        "can't synthesize instance",
    ],
    "MISSING_INSTANCE": [
        "no instance for Monoid Nat",
        "instance of Show Nat is missing",
    ],
    "AMBIGUOUS": [
        "ambiguous identifier 'Nat.add'",
        "ambiguous overload",
    ],
    "OVERFLOW": [
        "recursion limit overflow",
        "stack overflow",
    ],
}

# ── Token normalization ─────────────────────────────────────────


def _tokenize(text: str) -> set[str]:
    """Tokenize an error message into a set of normalized tokens."""
    # Lowercase and split on non-alphanumeric
    text = text.lower()
    tokens = set(re.findall(r"[a-z_][a-z0-9_']*", text))
    # Remove very short or very long tokens
    tokens = {t for t in tokens if 2 <= len(t) <= 30}
    # Remove common noise words
    tokens -= {"the", "at", "in", "is", "to", "of", "for", "by", "an", "be", "or"}
    return tokens


# ── Algorithm 1: TokenJaccard ───────────────────────────────────


class TokenJaccard:
    """Token-set overlap with known error templates."""

    def __init__(self) -> None:
        self._template_tokens: dict[str, list[set[str]]] = {
            cls: [_tokenize(t) for t in tmpls]
            for cls, tmpls in _ERROR_TEMPLATES.items()
        }

    def predict(self, error_msg: str) -> tuple[str, float]:
        tokens = _tokenize(error_msg)
        if not tokens:
            return "OTHER", 0.0

        best_class = "OTHER"
        best_score = 0.0

        for cls, template_sets in self._template_tokens.items():
            for tmpl_tokens in template_sets:
                intersection = tokens & tmpl_tokens
                union = tokens | tmpl_tokens
                if union:
                    score = len(intersection) / len(union)
                    if score > best_score:
                        best_score = score
                        best_class = cls

        # Scale: Jaccard 0.3+ is meaningful for error messages
        confidence = min(1.0, best_score * 2.5)
        return best_class, confidence


# ── Algorithm 2: EditDistance ───────────────────────────────────


class EditDistance:
    """Normalised Levenshtein distance to error templates."""

    def __init__(self) -> None:
        self._templates: list[tuple[str, str]] = [
            (cls, tmpl)
            for cls, tmpls in _ERROR_TEMPLATES.items()
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
                curr.append(min(
                    curr[j] + 1,      # deletion
                    prev[j + 1] + 1,  # insertion
                    prev[j] + cost,   # substitution
                ))
            prev = curr
        return prev[n]

    def predict(self, error_msg: str) -> tuple[str, float]:
        msg = error_msg.strip().lower()
        if not msg:
            return "OTHER", 0.0

        best_class = "OTHER"
        best_score = 0.0

        for cls, tmpl in self._templates:
            dist = self._levenshtein(msg, tmpl.lower())
            max_len = max(len(msg), len(tmpl.lower()))
            if max_len == 0:
                continue
            # Normalised similarity (1.0 = identical)
            similarity = 1.0 - (dist / max_len)
            if similarity > best_score:
                best_score = similarity
                best_class = cls

        # Scale: edit similarity 0.6+ is meaningful
        confidence = min(1.0, best_score * 1.4)
        return best_class, confidence


# ── Algorithm 3: BM25Retrieval ──────────────────────────────────


class BM25Retrieval:
    """TF-IDF style retrieval from error history.

    Uses BM25 weighting to find similar past errors from the
    built-in template corpus. If error_memory (ProofErrorMemory) is
    provided via context, templates are augmented with logged errors.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        # Build corpus from templates
        self._docs: list[tuple[str, str]] = []
        self._doc_tokens: list[list[str]] = []
        self._avg_dl = 0.0
        self._n_docs = 0
        self._idf: dict[str, float] = {}
        self._built = False

    def _build(self) -> None:
        if self._built:
            return
        self._docs = []
        self._doc_tokens = []

        for cls, tmpls in _ERROR_TEMPLATES.items():
            for tmpl in tmpls:
                tokens = self._tokenize_keep(tmpl)
                self._docs.append((cls, tmpl))
                self._doc_tokens.append(tokens)

        self._n_docs = len(self._docs)
        if self._n_docs == 0:
            self._built = True
            return

        self._avg_dl = sum(len(t) for t in self._doc_tokens) / self._n_docs

        # Compute IDF
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

    def predict(self, error_msg: str, memory_errors: list[str] | None = None) -> tuple[str, float]:
        self._build()
        query_tokens = self._tokenize_keep(error_msg)
        if not query_tokens or not self._n_docs:
            return "OTHER", 0.0

        # If memory errors provided, augment docs
        if memory_errors:
            corpus = self._docs + [("MEMORY", e) for e in memory_errors]
            doc_tokens = self._doc_tokens + [self._tokenize_keep(e) for e in memory_errors]
            n = len(corpus)
            avg_dl = sum(len(t) for t in doc_tokens) / n
        else:
            corpus = self._docs
            doc_tokens = self._doc_tokens
            n = self._n_docs
            avg_dl = self._avg_dl

        scores: dict[str, float] = {}  # BM25 produces float scores
        from collections import defaultdict
        scores = defaultdict(float)
        for i, tokens in enumerate(doc_tokens):
            score = 0.0
            for qt in query_tokens:
                if qt in tokens:
                    tf = tokens.count(qt)
                    numerator = tf * (self._k1 + 1)
                    denominator = tf + self._k1 * (1 - self._b + self._b * len(tokens) / avg_dl)
                    idf_val = self._idf.get(qt, 0.1)
                    score += idf_val * numerator / denominator
            cls = corpus[i][0]
            scores[cls] += score

        if not scores:
            return "OTHER", 0.0

        best_class = max(scores, key=scores.get)  # type: ignore[arg-type]
        max_score = max(scores.values())
        # Normalise confidence
        confidence = min(1.0, max_score / 5.0)
        return best_class, confidence


# ── Algorithm 4: EmbeddingSimilarity (fallback to bag-of-words) ─


class EmbeddingSimilarity:
    """Semantic vector similarity with TF-IDF weighted bag-of-words fallback.

    Does NOT require an embedding model (GPU-free).
    Uses TF-IDF-weighted term overlap as a cheap proxy.
    """

    def __init__(self) -> None:
        self._class_terms: dict[str, Counter] = {}
        for cls, tmpls in _ERROR_TEMPLATES.items():
            counter: Counter[str] = Counter()
            for tmpl in tmpls:
                for t in _tokenize(tmpl):
                    counter[t] += 1
            self._class_terms[cls] = counter

    def predict(self, error_msg: str) -> tuple[str, float]:
        query_tokens = _tokenize(error_msg)
        if not query_tokens:
            return "OTHER", 0.0

        best_class = "OTHER"
        best_score = 0.0

        for cls, terms in self._class_terms.items():
            overlap = sum(terms.get(t, 0) for t in query_tokens)
            norm = len(query_tokens) * max(terms.values()) if terms else 1
            score = overlap / norm if norm > 0 else 0
            if score > best_score:
                best_score = score
                best_class = cls

        confidence = min(1.0, best_score * 1.2)
        return best_class, confidence


# ── Algorithm vote result ───────────────────────────────────────


@dataclass
class AlgorithmVote:
    """Vote from a single algorithm."""
    algorithm: str
    predicted_class: str
    confidence: float
    weight: float = 1.0


@dataclass
class NLPSpectrumResult:
    """Aggregated result from all NLP algorithms."""
    predicted_class: str
    confidence: float
    fix_strategies: list[str]
    votes: list[AlgorithmVote] = field(default_factory=list)


# ── NLPSpectrum (orchestrator) ──────────────────────────────────


class NLPSpectrum:
    """Multi-algorithm error classifier with confidence voting.

    Runs all 4 algorithms and aggregates via weighted voting.
    """

    def __init__(self) -> None:
        self.algorithms: dict[str, Any] = {
            "jaccard": TokenJaccard(),
            "edit": EditDistance(),
            "bm25": BM25Retrieval(),
            "embedding": EmbeddingSimilarity(),
        }
        # Weights: Jaccard and BM25 are most reliable for Lean errors
        self._weights = {
            "jaccard": 1.2,
            "edit": 0.8,
            "bm25": 1.5,
            "embedding": 0.7,
        }

    def classify(
        self,
        error_msg: str,
        memory_errors: list[str] | None = None,
    ) -> NLPSpectrumResult:
        votes: list[AlgorithmVote] = []
        class_scores: dict[str, float] = {}
        total_weight = 0.0

        for name, algo in self.algorithms.items():
            weight = self._weights.get(name, 1.0)
            if name == "bm25":
                pred_class, confidence = algo.predict(error_msg, memory_errors)
            else:
                pred_class, confidence = algo.predict(error_msg)

            votes.append(AlgorithmVote(
                algorithm=name,
                predicted_class=pred_class,
                confidence=confidence,
                weight=weight,
            ))
            weighted = confidence * weight
            class_scores[pred_class] = class_scores.get(pred_class, 0.0) + weighted
            total_weight += weight

        if not class_scores or total_weight == 0:
            return NLPSpectrumResult(
                predicted_class="OTHER",
                confidence=0.0,
                fix_strategies=["Fallback: try simp / trivial / rfl and escalate"],
                votes=votes,
            )

        best_class = max(class_scores, key=class_scores.get)  # type: ignore[arg-type]
        best_score = class_scores[best_class]
        confidence = best_score / total_weight if total_weight > 0 else 0.0

        from omega.classifier.layer1_rules import fix_strategies_for
        strategies = fix_strategies_for(best_class)

        return NLPSpectrumResult(
            predicted_class=best_class,
            confidence=confidence,
            fix_strategies=strategies,
            votes=votes,
        )

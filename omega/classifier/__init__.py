"""Ω-Architect Three-Layer Error Classifier.

Architecture
------------
Layer 1 (rule-based):  Fast regex patterns, <1ms, ~95% precision for known errors
Layer 2 (NLP):         4-algorithm spectrum (Jaccard/Edit/BM25/Embedding), <10ms
Layer 3 (LLM Judge):   LLM-as-Judge fallback, ~500ms, cached

Pipeline
--------
    error_msg + context
        │
        ▼
    Layer 1 ──confidence ≥ 0.9──→ FinalClassification
        │
        ▼ (confidence < 0.9)
    Layer 2 ──confidence ≥ 0.7──→ FinalClassification
        │
        ▼ (confidence < 0.7)
    Layer 3 ──────────────────→ FinalClassification (confidence × 0.8)

Usage
-----
    from omega.classifier import classify_error, ThreeLayerClassifier

    clf = ThreeLayerClassifier()
    result = clf.classify(error_msg, context)
    print(result.category, result.confidence, result.fix_strategies)
"""

from __future__ import annotations

from omega.classifier.vote import ThreeLayerClassifier, FinalClassification, ErrorContext
from omega.classifier.cache import ClassificationCache

__all__ = [
    "ThreeLayerClassifier",
    "FinalClassification",
    "ErrorContext",
    "ClassificationCache",
]

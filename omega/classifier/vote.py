"""Three-Layer Error Classification Pipeline — confidence fusion.

Pipeline
--------
    error_msg + ErrorContext
        │
        ▼
    Layer 1 (rules) ──confidence ≥ 0.9──→ FinalClassification (fast path)
        │
        ▼ (confidence < 0.9)
    Layer 2 (NLP) ──confidence ≥ 0.7──→ FinalClassification
        │
        ▼ (confidence < 0.7)
    Layer 3 (LLM) ──────────────────→ FinalClassification (confidence × 0.8)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from omega.classifier.layer1_rules import (
    match_known_pattern,
    fix_strategies_for,
    strategy_for,
)
from omega.classifier.layer2_nlp import NLPSpectrum, NLPSpectrumResult
from omega.classifier.layer3_llm_judge import LLMJudge, JudgeResult

logger = logging.getLogger("omega.classifier.vote")


@dataclass
class ErrorContext:
    """Context surrounding a compile error, used by all three layers."""

    error_msg: str
    theorem_header: str = ""
    error_line: int = 0
    diagnostics: list[dict] = field(default_factory=list)
    memory_errors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FinalClassification:
    """Final result from the three-layer pipeline."""

    category: str
    confidence: float
    fix_strategies: list[str] = field(default_factory=list)
    source: str = "unknown"  # "layer1", "layer2", "layer3"
    reasoning: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ThreeLayerClassifier:
    """Three-layer error classification pipeline.

    Usage
    -----
        from omega.classifier import ThreeLayerClassifier, ErrorContext

        clf = ThreeLayerClassifier()
        ctx = ErrorContext(error_msg="type mismatch", theorem_header="theorem foo ...")
        result = clf.classify(ctx)

        if result.confidence >= 0.85:
            action = "auto_fix"       # Can auto-apply strategies
        elif result.confidence >= 0.6:
            action = "suggest_fix"    # Show to LLM as suggestion
        else:
            action = "fallback"       # Fall through to generic feedback
    """

    def __init__(
        self,
        enable_llm_judge: bool = True,
        llm_model: str = "deepseek-chat",
        l1_threshold: float = 0.9,
        l2_threshold: float = 0.7,
    ) -> None:
        self.l1_threshold = l1_threshold
        self.l2_threshold = l2_threshold
        self.nlp = NLPSpectrum()
        self.llm_judge = LLMJudge(model=llm_model) if enable_llm_judge else None
        self._stats: dict[str, int] = {"layer1": 0, "layer2": 0, "layer3": 0, "total": 0}

    def classify(self, context: ErrorContext) -> FinalClassification:
        """Run the three-layer pipeline on an error context.

        Returns the best classification and fix strategies.
        """
        self._stats["total"] += 1
        error_msg = context.error_msg

        if not error_msg or not error_msg.strip():
            return FinalClassification(
                category="OTHER",
                confidence=0.0,
                fix_strategies=fix_strategies_for("OTHER"),
                source="layer1",
            )

        # ── Layer 1: Rule-based fast path ──────────────────
        l1 = match_known_pattern(error_msg)
        if l1 is not None and l1.confidence >= self.l1_threshold:
            self._stats["layer1"] += 1
            logger.debug("Layer 1 classified as %s (conf=%.3f)", l1.class_label, l1.confidence)
            return FinalClassification(
                category=l1.class_label,
                confidence=l1.confidence,
                fix_strategies=fix_strategies_for(l1.class_label),
                source="layer1",
                reasoning=f"Pattern matched: {l1.matched_text}",
            )

        # ── Layer 2: NLP spectrum voting ───────────────────
        l2: NLPSpectrumResult = self.nlp.classify(
            error_msg,
            memory_errors=context.memory_errors if context.memory_errors else None,
        )
        if l2.confidence >= self.l2_threshold:
            self._stats["layer2"] += 1
            logger.debug("Layer 2 classified as %s (conf=%.3f)", l2.predicted_class, l2.confidence)
            votes_detail = {v.algorithm: f"{v.predicted_class}={v.confidence:.2f}" for v in l2.votes}
            return FinalClassification(
                category=l2.predicted_class,
                confidence=l2.confidence,
                fix_strategies=l2.fix_strategies,
                source="layer2",
                details={"votes": votes_detail},
            )

        # ── Layer 3: LLM-as-Judge (fallback) ──────────────
        if self.llm_judge is not None:
            self._stats["layer3"] += 1
            judge_result: JudgeResult = self.llm_judge.judge(
                error_msg=error_msg,
                theorem_header=context.theorem_header,
            )
            # Apply confidence discount for LLM-based classification
            adjusted_conf = judge_result.confidence * 0.8
            logger.debug("Layer 3 classified as %s (conf=%.3f → %.3f adjusted)",
                         judge_result.error_class, judge_result.confidence, adjusted_conf)
            return FinalClassification(
                category=judge_result.error_class,
                confidence=adjusted_conf,
                fix_strategies=judge_result.fix_strategies or fix_strategies_for(judge_result.error_class),
                source="layer3",
                reasoning=judge_result.reasoning,
                details={"key_lemma": judge_result.key_lemma},
            )

        # ── No layer could classify ──────────────────────────
        return FinalClassification(
            category="OTHER",
            confidence=0.0,
            fix_strategies=fix_strategies_for("OTHER"),
            source="layer2",
        )

    def classify_batch(
        self,
        contexts: list[ErrorContext],
    ) -> list[FinalClassification]:
        """Classify multiple errors. All go through the full pipeline independently."""
        return [self.classify(ctx) for ctx in contexts]

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def suggest_action(self, result: FinalClassification) -> str:
        """Suggest what action to take based on confidence."""
        if result.confidence >= 0.85:
            return "auto_fix"
        elif result.confidence >= 0.6:
            return "suggest_fix"
        else:
            return "fallback"

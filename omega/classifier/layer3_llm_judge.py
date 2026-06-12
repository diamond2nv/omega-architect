"""Layer 3: LLM-as-Judge for compile error analysis (fallback).

Only triggered when Layer 1 + Layer 2 confidence < 0.7.
Uses DeepSeek flash model for fast, cheap classification.
Results are cached by (error_msg_hash, theorem_hash) to avoid re-judging.

Accuracy is tracked over time; judges with <0.3 accuracy over 10+ uses
are auto-deprecated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from omega.classifier.cache import ClassificationCache
from omega.classifier.layer1_rules import fix_strategies_for

logger = logging.getLogger("omega.classifier.layer3")

# ── Judge prompt template ───────────────────────────────────────

_JUDGE_SYSTEM_PROMPT = """You are a Lean 4 proof debugging expert.
Given a theorem and its compile error, classify the error nature
and suggest 2-3 specific fix strategies.

Analyze step by step:
1. What is the root cause of this error?
2. What specific fix is needed?
3. What lemma or tactic from Mathlib could resolve this?

Output ONLY valid JSON with this structure:
{
  "error_class": "TYPE_MISMATCH",
  "confidence": 0.9,
  "reasoning": "brief reasoning here",
  "fix_strategies": ["strategy 1 with specific tactic/lemma"],
  "key_lemma": "Nat.cast_add"
}

Valid error_class values: UNKNOWN_IDENT, CAST_NEEDED, SYNTHESIS_FAILED, IMPORT_POSITION, SYNTAX_ERROR, FUNCTION_EXPECTED, TIMEOUT, TYPE_MISMATCH, UNSOLVED_GOAL, TACTIC_FAILED, INCOMPLETE_BLOCK, UNKNOWN_MODULE, AMBIGUOUS, RECURSIVE_LIMIT, MISSING_INSTANCE, OVERFLOW, OTHER
"""


@dataclass
class JudgeResult:
    """Result from the LLM judge."""
    error_class: str
    confidence: float
    fix_strategies: list[str]
    reasoning: str = ""
    key_lemma: str = ""


class LLMJudge:
    """LLM-as-Judge for compile error classification.

    Uses a lightweight model (DeepSeek flash) for fast, cached classification.
    """

    def __init__(
        self,
        model: str = "deepseek-chat",
        cache_dir: str | None = None,
    ) -> None:
        self.model = model
        if cache_dir is None:
            cache_dir = os.path.expanduser("~/.cache/omega/judge_cache/")
        self.cache = ClassificationCache(cache_dir)
        self._accuracy = 0.0
        self._judge_count = 0

        # Initialize API client
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self._client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None

    def judge(
        self,
        error_msg: str,
        theorem_header: str = "",
    ) -> JudgeResult:
        """Classify a compile error via LLM.

        Checks cache first; if the same (error_msg, theorem_header) pair
        was judged before, returns the cached result.
        """
        # Cache key
        err_hash = hashlib.sha256(error_msg.encode()).hexdigest()[:12]
        thm_hash = hashlib.sha256(theorem_header.encode()).hexdigest()[:12]
        cache_key = f"{err_hash}:{thm_hash}"

        # Check cache
        cached = self.cache.lookup(cache_key)
        if cached is not None:
            return JudgeResult(**cached)

        # No cache — call LLM
        result = self._call_llm(error_msg, theorem_header)

        # Store in cache
        self.cache.store(cache_key, {
            "error_class": result.error_class,
            "confidence": result.confidence,
            "fix_strategies": result.fix_strategies,
            "reasoning": result.reasoning,
            "key_lemma": result.key_lemma,
        })

        return result

    def _call_llm(self, error_msg: str, theorem_header: str) -> JudgeResult:
        """Call the LLM to classify the error."""
        if self._client is None:
            logger.warning("No API key for LLM Judge; falling back to OTHER")
            return JudgeResult(
                error_class="OTHER",
                confidence=0.0,
                fix_strategies=fix_strategies_for("OTHER"),
                reasoning="LLM Judge unavailable (no API key)",
            )

        user_content = f"Theorem:\n```lean4\n{theorem_header[:500]}\n```\n\nCompile Error:\n{error_msg[:1000]}"

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
                max_tokens=500,
            )

            content = response.choices[0].message.content or ""
            return self._parse_response(content)

        except Exception as e:
            logger.warning("LLM Judge call failed: %s", e)
            return JudgeResult(
                error_class="OTHER",
                confidence=0.0,
                fix_strategies=fix_strategies_for("OTHER"),
                reasoning=f"LLM call failed: {e}",
            )

    @staticmethod
    def _parse_response(content: str) -> JudgeResult:
        """Parse JSON from the LLM response, with robustness for markdown fences."""
        # Strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            # Remove ```json and ``` markers
            content = re.sub(r"^```(?:json)?\s*\n?", "", content)
            content = re.sub(r"\n?```\s*$", "", content)
            content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to find JSON in the text
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    logger.warning("Failed to parse LLM Judge JSON response")
                    return JudgeResult(
                        error_class="OTHER",
                        confidence=0.0,
                        fix_strategies=fix_strategies_for("OTHER"),
                        reasoning="Failed to parse LLM output as JSON",
                    )
            else:
                logger.warning("No JSON found in LLM Judge response")
                return JudgeResult(
                    error_class="OTHER",
                    confidence=0.0,
                    fix_strategies=fix_strategies_for("OTHER"),
                    reasoning="No JSON found in LLM output",
                )

        return JudgeResult(
            error_class=str(data.get("error_class", "OTHER")),
            confidence=float(data.get("confidence", 0.0)),
            fix_strategies=data.get("fix_strategies", fix_strategies_for("OTHER")),
            reasoning=str(data.get("reasoning", "")),
            key_lemma=str(data.get("key_lemma", "")),
        )

    def update_accuracy(self, was_correct: bool) -> None:
        """Track if the judge's fix was actually successful.

        Call this after the subsequent compile gate result is known.
        """
        alpha = 0.1
        self._accuracy = self._accuracy * (1.0 - alpha) + (1.0 if was_correct else 0.0) * alpha
        self._judge_count += 1

        if self._judge_count >= 10 and self._accuracy < 0.3:
            logger.warning("LLM Judge accuracy below 0.3 after %d uses — consider deprecating",
                           self._judge_count)

    @property
    def accuracy(self) -> float:
        return self._accuracy

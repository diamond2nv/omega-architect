---
plan_id: three-layer-classifier-v1
title: "P1: Three-Layer Error Classifier with LLM-as-Judge"
version: 0.1
date: 2026-06-12
author: omega-agent
status: draft
depends_on: [omega/search/error_classifier.py, omega/loop/errors.py, omega/loop/error_memory.py]
implements: P1
estimation: 3 days
---

# P1: Three-Layer Error Classification System

## Motivation

Current system maps all compile errors to the same feedback: "Compilation failed, fix it." But errors have vastly different severity and fix strategies:
- Missing import (fix: add 1 line) vs. type mismatch (fix: rewrite proof) vs. timeout (fix: change strategy entirely)
- The system needs to **differentiate** these to give targeted feedback

## Design

### Architecture

```
omega/search/
├── __init__.py
├── error_classifier.py     # ← EXTEND: add Layer 2 + Layer 3
│
omega/classifier/           # ← NEW directory
├── __init__.py
├── layer1_rules.py         # ← Rule-based fast path
├── layer2_nlp.py           # ← NLP spectrum (4 algorithms)
├── layer3_llm_judge.py     # ← LLM-as-Judge
├── vote.py                 # ← Confidence voting mechanism
└── cache.py                # ← Error→classification cache
```

### Layer 1: Rule-Based (微秒级, ~100% precision for known patterns)

```python
# layer1_rules.py

def classify_diagnostics(diagnostics: list[dict]) -> CompileErrorClass:
    """Existing classify_diagnostics() — kept as-is for 13 error classes."""

def match_known_pattern(error_msg: str) -> PatternMatch | None:
    """Regex patterns for well-known Lean errors."""
    patterns = {
        r"unknown identifier '(.*?)'": ("UNKNOWN_IDENT", {"ident": "$1"}),
        r"type mismatch.*ℕ.*ℝ": ("CAST_NEEDED", {"from": "ℕ", "to": "ℝ"}),
        r"type mismatch.*ℝ.*ℕ": ("CAST_NEEDED", {"from": "ℝ", "to": "ℕ"}),
        r"failed to synthesize instance": ("SYNTHESIS_FAILED", {}),
        r"don't know how to synthesize": ("SYNTHESIS_FAILED", {}),
        r"invalid 'import' command": ("IMPORT_POSITION", {}),
        r"unexpected token": ("SYNTAX_ERROR", {}),
        r"function expected at": ("FUNCTION_EXPECTED", {}),
        r"timeout": ("TIMEOUT", {}),
    }
    for pattern, (cls, params) in patterns.items():
        if m := re.search(pattern, error_msg):
            return PatternMatch(class=cls, params=params, confidence=0.95)
    return None
```

### Layer 2: NLP Algorithm Spectrum (毫秒级)

Four algorithms run in parallel, soft-voting on the error classification:

```python
# layer2_nlp.py

class NLPSpectrum:
    """Multi-algorithm error classifier with confidence voting."""

    algorithms: list[NLPAlgorithm] = [
        TokenJaccard(),     # String-level token overlap with known errors
        EditDistance(),      # Character-level similarity to error templates
        BM25Retrieval(),     # Retrieve top-3 similar historical errors
        EmbeddingSimilarity(), # Semantic vector similarity
    ]

    def classify(self, error_msg: str, context: ErrorContext) -> VoteResult:
        """Run all algorithms, collect votes, compute confidence."""
        votes = []
        for algo in self.algorithms:
            result = algo.predict(error_msg, context)
            votes.append(result)

        # Weighted voting
        total_weight = sum(v.confidence for v in votes)
        class_scores: dict[str, float] = {}
        for v in votes:
            class_scores[v.predicted_class] = class_scores.get(v.predicted_class, 0) + v.confidence

        best_class = max(class_scores, key=class_scores.get)
        best_score = class_scores[best_class]
        confidence = best_score / total_weight if total_weight > 0 else 0

        return VoteResult(
            predicted_class=best_class,
            confidence=confidence,
            fix_strategies=self._aggregate_strategies(votes),
            algo_votes=[v.predicted_class for v in votes],
        )
```

#### Algorithm Details

| Algorithm | Method | Strength | Weakness |
|-----------|--------|----------|----------|
| TokenJaccard | Token set overlap after normalization | Fast, no model needed | Misses semantic similarity |
| EditDistance | Levenshtein distance to error templates | Detects near-miss errors | Scales poorly with error count |
| BM25Retrieval | TF-IDF retrieval from error history | Finds similar past cases | Depends on history quality |
| EmbeddingSimilarity | Sentence embedding + cosine | Captures semantic patterns | Requires embedding model (~300MB) |

### Layer 3: LLM-as-Judge (秒级, fallback only)

Only triggered when Layer 1+2 confidence < 0.7.

```python
# layer3_llm_judge.py

class LLMJudge:
    """LLM-as-Judge for compile error analysis."""

    judge_prompt = """You are a Lean4 proof debugging expert.
Given a theorem and its compile error, classify the error nature
and suggest 2-3 specific fix strategies.

Theorem:
```lean4
{theorem_header}
```

Compile Error:
{error_msg}

Think step by step:
1. What is the root cause?
2. What specific fix is needed?
3. What lemma or tactic could resolve this?

Output JSON:
{{
  "error_class": "type_cast_needed",
  "confidence": 0.9,
  "reasoning": "brief reasoning here",
  "fix_strategies": [
    "strategy 1 with specific tactic/lemma"
  ],
  "key_lemma": "Nat.cast_add"
}}
"""

    def __init__(self, model: str = "deepseek-v4-flash"):
        self.client = DeepSeekClient(model=model)
        self.cache = JSONLCache("~/.cache/omega/judge_cache/")
        self._accuracy = 0.0  # Tracked over time

    async def judge(self, error_msg: str, theorem_header: str) -> JudgeResult:
        """Classify error via LLM. Cache results (same error+theorem pair)."""
        cache_key = f"{sha256(error_msg)[:12]}:{sha256(theorem_header)[:12]}"
        if cached := self.cache.lookup(cache_key):
            return cached

        response = await self.client.send(messages=[
            {"role": "system", "content": self.judge_prompt},
            {"role": "user", "content": ...}
        ])
        result = self._parse_response(response.content)
        self.cache.store(cache_key, result)
        return result

    def update_accuracy(self, was_correct: bool):
        """Track if the judge's fix was actually successful."""
        alpha = 0.1
        self._accuracy = self._accuracy * (1 - alpha) + (1.0 if was_correct else 0.0) * alpha
```

### Voting & Confidence Fusion

```python
# vote.py

def classify_error(error_msg: str, context: ErrorContext) -> FinalClassification:
    """Three-layer classification with confidence-based fallback."""

    # Layer 1: Quick rule match
    l1 = layer1.match_known_pattern(error_msg)
    if l1 and l1.confidence >= 0.9:
        return FinalClassification(l1.class, l1.confidence, l1.params)

    # Layer 2: NLP spectrum voting
    l2 = nlp_spectrum.classify(error_msg, context)
    if l2.confidence >= 0.7:
        return FinalClassification(l2.predicted_class, l2.confidence, l2.fix_strategies)

    # Layer 3: LLM Judge (async, only if needed)
    l3 = await llm_judge.judge(error_msg, context.theorem_header)
    return FinalClassification(l3.error_class, l3.confidence * 0.8, l3.fix_strategies)
```

### Integration with Inner Loop

```python
# In inner.py, replace the error feedback section:

# Old:
extra_suggestions = _strategy_hint_for_error(compile_result, user_content)

# New:
classification = classify_error(
    error_msg=compile_result.errors[0],
    context=ErrorContext(
        theorem_header=theorem_header,
        error_line=compile_result.line,
        diagnostics=compile_result.diagnostics,
    )
)

if classification.confidence >= 0.85:
    feedback = f"🎯 {classification.fix_strategies[0]}"
elif classification.confidence >= 0.6:
    feedback = f"💡 Suggested fix: {classification.fix_strategies[0]}\n\nFull error:\n{error_text}"
else:
    feedback = error_text  # Fall through to generic feedback
```

### Judge Accuracy Tracking

```python
# After the round completes:
if classification.source == "llm_judge":
    llm_judge.update_accuracy(was_correct=result.success)
    # Auto-deprecate judges with <0.3 accuracy over 10+ uses
```

## Implementation Steps

```
Day 1: Layer 1 extensions + harden existing rule patterns
Day 2: Layer 2 NLP spectrum (4 algorithms + voting)
Day 3: Layer 3 LLM-as-Judge + integration with inner_loop + cache + accuracy tracking
```

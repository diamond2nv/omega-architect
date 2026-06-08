# ML Route Integration — ModelRouter v2.1

## Overview

MLRoute adds optional ML-based tier prediction to ModelRouter. The trained
LightGBM classifier (95.2% test accuracy, 95.5% T0 F1 after augmentation)
provides a second opinion that can override the rule-based `_estimate_complexity`
when confidence exceeds a threshold.

## Architecture

```
ModelRouter.select(header)
    │
    ├─ 1. Rule-based estimate ────────── _estimate_complexity(header)
    │       tier_str, flags, base_tier
    │
    ├─ 2. ML override ────────────────── MLRoute.predict_if_confident(header)
    │       │  if ML available AND confidence >= 0.6 AND tier ≠ rule_tier
    │       │  → override complexity = ml_pred.tier
    │       │
    │       MLRoute
    │       ├─ _lazy_load()  (once)
    │       │   ├─ feature_pipeline.joblib → vectorizer + SVD
    │       │   ├─ ONNX model (optional, ~3.7 MB)
    │       │   └─ LightGBM fallback (~5.5 MB)
    │       └─ predict(header):
    │           ├─ extract_hc(header)                → 51-dim
    │           ├─ vectorizer.transform([header])     → TFIDF
    │           ├─ svd.transform(tfidf)               → 102-dim
    │           └─ model.predict(concat)              → 3-class probs
    │
    └─ 3. _rank(complexity) → filtered models → best
```

## Files

| File | Role |
|------|------|
| `omega/resource/models/router_lgbm_v1.txt` | LightGBM Booster (5.5 MB, 561 trees) |
| `omega/resource/models/router_lgbm_v1.onnx` | ONNX export (3.7 MB, opset 15) |
| `omega/resource/models/feature_pipeline.joblib` | TFIDF vectorizer + SVD |
| `omega/resource/models/feature_names.npy` | 153 feature names |

## API

```python
# Standalone ML prediction
route = MLRoute()
pred = route.predict("theorem t (n : ℕ) : n + 0 = n :=")
# → MLPrediction(tier='medium', confidence=0.87, probabilities=[0.02, 0.87, 0.11])

# Only if confident (>threshold)
pred = route.predict_if_confident(header)

# Batch
results = route.predict_batch([h1, h2, h3])

# Integration with ModelRouter
router = ModelRouter(enable_ml=True)     # default: on
router = ModelRouter(enable_ml=False)    # opt out
```

## Configuration

| Parameter | Default | Effect |
|-----------|---------|--------|
| `ModelRouter(enable_ml=True)` | Enabled | Lazy-loads MLRoute on first `select()` |
| `MLRoute(confidence_threshold=0.6)` | 0.6 | Minimum confidence to override rule-based tier |
| `MLRoute(prefer_onnx=False)` | LightGBM | Tries LightGBM first |

## Decision Logic

```python
# In ModelRouter.select():
complexity = rule_based_tier  # from _estimate_complexity
if enable_ml:
    ml_pred = _ml_predict(header)
    if (ml_pred and
        ml_pred.tier != complexity and
        ml_pred.confidence >= 0.6):
        complexity = ml_pred.tier  # override
```

## Performance

| Step | Time |
|------|------|
| HC feature extraction | ~10 µs |
| TFIDF + SVD transform | ~30 µs |
| LightGBM inference | ~60 µs |
| **Total per header** | **~100 µs** |
| ONNX inference | ~40 µs (vs LightGBM 60 µs) |

## Testing

62 tests covering:

- **TestRuleBasedModelRouter** (19) — regression: heuristic unchanged
- **TestMLModelRouter** (11) — LightGBM model loading, features, prediction
- **TestRouterPipeline** (6) — end-to-end selection + history
- **TestRouterEdgeCases** (7) — boundary conditions
- **TestMLRoute** (11) — MLRoute predict, confidence, batch, unavailable
- **TestMLRouteONNX** (3) — ONNX backend, agreement with LightGBM
- **TestModelRouterWithML** (4) — integration: enable_ml=True/False

## Future Work

- **ONNX as default** — when onnxruntime is available, prefer ONNX for faster
  inference (~40 µs vs 60 µs)
- **Confidence calibration** — temperature scaling improves threshold tuning
- **Feedback loop** — record ML prediction outcomes to incrementally improve
- **Incremental training** — when wrong, add to training set for next version

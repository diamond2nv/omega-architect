# LightGBM Training Data Pipeline — Phase C

> Source-to-feature pipeline for training the ModelRouter LightGBM classifier
> for Omega's 3-tier theorem complexity routing.

## 1. Data Sources

| Source | Type | Size | Content | License | Use |
|--------|------|------|---------|---------|-----|
| **Omega Benchmark Suite** (Tiers 1-3) | Local structured | 25 theorems | Lean 4 theorem headers + known correct tier | Apache-2.0 | Gold-standard labels (S=0, M=1, H=2) |
| **Goedel-LM/Lean-workbook-proofs** | HF Dataset | ~100K proofs | `imports + theorem_header + proof` tuples | Apache-2.0 | Bulk training examples |
| **Goedel-LM/Goedel-Prover-SFT** | HF Dataset | ~200K examples | Instruction-finetuning data (NL+Lean) | Apache-2.0 | Diversity augmentation |
| **Goedel-LM/RL_dataset_V2** | HF Dataset | ~50K examples | RL training data with difficulty metrics | Apache-2.0 | Proxy difficulty scores |
| **miniF2F** (via arXiv:2605.17283 OProofs) | Local JSONL | 244 problems | IMO/AMC/AIME formal statements | Mixed | Tier-4 regression |
| **FrenzyMath/Herald_statements** | HF Dataset | 580K NL↔FL pairs | NL math problems + Lean formal statements | Apache-2.0 | NL→feature domain adaptation |
| **Omega T2 compile cache** (`~/.omega/benchmark_interim.json`) | Local | N generated | Real T2 compile metrics (time, attempts) | Apache-2.0 | Ground-truth difficulty labels |

### Dataset Sizing Summary

```
Source                Examples    Gold Labels    Total Features
─────────────────────────────────────────────────────────────
Omega Bench           25          ✅ manual      25 (seed)
Lean-workbook-proofs  100K        ❌ heuristic   100K bulk
Goedel-Prover-SFT     200K        ❌ heuristic   50K sampled
RL_dataset_V2         50K         ✅ proxy*      50K
Herald_statements     580K        ❌ heuristic   20K sampled
─────────────────────────────────────────────────────────────
Total available:      ~955K      ✅ 50+25        ~220K trainable
```

*RL_dataset_V2 contains per-example difficulty metrics from the Goedel-Prover training pipeline — used as proxy tier labels.

## 2. Label Generation Strategy

### 2.1 Gold Standard Labels (Omega Bench 25 + RL 50)

Manually verified ground truth:

```python
# From omega/benchmark/suite.py:
GOLD_LABELS: dict[str, int] = {
    # Tier 1: Pure Lean (simple = rfl/trivial)
    "trivial_true":        0,  # True := trivial
    "rfl_simple":          0,  # 1 = 1 := rfl
    "and_true":            0,  # True ∧ True := ⟨trivial, trivial⟩
    "or_true":             0,  # True ∨ True := Or.inl trivial
    "eq_refl":             0,  # a = a := rfl
    # Tier 2: Mathlib simp
    "dbl_zero":            1,  # dbl 0 = 0 := simp [dbl]
    "add_one":             1,  # n+1 = Nat.succ n := rfl
    "sq_zero":             1,  # sq 0 = 0 := simp [sq]
    "succ_eq_add_one":     1,  # Nat.succ n = n + 1 := rfl
    "add_self_zero":       1,  # n+n = 0 ↔ n = 0 := constructor...
    "sub_self":            1,  # n - n = 0 := omega / simp
    "zero_le":             1,  # 0 ≤ n := omega / exact Nat.zero_le _
    "one_mul_custom":      1,  # 1 * n = n := simp
    "le_refl":             1,  # n ≤ n := le_rfl
    "zero_add_custom":     1,  # 0 + n = n := simp
    # Tier 3: Induction / multi-tactic
    "double_succ":         2,  # double (succ n) = double n + 2
    "add_assoc_custom":    2,  # (a+b)+c = a+(b+c) -- induction
    "mul_comm_custom":     2,  # a*b = b*a -- induction
    "add_comm_custom":     2,  # a+b = b+a -- induction
    "mul_add_custom":      2,  # a*(b+c) = a*b + a*c
    "add_mul_custom":      2,  # (a+b)*c = a*c + b*c
    "mul_assoc_custom":    2,  # (a*b)*c = a*(b*c)
    "pow_two_custom":      2,  # n^2 = n*n
    "succ_mul_custom":     2,  # (succ a)*b = a*b + b
    "fact_pos":            2,  # fact n > 0 -- induction
}
```

For RL_dataset_V2 difficulty proxy:
```python
# RL_dataset_V2 has {"difficulty": 0.0-1.0} per example
# Mapping: difficulty < 0.3 → tier 0, 0.3-0.6 → tier 1, > 0.6 → tier 2
```

### 2.2 Heuristic Labels (Bulk data)

For unlabeled sources, use Omega's existing `_estimate_complexity` + `TheoremFlags` as labeler:

```yaml
Heuristic Labeling Pipeline:
  1. Extract theorem_header from each example
  2. Run analyze_theorem_pattern() → strategy + confidence
  3. Run compute_theorem_flags() → flags
  4. apply_postprocess(base_tier, flags) → final_tier (0/1/2)
  5. Accept if confidence > 0.5 (filter ambiguous samples)
```

Filtering: only keep examples where heuristic confidence ≥ 0.5 (reject ~30% ambiguous cases → ~154K usable)

### 2.3 Confidence Filtering

```
Heuristic Confidence    Accept    Yield    Cumulative
────────────────────────────────────────────────────
≥ 0.9 (trivial/rfl)     ✅        ~40K     40K
≥ 0.7 (simp, clear)     ✅        ~50K     90K
≥ 0.5 (medium conf)     ✅        ~64K     154K
< 0.5 (ambiguous)       ❌        ~66K     rejected
```

**Final training dataset: ~154K examples + 75 gold-standard = ~154K examples**

## 3. Feature Extraction

### 3.1 Handcrafted Features (HC, 51 dims)

Replicating SquillaRouter's HC pattern for theorem headers:

```python
def extract_hc(header: str) -> np.ndarray:
    """51-dim handcrafted features for Lean theorem header."""
    feats = np.zeros(51, dtype=np.float32)
    # Length measures (0-4)
    feats[0] = min(len(header) / 500, 1.0)       # normalized length
    feats[1] = np.log1p(len(header)) / 10.0       # log length
    feats[2] = min(len(header.split()) / 50, 1.0)  # token density
    feats[3] = min(sum(1 for c in header if c.isdigit()) / len(header) * 5, 1.0)  # digit ratio
    # Symbol density (5-15)
    feats[4] = min(header.count("→") / 5, 1.0)
    feats[5] = min(header.count("∀") / 5, 1.0)
    feats[6] = min(header.count("∃") / 5, 1.0)
    feats[7] = min(header.count("∧") / 5, 1.0)
    feats[8] = min(header.count("∨") / 5, 1.0)
    feats[9] = min(header.count("¬") / 3, 1.0)
    feats[10] = min(header.count("=") / 10, 1.0)
    feats[11] = min(header.count("<") + header.count("≤") / 5, 1.0)
    feats[12] = min(header.count("(") / 10, 1.0)
    feats[13] = min(header.count(":") / 10, 1.0)
    feats[14] = min(header.count("ℕ") + header.count("ℤ") + header.count("ℝ"), 1.0)
    # Type complexity (16-25)
    feats[15] = min(header.count("List") / 3, 1.0)
    feats[16] = min(header.count("Matrix") + header.count("Vector"), 1.0)
    feats[17] = min(header.count("ℕ →") + header.count("→ ℕ"), 1.0)
    feats[18] = min(header.count("∀") + header.count("∃"), 1.0) / 3
    feats[19] = float("import" in header.lower())
    feats[20] = float("omega" in header.lower())
    # Structural (26-50)
    feats[21] = float("induction" in header)
    feats[22] = float("cases" in header)
    feats[23] = min(header.count("theorem"), 3.0) / 3
    feats[24] = float("def" in header)
    feats[25] = float(header.count(":=") > 1)           # nested definitions
    # ... (remaining 25 dims for Lean-specific patterns)
    return feats
```

### 3.2 TFIDF Features (102 dims)

```python
# LightGBM requires ~100 TFIDF dims (matching SquillaRouter's 102)
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD

# Fit on ~150K Lean theorem headers
vectorizer = TfidfVectorizer(
    max_features=500,
    ngram_range=(1, 3),       # unigrams + bigrams + trigrams for Lean syntax
    analyzer="char_wb",       # character-level with word boundaries
    sublinear_tf=True,        # log scaling
)
svd = TruncatedSVD(n_components=102, random_state=42)

tfidf_raw = vectorizer.fit_transform(theorem_headers)  # (N, 500)
tfidf_svd = svd.fit_transform(tfidf_raw)               # (N, 102)
```

### 3.3 Context Features (10 dims — runtime only)

These are set at inference time, not training time (no context during training):

| Index | Feature | Source |
|-------|---------|--------|
| 0 | history_len | How many prior proof attempts |
| 1 | has_prev_asst | Whether there's a prior assistant response |
| 2 | prev_failed | Whether the prior attempt failed |
| 3 | turn_index | Current turn index in conversation |
| ... | ... | ... |

### 3.4 BGE Embeddings (optional, Phase C+)

`BAAI/bge-small-zh-v1.5` (384-dim → PCA(64)) for semantic understanding:

```python
from sentence_transformers import SentenceTransformer
bge = SentenceTransformer("BAAI/bge-small-zh-v1.5")
emb = bge.encode(theorem_header)  # (384,)
pca_64 = pca.transform([emb])[0]  # (64,) — only with fitted PCA
```

Skip for Phase C baseline — add in Phase C+.

## 4. Pipeline Architecture

```python
# omega/research/training/ (new directory)
#
# ┌──────────────────────────────────────────────────────┐
# │  1. data_collection.py                               │
# │     ├── collect_omega_benchmarks()     → 25 examples │
# │     ├── download_lean_workbook()       → 100K       │
# │     ├── download_goedel_sft()          → 50K sampled│
# │     ├── download_rl_dataset_v2()       → 50K        │
# │     └── download_herald_statements()   → 20K sampled│
# │                                                     │
# │  2. label_generation.py                              │
# │     ├── heuristic_label(header)        → 0/1/2      │
# │     ├── gold_label(name)               → 0/1/2      │
# │     ├── confidence_filter(conf)        → bool       │
# │     └── merge_labels()                 → DataFrame  │
# │                                                     │
# │  3. feature_extraction.py                            │
# │     ├── extract_hc(text)               → (51,)      │
# │     ├── extract_tfidf(text, svd)       → (102,)     │
# │     ├── build_feature_vector(...)      → (153,)     │
# │     └── build_dataset(df)              → X, y       │
# │                                                     │
# │  4. train_lgbm.py                                    │
# │     ├── split_train_val_test(df)                    │
# │     ├── train_lightgbm(X, y)           → model      │
# │     ├── evaluate(model, X_val, y_val)               │
# │     └── export_onnx(model, path)                    │
# │                                                     │
# │  5. evaluate_routing.py                              │
# │     ├── predict_tier(model, header)    → 0/1/2      │
# │     ├── compare_vs_heuristic(model, test_set)       │
# │     └── benchmark_vs_omega(bench_suite)             │
# └──────────────────────────────────────────────────────┘
```

## 5. HF Dataset Download Details

### Goedel-LM/Lean-workbook-proofs

```bash
# From Hermes venv (has hfpclawer)
hfpclawer download Goedel-LM/Lean-workbook-proofs \
    --output ~/.omega/training/lean_workbook_proofs \
    --max_samples 100_000

# Schema per example:
# {
#   "header": "theorem lean_workbook_10009 ... :=",
#   "proof": "...",
#   "imports": ["Mathlib", "Aesop"],
#   "difficulty": 0.7  # normalized [0, 1]
# }
```

### Goedel-LM/Goedel-Prover-SFT

```bash
hfpclawer download Goedel-LM/Goedel-Prover-SFT \
    --output ~/.omega/training/goedel_prover_sft \
    --max_samples 50_000

# Schema per example:
# {
#   "instruction": "Prove the following...",
#   "formal_statement": "theorem t ... :=",
#   "output": "..."
# }
```

### Goedel-LM/RL_dataset_V2 (best for proxy labels)

```bash
hfpclawer download Goedel-LM/RL_dataset_V2 \
    --output ~/.omega/training/rl_v2 \
    --max_samples 50_000
# Contains built-in difficulty metrics!
```

### FrenzyMath/Herald_statements

```bash
hfpclawer download FrenzyMath/Herald_statements \
    --output ~/.omega/training/herald \
    --max_samples 20_000
```

### Total Download: ~220K examples, ~2-4 GB disk

## 6. Training Run (target: ~15 min on RTX 4500 Ada)

```python
X_train.shape  # (120K, 153)  — HC(51) + TFIDF(102)
y_train.shape  # (120K,)       — 0/1/2 tier labels

# LightGBM training
import lightgbm as lgb
model = lgb.LGBMClassifier(
    n_estimators=500,
    learning_rate=0.05,
    max_depth=8,
    num_leaves=64,
    class_weight="balanced",
    random_state=42,
)
model.fit(
    X_train, y_train,
    eval_set=[(X_val, y_val)],
    eval_metric="multi_logloss",
    callbacks=[lgb.early_stopping(50)],
)

# Expected: ~200 trees, ~0.85-0.90 validation accuracy
# Feature importance: HC(51) ≈ 35%, TFIDF(102) ≈ 65%
```

### Expected Accuracy

| Split | Size | Accuracy | Notes |
|-------|------|----------|-------|
| Training | 120K | ~0.92 | LightGBM fits heuristic labels well |
| Validation | 30K | ~0.88 | Good generalization to held-out heuristic labels |
| Test (Manual) | 75 | ~0.82 | On gold-standard vs heuristic-disagreement samples |
| Omega Bench (Gold) | 25 | ~0.96 | Known tier labels from benchmark suite |

## 7. Integration with Omega

### Training Pipeline CLI

```bash
# Step 1: Download and label
omega train-router stage1         # Download HF datasets + generate heuristic labels

# Step 2: Extract features
omega train-router stage2         # HC + TFIDF feature extraction → numpy arrays

# Step 3: Train LightGBM
omega train-router stage3         # Train LGBM + evaluate + export ONNX

# Step 4: Deploy
omega train-router deploy         # Replace heuristic with ML model
```

### Inference Integration (model_router.py)

```python
class MLModelRouter(ModelRouter):
    """Variant using LightGBM instead of heuristic patterns."""

    def __init__(self, model_path: str = "~/.omega/models/router_lgbm.txt"):
        import lightgbm as lgb
        self._model = lgb.Booster(model_file=model_path)
        # Load fitted TFIDF + SVD from training
        self._features = load_feature_pipeline()
        super().__init__()

    def _estimate_complexity(self, header: str) -> str:
        X = self._features.transform(header)  # (1, 153)
        tier_idx = int(self._model.predict(X)[0])  # 0, 1, or 2
        return _TIER_TO_STR[tier_idx]
```

### Feature Pipeline Serialization

```python
# Save to ~/.omega/models/router_features.joblib
joblib.dump({
    "tfidf_vectorizer": vectorizer,
    "tfidf_svd": svd,
    "feature_names": feature_names,
}, "~/.omega/models/router_features.joblib")
```

### Performance Comparison

| Metric | Heuristic (current) | LightGBM (Phase C) | Delta |
|--------|--------------------|-------------------|-------|
| Accuracy vs gold | ~0.72 (est) | ~0.82 (target) | +10% |
| Runtime per select | <1ms (regex) | <5ms (LGBM) | ~5ms overhead |
| Ambiguous rejection | ~30% | ~10% | +20% coverage |
| Feature engineering | manual | automated importance | maintenance save |
| Cross-domain transfer | low | medium | broadens scope |

## 8. File Structure

```
omega/research/training/
├── __init__.py
├── data_collection.py         # Step 1: HF download + caching
├── label_generation.py        # Step 2: Heuristic + gold labels
├── feature_extraction.py      # Step 3: HC(51) + TFIDF(102)
├── train_lgbm.py              # Step 4: LightGBM training + ONNX export
├── evaluate_routing.py        # Step 5: Compare vs heuristic
├── config.py                  # Training config (paths, hyperparams)
└── references/
    └── dataset_sources.md     # Dataset locations + licenses
```

## 9. Compliance

All datasets used are Apache-2.0 licensed (Goedel-LM, Herald) or
research-permissive (miniF2F). Model weights (LightGBM .txt / ONNX .onnx)
are trained derived works — Omega can distribute them under Apache-2.0.

### Dataset Licenses

| Dataset | License | Commercial Use | Citation Needed |
|---------|---------|---------------|-----------------|
| Goedel-LM/Lean-workbook-proofs | Apache-2.0 | ✅ | ✅ Goedel-Prover paper |
| Goedel-LM/Goedel-Prover-SFT | Apache-2.0 | ✅ | ✅ |
| Goedel-LM/RL_dataset_V2 | Apache-2.0 | ✅ | ✅ |
| FrenzyMath/Herald_statements | Apache-2.0 | ✅ | ✅ Herald paper |
| Omega Benchmark Suite | Apache-2.0 | ✅ | Omega-Architect |

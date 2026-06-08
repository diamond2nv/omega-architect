#!/usr/bin/env python3
"""Feature extraction for ModelRouter LightGBM training — Stage 3.

Transforms labeled theorem headers into 153-dim feature vectors:
  HC(51)    — Handcrafted features (length, symbol density, type complexity)
  TFIDF(102) — Truncated SVD-reduced TF-IDF features (character n-grams)

Output: X.npy (N×153), y.npy (N,), and feature pipeline serialization.

Usage::

    # Extract features for all labeled data
    python -m omega.research.training.feature_extraction extract

    # Show feature importance (after training)
    python -m omega.research.training.feature_extraction importance
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

# ── Paths ──────────────────────────────────────────────────────

_CACHE_DIR = Path.home() / ".omega" / "training"
_LABELED_PATH = _CACHE_DIR / "labeled" / "data.jsonl"
_FEATURES_DIR = _CACHE_DIR / "features"
_X_PATH = _FEATURES_DIR / "X.npy"
_Y_PATH = _FEATURES_DIR / "y.npy"
_HEADER_PATH = _FEATURES_DIR / "headers.npy"
_PIPELINE_PATH = _FEATURES_DIR / "feature_pipeline.joblib"


# ═══════════════════════════════════════════════════════════════
# Handcrafted Features (51-dim)
# ═══════════════════════════════════════════════════════════════

# Lean 4 keywords and symbols for density features
_LEAN_KEYWORDS = [
    "theorem", "lemma", "def", "inductive", "structure",
    "induction", "cases", "simp", "rw", "omega",
    "calc", "by", "have", "show", "let",
    "fun", "forall", "match", "refine", "apply",
]

_LEAN_TYPE_SYMBOLS = [
    "→", "∀", "∃", "∧", "∨", "¬", "↔",
    "=", "≠", "<", "≤", ">", "≥",
    "α", "β", "γ", "ℕ", "ℤ", "ℝ", "ℂ", "ℚ",
]

_TRIGRAM_MATHS_PATTERNS = [
    r"Nat\.", r"Int\.", r"Real\.", r"List\.", r"Set\.",
    r"Finset\.", r"RingHom", r"Module", r"Category",
    r"Topology", r"Algebra", r"Polynomial",
]


# Derived from OpenSquilla's HC(51) feature design pattern (Apache-2.0).
def extract_hc(header: str) -> np.ndarray:
    """51-dim handcrafted features for Lean theorem header.

    Feature layout:
      [0:4]    Length measures
      [4:9]    Character-level features
      [9:19]   Syntax density
      [19:29]  Type complexity
      [29:39]  Keyword presence
      [39:49]  Structural indicators
      [49:51]  Composition features
    """
    feats = np.zeros(51, dtype=np.float32)
    h = header or ""

    # ── Length measures (0:4) ────────────────────────────
    feats[0] = min(len(h) / 500, 1.0)          # normalized length
    feats[1] = np.log1p(len(h)) / 10.0          # log length
    feats[2] = min(len(h.split()) / 50, 1.0)     # word count
    feats[3] = min(h.count(":") / 15, 1.0)       # type annotation density

    # ── Character-level features (4:9) ───────────────────
    total_chars = max(len(h), 1)
    feats[4] = sum(1 for c in h if c.isdigit()) / total_chars * 5  # digit ratio
    feats[5] = sum(1 for c in h if c.isupper()) / total_chars * 5  # uppercase ratio
    feats[6] = sum(1 for c in h if c in "({[") / 5                 # bracket open
    feats[7] = sum(1 for c in h if c in ")}]") / 5                 # bracket close
    feats[8] = h.count(":=") / 5                                    # definition

    # ── Syntax density (9:19) ────────────────────────────
    for i, sym in enumerate(_LEAN_TYPE_SYMBOLS[:10]):
        feats[9 + i] = min(h.count(sym) / 5, 1.0)

    # ── Type complexity (19:29) ──────────────────────────
    feats[19] = min(h.count("→") + h.count("→") / 3, 1.0)  # function type
    feats[20] = min(h.count("∀") / 3, 1.0)                  # universal quantifier
    feats[21] = min(h.count("∃") / 3, 1.0)                  # existential
    feats[22] = min(h.count("∧") + h.count("∧") / 5, 1.0)  # conjunction
    feats[23] = min(h.count("∨") + h.count("∨") / 3, 1.0)  # disjunction
    feats[24] = min(h.count("¬") / 3, 1.0)                  # negation
    feats[25] = min(h.count("ℕ") + h.count("ℤ") + h.count("ℚ"), 1.0)  # discrete types
    feats[26] = min(h.count("ℝ") + h.count("ℂ"), 1.0)                  # continuous types
    feats[27] = min(h.count("List") / 3, 1.0)                           # list type
    feats[28] = min(h.count("Set") + h.count("Finset") + h.count("Finset") / 3, 1.0)  # set types

    # ── Keyword presence (29:39) ─────────────────────────
    hl = h.lower()
    for i, kw in enumerate(_LEAN_KEYWORDS[:10]):
        feats[29 + i] = float(kw in hl)

    # ── Structural indicators (39:49) ────────────────────
    feats[39] = float("induction" in hl)                    # induction strategy
    feats[40] = float("cases" in hl)                        # case analysis
    feats[41] = float("import" in hl)                       # external library
    feats[42] = min(h.count("(") / 10, 1.0)                 # binder count proxy
    feats[43] = min(h.count(":") / 15, 1.0)                 # binder count proxy
    # Trigram math patterns
    for i, pat in enumerate(_TRIGRAM_MATHS_PATTERNS[:6]):
        feats[44 + i] = min(len(re.findall(pat, h)) / 3, 1.0)

    # ── Composition features (49:51) ─────────────────────
    feats[49] = float(h.count(":=") > 1)                    # nested definitions
    feats[50] = min(len(h) / 100, 1.0) * float("inductive" in hl)  # inductive + long

    return feats


# ═══════════════════════════════════════════════════════════════
# TFIDF Features (102-dim)
# ═══════════════════════════════════════════════════════════════

_TFIDF_DIM = 102


def _build_tfidf_pipeline(headers: list[str]) -> tuple[Any, Any]:
    """Build TFIDF vectorizer + SVD from a corpus of theorem headers."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    vectorizer = TfidfVectorizer(
        max_features=500,
        ngram_range=(1, 3),
        analyzer="char_wb",
        sublinear_tf=True,
    )
    svd = TruncatedSVD(n_components=_TFIDF_DIM, random_state=42)

    tfidf_raw = vectorizer.fit_transform(headers)
    tfidf_svd = svd.fit_transform(tfidf_raw)
    print(f"[TFIDF]  Vocabulary size: {len(vectorizer.get_feature_names_out())}")
    print(f"[TFIDF]  SVD explained variance: {svd.explained_variance_ratio_.sum():.3f}")

    return vectorizer, svd


def _transform_tfidf(
    headers: list[str],
    vectorizer: Any,
    svd: Any,
) -> np.ndarray:
    """Transform theorem headers to TFIDF features."""
    tfidf_raw = vectorizer.transform(headers)
    return svd.transform(tfidf_raw).astype(np.float32)


# ═══════════════════════════════════════════════════════════════
# Main extraction
# ═══════════════════════════════════════════════════════════════


def extract(
    labeled_path: Path | None = None,
    output_dir: Path | None = None,
    sample: int | None = None,
    train_ratio: float = 0.8,
    val_ratio: float = 0.1,
    label_types: list[str] | None = None,
) -> dict:
    """Extract features from labeled data and produce train/val/test splits.

    Parameters
    ----------
    labeled_path : Path, optional
        Path to labeled data JSONL.
    output_dir : Path, optional
        Output directory for feature matrices.
    sample : int, optional
        Subsample N examples (for quick iteration).
    train_ratio : float
        Fraction for training (default 0.8).
    val_ratio : float
        Fraction for validation (default 0.1).
    label_types : list[str], optional
        Label methods to include (default: all).

    Returns
    -------
    dict
        Statistics about the extraction.
    """
    if labeled_path is None:
        labeled_path = _LABELED_PATH
    if output_dir is None:
        output_dir = _FEATURES_DIR
    if label_types is None:
        label_types = ["gold", "heuristic"]

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Read labeled data ─────────────────────────────────
    print(f"[Read] Loading {labeled_path} ...")
    examples: list[dict] = []
    with open(labeled_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                ex = json.loads(line)
                if ex["label_method"] in label_types:
                    examples.append(ex)

    if sample and sample < len(examples):
        import random
        random.seed(42)
        examples = random.sample(examples, sample)

    print(f"[Read]  {len(examples)} labeled examples ({', '.join(label_types)})")

    # ── Extract HC features ───────────────────────────────
    print("[HC]    Extracting 51-dim handcrafted features ...")
    t0 = time.time()
    hc_features = np.array([extract_hc(ex["header"]) for ex in examples], dtype=np.float32)
    elapsed = time.time() - t0
    print(f"[HC]    {hc_features.shape} in {elapsed:.1f}s ({len(examples)/max(elapsed,0.01):.0f} ex/s)")

    # ── Build + extract TFIDF features ────────────────────
    headers = [ex["header"] for ex in examples]
    print(f"[TFIDF] Fitting on {len(headers)} headers ...")
    t0 = time.time()
    vectorizer, svd = _build_tfidf_pipeline(headers)
    tfidf = _transform_tfidf(headers, vectorizer, svd)
    elapsed = time.time() - t0
    print(f"[TFIDF] {tfidf.shape} in {elapsed:.1f}s")

    # ── Combine features ──────────────────────────────────
    X = np.concatenate([hc_features, tfidf], axis=1).astype(np.float32)
    y = np.array([ex["tier"] for ex in examples], dtype=np.int32)
    names = np.array([ex["name"] for ex in examples], dtype=object)
    sources = np.array([ex["source"] for ex in examples], dtype=object)

    print(f"[Combine] X shape: {X.shape} (HC 51 + TFIDF {_TFIDF_DIM} = {51 + _TFIDF_DIM})")
    print(f"[Combine] y shape: {y.shape}, classes: {set(y.tolist())}")
    print(f"[Combine] Class distribution: 0={int((y==0).sum())}, "
          f"1={int((y==1).sum())}, 2={int((y==2).sum())}")

    # ── Train/val/test split ──────────────────────────────
    n = len(examples)
    indices = np.random.RandomState(42).permutation(n)

    n_train = int(n * train_ratio)
    n_val = int(n * val_ratio)

    train_idx = indices[:n_train]
    val_idx = indices[n_train:n_train + n_val]
    test_idx = indices[n_train + n_val:]

    splits = {
        "train": (X[train_idx], y[train_idx], names[train_idx]),
        "val": (X[val_idx], y[val_idx], names[val_idx]),
        "test": (X[test_idx], y[test_idx], names[test_idx]),
    }

    for name, (xx, yy, nn) in splits.items():
        print(f"[Split]  {name}: {len(xx)} examples "
              f"(T0={int((yy==0).sum())}, T1={int((yy==1).sum())}, T2={int((yy==2).sum())})")

    # ── Save ──────────────────────────────────────────────
    np.save(str(output_dir / "X.npy"), X)
    np.save(str(output_dir / "y.npy"), y)
    np.save(str(output_dir / "headers.npy"), names)
    np.save(str(output_dir / "sources.npy"), sources)

    # Save splits
    np.save(str(output_dir / "train_idx.npy"), train_idx)
    np.save(str(output_dir / "val_idx.npy"), val_idx)
    np.save(str(output_dir / "test_idx.npy"), test_idx)

    # Save feature pipeline
    import joblib
    pipeline = {"vectorizer": vectorizer, "svd": svd, "feature_dim": 51 + _TFIDF_DIM}
    joblib.dump(pipeline, str(output_dir / "feature_pipeline.joblib"))
    print(f"[Save]   Pipeline → {output_dir / 'feature_pipeline.joblib'}")

    # ── Feature names ─────────────────────────────────────
    hc_names = [f"hc_{i}" for i in range(51)]
    tfidf_names = [f"tfidf_{i}" for i in range(_TFIDF_DIM)]
    all_names = hc_names + tfidf_names
    np.save(str(output_dir / "feature_names.npy"), np.array(all_names, dtype=object))
    print(f"[Save]   Feature names → {output_dir / 'feature_names.npy'}")

    return {
        "total": n,
        "feature_dim": 51 + _TFIDF_DIM,
        "class_distribution": {0: int((y == 0).sum()), 1: int((y == 1).sum()), 2: int((y == 2).sum())},
        "splits": {name: len(xx) for name, (xx, _, _) in splits.items()},
    }


def load_features(output_dir: Path | None = None) -> dict:
    """Load pre-computed features from disk."""
    if output_dir is None:
        output_dir = _FEATURES_DIR
    return {
        "X": np.load(str(output_dir / "X.npy")),
        "y": np.load(str(output_dir / "y.npy")),
        "headers": np.load(str(output_dir / "headers.npy"), allow_pickle=True),
        "sources": np.load(str(output_dir / "sources.npy"), allow_pickle=True),
        "train_idx": np.load(str(output_dir / "train_idx.npy")),
        "val_idx": np.load(str(output_dir / "val_idx.npy")),
        "test_idx": np.load(str(output_dir / "test_idx.npy")),
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "extract"

    if cmd == "extract":
        sample = None
        if "--sample" in sys.argv:
            ix = sys.argv.index("--sample")
            sample = int(sys.argv[ix + 1])

        t0 = time.time()
        stats = extract(sample=sample)
        elapsed = time.time() - t0
        print(f"\n  Extraction complete in {elapsed:.1f}s")
        print(f"  Total: {stats['total']} examples, {stats['feature_dim']}-dim features")
        print(f"  Splits: {stats['splits']}")

    elif cmd == "stats":
        from omega.research.training.label_generation import stats, print_stats
        s = stats()
        print_stats(s)

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python -m omega.research.training.feature_extraction [extract|stats]")
        print("  Options: --sample N  (subsample for quick iteration)")
        return 1
    return 0


if __name__ == "__main__":
    main()

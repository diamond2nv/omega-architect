#!/usr/bin/env python3
"""LightGBM training for ModelRouter 3-tier classifier — Stage 4.

Trains a LightGBM multi-class classifier on the 153-dim feature vectors
(51 HC + 102 TFIDF) generated in Stage 3.  Handles class imbalance
via ``scale_pos_weight``.  Exports the trained model as ``.txt`` for
direct LightGBM inference and as ``.onnx`` for ONNX Runtime deployment.

Usage::

    # Train the model (default)
    python -m omega.research.training.train_lgbm

    # Train with different hyperparams
    python -m omega.research.training.train_lgbm --learning-rate 0.1 --n-estimators 1000

    # Deploy: copy model to omega/resource/
    python -m omega.research.training.train_lgbm deploy

    # Evaluate on test set
    python -m omega.research.training.train_lgbm evaluate
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

# ── Paths ──────────────────────────────────────────────────────

_FEATURES_DIR = Path.home() / ".omega" / "training" / "features"
_MODELS_DIR = Path.home() / ".omega" / "models"
_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "omega" / "resource" / "models"


# ═══════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════


def load_data() -> dict[str, np.ndarray]:
    """Load pre-computed feature arrays from Stage 3."""
    needed = ["X", "y", "train_idx", "val_idx", "test_idx"]
    result: dict[str, np.ndarray] = {}
    for name in needed:
        path = _FEATURES_DIR / f"{name}.npy"
        if not path.exists():
            raise FileNotFoundError(f"Missing feature file: {path}. Run Stage 3 first.")
        result[name] = np.load(str(path))
    return result


# ═══════════════════════════════════════════════════════════════
# Training
# ═══════════════════════════════════════════════════════════════


def _class_weight_balanced(y: np.ndarray) -> list[float]:
    """Compute per-class weights inversely proportional to frequency."""
    classes, counts = np.unique(y, return_counts=True)
    n_samples = len(y)
    n_classes = len(classes)
    weights = [n_samples / (n_classes * c) for c in counts]
    return weights


def train(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    learning_rate: float = 0.03,
    n_estimators: int = 800,
    max_depth: int = 6,
    num_leaves: int = 31,
    min_data_in_leaf: int = 20,
    feature_fraction: float = 0.8,
    bagging_fraction: float = 0.8,
    bagging_freq: int = 5,
    reg_alpha: float = 0.1,
    reg_lambda: float = 0.1,
    early_stopping_rounds: int = 50,
    verbose: int = 100,
) -> tuple[Any, dict]:
    """Train a LightGBM multi-class classifier.

    Parameters
    ----------
    X_train : (N, 153) float32
    y_train : (N,) int {0, 1, 2}
    X_val, y_val : same shapes for validation
    learning_rate : float
        Lower = more robust but slower convergence.
    n_estimators : int
        Max number of boosting rounds.
    max_depth : int
        Tree depth limit.
    num_leaves : int
        Max leaves per tree.
    min_data_in_leaf : int
        Minimum samples per leaf (prevent overfitting).
    feature_fraction : float
        Feature sampling ratio per tree.
    bagging_fraction : float
        Row sampling ratio.
    bagging_freq : int
        Frequency of bagging.
    reg_alpha, reg_lambda : float
        L1/L2 regularization.
    early_stopping_rounds : int
        Stop if no improvement for N rounds.
    verbose : int
        Logging frequency.

    Returns
    -------
    tuple[model, dict[str, Any]]
        Trained LightGBM Booster and training metrics.
    """
    import lightgbm as lgb

    # Compute class weights for imbalance
    class_weight = _class_weight_balanced(y_train)
    print(f"  Class weights (balanced): T0={class_weight[0]:.3f}, "
          f"T1={class_weight[1]:.3f}, T2={class_weight[2]:.3f}")

    # Create datasets
    train_data = lgb.Dataset(X_train, label=y_train)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    # Parameters
    params = {
        "objective": "multiclass",
        "num_class": 3,
        "metric": ["multi_logloss", "multi_error"],
        "boosting_type": "gbdt",
        "learning_rate": learning_rate,
        "n_estimators": n_estimators,
        "max_depth": max_depth,
        "num_leaves": num_leaves,
        "min_data_in_leaf": min_data_in_leaf,
        "feature_fraction": feature_fraction,
        "bagging_fraction": bagging_fraction,
        "bagging_freq": bagging_freq,
        "reg_alpha": reg_alpha,
        "reg_lambda": reg_lambda,
        "class_weight": class_weight,
        "seed": 42,
        "verbosity": -1,
    }

    # Pretty-print params (excluding class_weight which is verbose)
    print("  Params:")
    for k, v in params.items():
        if k != "class_weight":
            print(f"    {k}: {v}")
    print(f"  Training on {len(X_train)} samples, validating on {len(X_val)} ...")

    t0 = time.time()
    model = lgb.train(
        params,
        train_data,
        valid_sets=[train_data, val_data],
        callbacks=[
            lgb.early_stopping(early_stopping_rounds),
            lgb.log_evaluation(verbose),
        ],
    )
    elapsed = time.time() - t0

    # Metrics
    best_iter = model.best_iteration
    val_loss = model.best_score["valid_1"]["multi_logloss"]
    val_err = model.best_score["valid_1"]["multi_error"]
    train_loss = model.best_score["training"]["multi_logloss"]
    train_err = model.best_score["training"]["multi_error"]

    print(f"\n  Training complete in {elapsed:.1f}s")
    print(f"  Best iteration: {best_iter}")
    print(f"  Train multi_logloss: {train_loss:.4f}  error: {train_err:.4f}")
    print(f"  Val   multi_logloss: {val_loss:.4f}  error: {val_err:.4f}")

    metrics = {
        "best_iteration": best_iter,
        "train_logloss": train_loss,
        "train_error": train_err,
        "val_logloss": val_loss,
        "val_error": val_err,
        "training_time_s": round(elapsed, 1),
        "n_train": len(X_train),
        "n_val": len(X_val),
        "feature_dim": X_train.shape[1],
    }

    return model, metrics


# ═══════════════════════════════════════════════════════════════
# Evaluation
# ═══════════════════════════════════════════════════════════════


def evaluate(model, X_test: np.ndarray, y_test: np.ndarray) -> dict:
    """Evaluate on held-out test set."""
    import lightgbm as lgb

    if isinstance(model, str):
        model = lgb.Booster(model_file=model)

    y_pred_probs = model.predict(X_test)  # (N, 3)
    y_pred = np.argmax(y_pred_probs, axis=1)

    accuracy = float(np.mean(y_pred == y_test))
    conf_matrix = np.zeros((3, 3), dtype=int)
    for true_idx, pred_idx in zip(y_test, y_pred):
        conf_matrix[true_idx, pred_idx] += 1

    # Per-class metrics
    per_class = {}
    for cls in range(3):
        tp = conf_matrix[cls, cls]
        fp = conf_matrix[:, cls].sum() - tp
        fn = conf_matrix[cls, :].sum() - tp
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-10)
        per_class[cls] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": int(conf_matrix[cls, :].sum()),
        }

    print(f"\n  Test set evaluation:")
    print(f"  Accuracy: {accuracy:.4f} ({int(accuracy * len(y_test))}/{len(y_test)})")
    print(f"  Confusion matrix:")
    print(f"          Pred 0  Pred 1  Pred 2")
    for true_idx in range(3):
        row = conf_matrix[true_idx]
        print(f"  True {true_idx}:  {row[0]:>6} {row[1]:>6} {row[2]:>6}")
    print()
    for cls in range(3):
        m = per_class[cls]
        print(f"  Class {cls}: P={m['precision']:.3f} R={m['recall']:.3f} "
              f"F1={m['f1']:.3f} n={m['support']}")

    return {
        "accuracy": accuracy,
        "confusion_matrix": conf_matrix.tolist(),
        "per_class": per_class,
        "n_test": len(y_test),
    }


# ═══════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════


def export_model(
    model: Any,
    output_dir: Path | None = None,
    version: str = "v1",
) -> dict[str, str]:
    """Export trained model to .txt + .onnx formats.

    Parameters
    ----------
    model : lgb.Booster
        Trained LightGBM model.
    output_dir : Path, optional
        Output directory. Defaults to ~/.omega/models/.
    version : str
        Version tag for output filenames.

    Returns
    -------
    dict[str, str]
        Paths to exported model files.
    """
    if output_dir is None:
        output_dir = _MODELS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # LightGBM .txt format
    lgb_path = output_dir / f"router_lgbm_{version}.txt"
    model.save_model(str(lgb_path))
    size_mb = lgb_path.stat().st_size / 1e6

    # ONNX format (optional dependency)
    onnx_path = output_dir / f"router_lgbm_{version}.onnx"
    onnx_ok = False
    try:
        import lightgbm as lgb
        # LightGBM has built-in ONNX conversion via `lgbm_to_onnx`
        # but it requires `skl2onnx`. Try the simpler path first.
        try:
            from skl2onnx import convert_lightgbm
            from skl2onnx.common.data_types import FloatTensorType
            initial_types = [("float_input", FloatTensorType([None, model.num_feature()]))]
            onnx_model = convert_lightgbm(model, initial_types=initial_types, target_opset=17)
            with open(onnx_path, "wb") as f:
                f.write(onnx_model.SerializeToString())
            onnx_ok = True
        except ImportError:
            raise  # will be caught below
    except Exception as e:
        print(f"  [WARN] ONNX export skipped (skl2onnx not available): {e}")
        onnx_path = None  # type: ignore[assignment]

    print(f"\n  Exported model:")
    print(f"    LightGBM: {lgb_path} ({size_mb:.1f} MB)")
    if onnx_ok:
        print(f"    ONNX:     {onnx_path} (skl2onnx)")

    return {
        "lgbm_path": str(lgb_path),
        "onnx_path": str(onnx_path) if onnx_ok else "",
        "size_mb": round(size_mb, 2),
    }


def deploy(
    source_path: Path | str | None = None,
    version: str = "v1",
) -> str:
    """Copy the trained model to ``omega/resource/models/``.

    Parameters
    ----------
    source_path : Path, optional
        Source model file.  Defaults to latest in ~/.omega/models/.
    version : str
        Version tag.

    Returns
    -------
    str
        Destination path.
    """
    if source_path is None:
        candidates = sorted(_MODELS_DIR.glob(f"router_lgbm_{version}.txt"))
        if not candidates:
            raise FileNotFoundError(
                f"No model found at {_MODELS_DIR}/router_lgbm_{version}.txt. "
                "Train first with: python -m omega.research.training.train_lgbm"
            )
        source_path = candidates[-1]

    source = Path(source_path)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = _OUTPUT_DIR / source.name
    import shutil
    shutil.copy2(str(source), str(dest))
    print(f"  Deployed {source} → {dest}")
    return str(dest)


# ═══════════════════════════════════════════════════════════════
# Feature importance
# ═══════════════════════════════════════════════════════════════


def print_feature_importance(model, top_n: int = 20) -> None:
    """Print top-N feature importances."""
    import lightgbm as lgb

    if isinstance(model, str):
        model = lgb.Booster(model_file=model)

    # Load feature names
    names_path = _FEATURES_DIR / "feature_names.npy"
    if names_path.exists():
        feature_names = np.load(str(names_path), allow_pickle=True).tolist()
    else:
        feature_names = [f"f{i}" for i in range(model.num_feature())]

    importance = model.feature_importance(importance_type="gain")
    idx_sorted = np.argsort(importance)[::-1]

    print(f"\n  Top {top_n} features by gain importance:")
    print(f"  {'Rank':<6} {'Feature':<30} {'Gain':<12} {'Group'}")
    print(f"  " + "-" * 65)
    for rank, idx in enumerate(idx_sorted[:top_n]):
        name = feature_names[idx] if idx < len(feature_names) else f"f{idx}"
        group = "TFIDF" if "tfidf" in name else ("HC" if name.startswith("hc_") else "Other")
        print(f"  {rank + 1:<6} {name:<30} {importance[idx]:<12.1f} {group}")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="LightGBM ModelRouter training")
    parser.add_argument("action", nargs="?", default="train",
                        choices=["train", "evaluate", "deploy", "importance"])
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--n-estimators", type=int, default=800)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--num-leaves", type=int, default=31)
    parser.add_argument("--model-path", type=str, default=None)
    parser.add_argument("--version", type=str, default="v1")
    parser.add_argument("--verbose", type=int, default=100)
    args = parser.parse_args()

    if args.action == "train":
        print("=" * 60)
        print("  LightGBM ModelRouter Training")
        print("=" * 60)

        # Load data
        data = load_data()
        X, y = data["X"], data["y"]
        train_idx, val_idx, test_idx = (
            data["train_idx"], data["val_idx"], data["test_idx"]
        )
        X_train, y_train = X[train_idx], y[train_idx]
        X_val, y_val = X[val_idx], y[val_idx]
        X_test, y_test = X[test_idx], y[test_idx]

        print(f"\n  Data shapes:")
        print(f"    Train: {X_train.shape} ({len(X_train)} ex)")
        print(f"    Val:   {X_val.shape} ({len(X_val)} ex)")
        print(f"    Test:  {X_test.shape} ({len(X_test)} ex)")
        print(f"    Feature dim: {X.shape[1]}")

        model, metrics = train(
            X_train, y_train, X_val, y_val,
            learning_rate=args.learning_rate,
            n_estimators=args.n_estimators,
            max_depth=args.max_depth,
            num_leaves=args.num_leaves,
            verbose=args.verbose,
        )

        # Evaluate on test
        eval_metrics = evaluate(model, X_test, y_test)
        metrics.update(eval_metrics)

        # Feature importance
        print_feature_importance(model)

        # Export
        export_model(model, version=args.version)

        # Save metrics JSON
        metrics_path = _MODELS_DIR / f"metrics_{args.version}.json"
        _MODELS_DIR.mkdir(parents=True, exist_ok=True)
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)
        print(f"\n  Metrics saved: {metrics_path}")

    elif args.action == "evaluate":
        if args.model_path is None:
            candidates = sorted(_MODELS_DIR.glob(f"router_lgbm_{args.version}.txt"))
            if not candidates:
                print("ERROR: No trained model found. Run 'train' first.")
                return 1
            args.model_path = str(candidates[-1])
        import lightgbm as lgb
        model = lgb.Booster(model_file=args.model_path)
        data = load_data()
        X_test, y_test = data["X"][data["test_idx"]], data["y"][data["test_idx"]]
        evaluate(model, X_test, y_test)

    elif args.action == "deploy":
        deploy(version=args.version)

    elif args.action == "importance":
        if args.model_path is None:
            candidates = sorted(_MODELS_DIR.glob(f"router_lgbm_{args.version}.txt"))
            if not candidates:
                print("ERROR: No trained model found. Run 'train' first.")
                return 1
            args.model_path = str(candidates[-1])
        print_feature_importance(Path(args.model_path), top_n=30)

    return 0


if __name__ == "__main__":
    main()

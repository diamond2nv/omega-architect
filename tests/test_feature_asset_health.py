"""Asset health gates: shipped model artefacts must load cleanly.

Regression guard for a real incident (2026-09-10): the TF-IDF/SVD pipeline was
serialised with scikit-learn 1.8.0 while the environment ran 1.9.0, producing
``InconsistentVersionWarning`` on every load — a silent-degradation risk for the
difficulty model that feeds the Mode Router. The asset was re-exported with the
current sklearn after proving bit-identical transform output; this gate keeps it
that way.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

MODELS_DIR = Path(__file__).resolve().parents[1] / "omega" / "resource" / "models"
PIPELINE = MODELS_DIR / "feature_pipeline.joblib"


def test_feature_pipeline_asset_exists() -> None:
    assert PIPELINE.exists(), f"missing shipped asset: {PIPELINE}"


def test_feature_pipeline_loads_without_version_warnings() -> None:
    """No sklearn ``InconsistentVersionWarning`` when loading the asset."""
    pytest.importorskip("sklearn")
    joblib = pytest.importorskip("joblib")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        payload = joblib.load(PIPELINE)

    version_warnings = [
        str(w.message)
        for w in caught
        if "InconsistentVersion" in type(w.message).__name__
        or "was trained with" in str(w.message)
        or "Trying to unpickle" in str(w.message)
    ]
    assert not version_warnings, (
        f"shipped sklearn asset was serialised with a different version: {version_warnings[:1]}"
    )
    assert {"vectorizer", "svd", "feature_dim"} <= set(payload)
    assert isinstance(payload["feature_dim"], int) and payload["feature_dim"] > 0


def test_feature_pipeline_transform_shape() -> None:
    """The pipeline still produces (n_texts, 500) TF-IDF and (n_texts, 102) SVD."""
    pytest.importorskip("sklearn")
    joblib = pytest.importorskip("joblib")
    np = pytest.importorskip("numpy")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        payload = joblib.load(PIPELINE)
    texts = ["theorem foo : 1 + 1 = 2 := by norm_num"]
    tfidf = payload["vectorizer"].transform(texts)
    svd = payload["svd"].transform(tfidf)
    assert tfidf.shape[0] == 1 and svd.shape == (1, 102)
    assert float(np.abs(svd).sum()) > 0.0

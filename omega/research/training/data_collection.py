#!/usr/bin/env python3
"""Data collection for ModelRouter LightGBM training.

Downloads Lean 4 theorem datasets from HuggingFace and local caches,
then filters/samples them into a unified ``.omega/training/`` cache.

Usage::

    # Quick scan — see what's available without downloading
    python -m omega.research.training.data_collection scan

    # Download all datasets (may take 5-15 min on ~100Mbps)
    python -m omega.research.training.data_collection download

    # Build unified dataset
    python -m omega.research.training.data_collection build
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# ── Paths ──────────────────────────────────────────────────────

_CACHE_DIR = Path.home() / ".omega" / "training"
_OMEGA_BENCH_PATH = Path(__file__).resolve().parents[4] / "omega" / "benchmark" / "suite.py"


@dataclass
class DatasetSource:
    """A single training data source."""
    name: str
    hf_id: str | None          # HuggingFace dataset ID (None for local)
    cache_path: str | Path     # Local cache subdirectory
    max_samples: int           # Max samples to keep
    has_labels: bool           # Has built-in difficulty labels
    license: str = "Apache-2.0"


# ── Known sources ──────────────────────────────────────────────

_DATASETS: list[DatasetSource] = [
    DatasetSource(
        name="Goedel-Prover-SFT",
        hf_id="Goedel-LM/Goedel-Prover-SFT",
        cache_path="goedel_sft",
        max_samples=50_000,
        has_labels=False,
    ),
    DatasetSource(
        name="RL_dataset_V2",
        hf_id="Goedel-LM/RL_dataset_V2",
        cache_path="rl_v2",
        max_samples=50_000,
        has_labels=True,   # Contains difficulty scores!
    ),
    DatasetSource(
        name="Lean-workbook-proofs",
        hf_id="Goedel-LM/Lean-workbook-proofs",
        cache_path="lean_workbook",
        max_samples=100_000,
        has_labels=True,   # Contains curated difficulty estimates
    ),
    DatasetSource(
        name="Herald_statements",
        hf_id="FrenzyMath/Herald_statements",
        cache_path="herald",
        max_samples=20_000,
        has_labels=False,
    ),
    # Local: Omega benchmark suite (gold-standard labels)
    DatasetSource(
        name="Omega_Benchmarks",
        hf_id=None,
        cache_path="omega_bench",
        max_samples=25,
        has_labels=True,   # Hand-labeled
    ),
]

# ── Gold-standard labels (Omega Benchmarks) ────────────────────

_GOLD_LABELS: dict[str, int] = {
    # Tier 1 (0 = simple)
    "trivial_true":       0,
    "rfl_simple":         0,
    "and_true":           0,
    "or_true":            0,
    "eq_refl":            0,
    # Tier 2 (1 = medium)
    "dbl_zero":           1,
    "add_one":            1,
    "sq_zero":            1,
    "succ_eq_add_one":    1,
    "add_self_zero":      1,
    "sub_self":           1,
    "zero_le":            1,
    "one_mul_custom":     1,
    "le_refl":            1,
    "zero_add_custom":    1,
    # Tier 3 (2 = hard)
    "double_succ":        2,
    "add_assoc_custom":   2,
    "mul_comm_custom":    2,
    "add_comm_custom":    2,
    "mul_add_custom":     2,
    "add_mul_custom":     2,
    "mul_assoc_custom":   2,
    "pow_two_custom":     2,
    "succ_mul_custom":    2,
    "fact_pos":           2,
}


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════


def scan() -> dict[str, Any]:
    """Check which datasets are already cached locally."""
    status: dict[str, Any] = {"cached": [], "missing": []}
    for ds in _DATASETS:
        path = _CACHE_DIR / ds.cache_path
        if ds.hf_id and path.exists() and list(path.iterdir()):
            n = count_examples(path)
            status["cached"].append({"name": ds.name, "path": str(path), "examples": n})
        elif not ds.hf_id:
            status["cached"].append({"name": ds.name, "source": "local", "examples": 25})
        else:
            status["missing"].append({"name": ds.name, "hf_id": ds.hf_id})
    return status


def download_single(ds: DatasetSource) -> Path:
    """Download a single HF dataset using hfpclawer or datasets lib."""
    out_dir = _CACHE_DIR / ds.cache_path
    if out_dir.exists() and list(out_dir.iterdir()):
        print(f"[{ds.name}] Already cached at {out_dir}")
        return out_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{ds.name}] Downloading {ds.hf_id} → {out_dir} ...")

    # Try hfpclawer first (Hermes native), fall back to datasets
    try:
        result = subprocess.run(
            ["hfpclawer", "download", ds.hf_id,
             "--output", str(out_dir),
             "--max_samples", str(ds.max_samples)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            print(f"[{ds.name}] Downloaded via hfpclawer")
            return out_dir
        print(f"[{ds.name}] hfpclawer failed: {result.stderr[:200]}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[{ds.name}] hfpclawer unavailable: {e}")

    # Fallback: Python datasets library
    try:
        from datasets import load_dataset
        dataset = load_dataset(ds.hf_id, split="train", trust_remote_code=True)
        n = min(len(dataset), ds.max_samples)
        sampled = dataset.select(range(n))

        # Save as JSONL
        jsonl_path = out_dir / "data.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for row in sampled:
                f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        print(f"[{ds.name}] Saved {n} examples to {jsonl_path}")
        meta = {"source": ds.hf_id, "n": n, "saved_at": time.time()}
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return out_dir
    except ImportError:
        print(f"[{ds.name}] ERROR: datasets library not installed. "
              "Install with: pip install datasets")
    except Exception as e:
        print(f"[{ds.name}] ERROR: {e}")

    return out_dir


def download_all(max_per_source: int | None = None) -> None:
    """Download all datasets."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for ds in _DATASETS:
        if ds.hf_id is None:
            continue
        if max_per_source:
            ds.max_samples = min(ds.max_samples, max_per_source)
        download_single(ds)


def collect_omega_benchmarks() -> list[dict]:
    """Extract theorem headers + gold labels from Omega's benchmark suite."""
    results = []
    # Import benchmark theorem lists
    sys.path.insert(0, str(_OMEGA_BENCH_PATH.parent.parent))  # omega/
    from omega.benchmark.suite import TIER_1, TIER_2, TIER_3, TIER_4

    for tier_idx, theorems in [(0, TIER_1), (1, TIER_2), (2, TIER_3)]:
        for theorem in theorems:
            name = theorem["name"]
            gold = _GOLD_LABELS.get(name)
            if gold is None:
                gold = tier_idx  # fallback to benchmark tier
            results.append({
                "name": name,
                "header": theorem["header"],
                "imports": theorem.get("imports", ""),
                "tier": gold,
                "source": "omega_bench",
            })

    # TIER 4 (MiniF2F) — labeled as hard (2)
    for theorem in TIER_4:
        results.append({
            "name": theorem["name"],
            "header": theorem["header"],
            "imports": theorem.get("imports", "import Mathlib"),
            "tier": 2,  # MiniF2F → hard
            "source": "omega_bench",
        })

    return results


def count_examples(path: Path) -> int:
    """Count examples in a dataset cache directory."""
    total = 0
    for pattern in ("*.jsonl", "*.json", "*.parquet"):
        for f in path.rglob(pattern):
            if f.name == "meta.json":
                continue
            if f.suffix == ".jsonl":
                total += sum(1 for _ in f.read_text().splitlines() if _.strip())
            elif f.suffix == ".json":
                # Large JSON arrays
                try:
                    data = json.loads(f.read_text())
                    if isinstance(data, list):
                        total += len(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
    return total


def summary() -> str:
    """Print a summary table of available data."""
    lines = [
        "=" * 60,
        "  ModelRouter Training Data — Inventory",
        "=" * 60,
        f"  {'Source':<26} {'Examples':>10} {'Labels':>8}",
        "  " + "-" * 44,
    ]
    for ds in _DATASETS:
        path = _CACHE_DIR / ds.cache_path
        n = count_examples(path) if path.exists() else 0
        lbl = "✅ gold" if ds.has_labels else "❌ proxy"
        status = "cached" if n > 0 else "missing"
        lines.append(f"  {ds.name:<26} {n:>6} {lbl:>12}  ({status})")

    lines.append("")
    lines.append(f"  Gold: 25 + 5 MiniF2F = 30 expertly labeled")
    lines.append(f"  Proxy: RL_V2 + Lean-workbook have built-in difficulty")
    lines.append(f"  Total target: ~220K trainable examples")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if cmd == "scan":
        status = scan()
        print(f"Cached datasets: {len(status['cached'])}")
        for ds in status["cached"]:
            print(f"  ✅ {ds['name']}: {ds['examples']} examples")
        print(f"Missing: {len(status['missing'])}")
        for ds in status["missing"]:
            print(f"  ❌ {ds['name']} (hf_id={ds['hf_id']})")
        print(summary())

    elif cmd == "download":
        download_all()
        print("\nDownload complete.")
        print(summary())

    elif cmd == "build":
        bench_data = collect_omega_benchmarks()
        print(f"Collected {len(bench_data)} Omega benchmark examples")
        out = _CACHE_DIR / "omega_bench" / "data.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for row in bench_data:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"Saved to {out}")
        print(summary())

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python -m omega.research.training.data_collection "
              "[scan|download|build]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

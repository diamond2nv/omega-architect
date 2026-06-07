#!/usr/bin/env python3
"""Standardized data layer for Omega experiments.

All experiment data, benchmarks, and proof caches use **JSONL** format
(one JSON object per line, append-friendly).

Future: converts directly to HuggingFace ``datasets.Dataset`` for
RLVR training, analysis, and model fine-tuning.

Design rationale
----------------
- **JSONL** (not JSON): append-only, streaming-friendly, process-linearly.
  Each theorem attempt is an independent record — no nested arrays to
  reconstruct on crash.  Compatible with ``jsonlines``, ``pandas.read_json``,
  and ``datasets.load_dataset`` (``format='json'`` with ``split='train'``).

- **Standard fields**: Every record has ``theorem_id``, ``timestamp``,
  ``source`` (which experiment/config), and typed data columns.  This
  ensures any downstream tool (HF trainer, analysis notebook, dashboard)
  can consume records without per-file schema negotiation.

- **HuggingFace bridge**: :func:`load_as_hf_dataset` returns a
  ``datasets.Dataset``.  Use for:
    - Training RLVR verifiers on proof outcomes
    - Analyzing error distributions with HF ``Dataset.filter()``
    - Exporting to Parquet for large-scale storage

Usage::

    from omega.data import RecordWriter, ProofRecord

    with RecordWriter("~/.omega/experiments/run.jsonl") as w:
        w.write(ProofRecord(
            theorem_id="mathd_algebra_141",
            succeeded=True,
            n_attempts=15,
            errors=["unsolved goals", ...],
            elapsed_s=199.4,
            config={"model": "qwen3-coder:30b", "num_samples": 2},
        ))

    # Future: convert to HuggingFace Dataset
    # from omega.data import load_as_hf_dataset
    # ds = load_as_hf_dataset("~/.omega/experiments/run.jsonl")
    # ds.filter(lambda r: r["succeeded"]).select_columns(["theorem_id", "elapsed_s"])
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("omega.data")

# -- Date types ----------------------------------------------------


@dataclass
class ProofRecord:
    """A single theorem-proving attempt result.

    One JSONL line = one :class:`ProofRecord`.
    Fields are kept flat for direct HuggingFace ``Dataset`` compatibility.

    Parameters
    ----------
    theorem_id : str
        Unique theorem identifier (e.g. ``"mathd_algebra_141"``).
    succeeded : bool
        Whether a T2-verified proof was found.
    n_attempts : int
        Total LLM + template attempts across all correction rounds.
    n_errors : int
        Total Lean compile errors across all attempts.
    elapsed_s : float
        Wall-clock seconds for the entire theorem run.
    config_model : str
        Model name used (e.g. ``"qwen3-coder:30b"``).
    config_num_samples : int
        ``num_samples`` parameter.
    config_correction_rounds : int
        ``max_correction_rounds`` parameter.
    error_unsolved_goal : int, optional
        Count of ``unsolved goal`` errors.
    error_syntax : int, optional
        Count of syntax errors.
    error_type_mismatch : int, optional
        Count of type mismatch errors.
    error_unknown_identifier : int, optional
        Count of unknown identifier errors.
    error_other : int, optional
        Count of other (uncategorized) errors.
    top_errors : list[str], optional
        Unique error first-lines (up to 5).
    proof_preview : str, optional
        First 200 characters of successful proof, if any.
    timestamp : str, optional
        ISO timestamp. Auto-set if omitted.
    source : str, optional
        Experiment name or config label.
    **extra : Any
        Additional metadata (passed through to the JSON line).
    """

    theorem_id: str
    succeeded: bool
    n_attempts: int = 0
    n_errors: int = 0
    elapsed_s: float = 0.0

    # Config dimensions (flat for HF Dataset column access)
    config_model: str = ""
    config_num_samples: int = 0
    config_correction_rounds: int = 0

    # Error breakdown
    error_unsolved_goal: int = 0
    error_syntax: int = 0
    error_type_mismatch: int = 0
    error_unknown_identifier: int = 0
    error_other: int = 0

    # Human-readable
    top_errors: list[str] = field(default_factory=list)
    proof_preview: str = ""

    # Provenance
    timestamp: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class AttemptRecord:
    """A single Lean compile attempt (one T2 call).

    Finer-grained than :class:`ProofRecord` — one per ``compile_fn`` call.
    Useful for RLVR training (each attempt = one reward signal).
    """

    theorem_id: str
    attempt_index: int
    round_index: int
    strategy: str  # "llm" | "template" | "error_based"
    suggested_tactic: str
    lean_code: str
    verified: bool
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    config_model: str = ""
    timestamp: str = ""


# -- Writer --------------------------------------------------------


class RecordWriter:
    """Append-only JSONL writer.

    Thread-safe for single-process use.  Each ``write()`` call flushes
    immediately so partial results survive crashes.

    Usage::

        with RecordWriter("~/.omega/experiments/run.jsonl") as w:
            w.write(proof_record)
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._f: Any = None

    def __enter__(self) -> RecordWriter:
        self._f = open(self._path, "a", encoding="utf-8")
        return self

    def __exit__(self, *args: Any) -> None:
        if self._f:
            self._f.close()

    def write(self, record: ProofRecord | AttemptRecord | dict[str, Any]) -> None:
        """Write one record as a JSONL line.

        Accepts :class:`ProofRecord`, :class:`AttemptRecord`, or a raw dict.
        """
        data = asdict(record) if isinstance(record, (ProofRecord, AttemptRecord)) else record
        line = json.dumps(data, ensure_ascii=False, default=str)
        self._f.write(line + "\n")
        self._f.flush()


# -- Reader --------------------------------------------------------


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load all records from a JSONL file.

    Returns list of dicts.  For large files, iterate manually::

        with open(path) as f:
            for line in f:
                record = json.loads(line)
                process(record)
    """
    path = Path(path).expanduser()
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_as_hf_dataset(path: str | Path, **kwargs: Any) -> Any:
    """Load JSONL as a HuggingFace ``datasets.Dataset``.

    Requires ``datasets`` installed (``pip install datasets``).
    All extra ``**kwargs`` are passed to ``datasets.load_dataset()``.

    Returns ``datasets.Dataset`` or ``None`` if ``datasets`` not installed.

    Usage::

        ds = load_as_hf_dataset("~/.omega/experiments/run.jsonl")
        print(ds.filter(lambda r: r[\"succeeded\"]).num_rows)
    """
    try:
        from datasets import load_dataset as hf_load_dataset

        path = str(Path(path).expanduser())
        ds = hf_load_dataset("json", data_files=path, split="train", **kwargs)
        return ds
    except ImportError:
        logger.warning("``datasets`` not installed. Run: pip install datasets")
        return None


# -- Conversion helper ---------------------------------------------


def proof_records_from_goedel_result(
    theorem_id: str,
    result: Any,
    config: dict[str, Any] | None = None,
) -> tuple[ProofRecord, list[AttemptRecord]]:
    """Convert a ``GoedelResult`` to structured records.

    Returns ``(proof_record, attempt_records)`` for JSONL persistence.

    Parameters
    ----------
    theorem_id : str
        Theorem identifier.
    result : GoedelResult
        Output from ``GoedelProver.run()``.
    config : dict or None
        Experiment configuration (model, num_samples, etc.).

    Returns
    -------
    tuple[ProofRecord, list[AttemptRecord]]
    """
    cfg = config or {}
    all_errors = []
    for att in result.attempts:
        all_errors.extend(att["errors"])

    # Error counts
    e_unsolved = sum(1 for e in all_errors if "unsolved" in e.lower())
    e_syntax = sum(1 for e in all_errors if ("unexpected" in e.lower() or "expected" in e.lower()))
    e_type = sum(1 for e in all_errors if "type mismatch" in e.lower())
    e_unknown_id = sum(1 for e in all_errors if "unknown identifier" in e.lower())
    e_other = len(all_errors) - e_unsolved - e_syntax - e_type - e_unknown_id

    # Top unique errors (first line only)
    seen = set()
    top_errors = []
    for att in result.attempts:
        for err in att["errors"]:
            first = err.split("\n")[0][:120]
            if first not in seen:
                seen.add(first)
                top_errors.append(first)
            if len(top_errors) >= 5:
                break
        if len(top_errors) >= 5:
            break

    proof_record = ProofRecord(
        theorem_id=theorem_id,
        succeeded=result.succeeded or False,
        n_attempts=result.n_attempts,
        n_errors=len(all_errors),
        elapsed_s=result.timings.get("total_s", 0.0),
        config_model=cfg.get("model", ""),
        config_num_samples=cfg.get("num_samples", 0),
        config_correction_rounds=cfg.get("max_correction_rounds", 0),
        error_unsolved_goal=e_unsolved,
        error_syntax=e_syntax,
        error_type_mismatch=e_type,
        error_unknown_identifier=e_unknown_id,
        error_other=e_other,
        top_errors=top_errors,
        proof_preview=(result.proof or "")[:200],
        source=cfg.get("source", ""),
    )

    attempt_records = []
    for i, att in enumerate(result.attempts):
        attempt_records.append(
            AttemptRecord(
                theorem_id=theorem_id,
                attempt_index=i + 1,
                round_index=att["round"],
                strategy=att.get("strategy", "llm"),
                suggested_tactic=att.get("tactic", ""),
                lean_code=att.get("lean_code", ""),
                verified=att.get("verified", False),
                errors=att.get("errors", []),
                elapsed_s=att.get("elapsed_s", 0.0),
                config_model=cfg.get("model", ""),
            )
        )

    return proof_record, attempt_records

"""Centralized logging infrastructure for Ω-Architect.

Provides rotating file handlers, structured log format, and a simple
initialization function that all modules call at startup.

Features:
  - Rotating file handler (10 MB per file, 5 backups) in /logs/
  - tail -f friendly format: ``TIMESTAMP [LEVEL] module: message``
  - Console handler for interactive runs
  - Benchmark-specific JSON log for machine consumption
  - Automatic log directory creation

Usage::

    from omega.logger import init_logging, get_benchmark_logger

    init_logging()  # Call once at startup
    logger = logging.getLogger("omega.search.passk")
    logger.info("Processing theorem %s", theorem_name)

For benchmarks that need machine-readable output::

    blog = get_benchmark_logger("minif2f")
    blog.log_progress(index=34, total=244, name="mathd_algebra_478",
                      difficulty_S=3.2, k=4, n_passed=1)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOG_DIR = _PROJECT_ROOT / "logs"

# ── Format ──────────────────────────────────────────────────────

_CONSOLE_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
_FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)-28s %(filename)s:%(lineno)-4d | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _ensure_log_dir(log_dir: str | Path) -> Path:
    """Create log directory if it does not exist."""
    path = Path(log_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def init_logging(
    log_dir: str | Path | None = None,
    level: int = logging.INFO,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
    capture_warnings: bool = True,
) -> Path:
    """Initialize the Omega logging system.

    Sets up two handlers:
      1. Rotating file handler — DEBUG level, /logs/omega.log (rotated)
      2. Console handler — INFO level, stderr

    Parameters
    ----------
    log_dir : str or Path, optional
        Directory for log files.  Default: ``<project_root>/logs/``.
    level : int
        Root logger level (default: ``logging.INFO``).
    console_level : int
        Console output level (default: ``logging.INFO``).
    file_level : int
        File output level (default: ``logging.DEBUG``).
    max_bytes : int
        Maximum file size before rotation (default 10 MB).
    backup_count : int
        Number of rotated backup files to keep (default 5).
    capture_warnings : bool
        Redirect ``warnings.warn`` to the logging system (default True).

    Returns
    -------
    Path
        The resolved log directory.
    """
    log_path = _ensure_log_dir(log_dir or _DEFAULT_LOG_DIR)
    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers on repeated calls
    if root.handlers:
        return log_path

    # -- File handler (rotating) --
    log_file = log_path / "omega.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _DATE_FORMAT))
    root.addHandler(file_handler)

    # -- Console handler (stderr) --
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    # Capture Python warnings
    if capture_warnings:
        logging.captureWarnings(True)

    logging.getLogger(__name__).info(
        "Logging initialised: dir=%s, file=%s (max_bytes=%d, backups=%d)",
        log_path, log_file, max_bytes, backup_count,
    )
    return log_path


# ── Benchmark JSON logger ───────────────────────────────────────


class BenchmarkJsonLogger:
    """Machine-readable JSON logger for long-running benchmarks.

    Writes one JSON object per line to ``<log_dir>/benchmark_<name>.jsonl``.
    Each line is a complete JSON record, making it safe for tail -f and
    line-oriented processing.

    Parameters
    ----------
    name : str
        Benchmark name (e.g. ``"minif2f"``, ``"putnambench"``).
    log_dir : str or Path, optional
        Output directory.  Default: ``<log_dir>/``.
    """

    def __init__(
        self,
        name: str,
        log_dir: str | Path | None = None,
    ):
        self.name = name
        self.log_path = (
            _ensure_log_dir(log_dir or _DEFAULT_LOG_DIR)
            / f"benchmark_{name}.jsonl"
        )
        self._file: Any = None  # Lazy open
        self._logger = logging.getLogger(f"omega.benchmark.{name}")

    def _open(self) -> None:
        if self._file is None:
            self._file = open(self.log_path, "a", encoding="utf-8")

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def log_progress(
        self,
        index: int,
        total: int,
        name: str,
        difficulty_S: float = 0.0,
        k: int = 0,
        n_passed: int = 0,
        elapsed_s: float = 0.0,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Log one theorem result to the JSONL file.

        Parameters are self-explanatory — each maps to the corresponding
        field in the benchmark report schema.
        """
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "theorem_result",
            "benchmark": self.name,
            "index": index,
            "total": total,
            "theorem": name,
            "difficulty_S": round(difficulty_S, 2),
            "k": k,
            "n_passed": n_passed,
            "pass_rate": round(n_passed / max(k, 1), 4),
            "elapsed_s": round(elapsed_s, 2),
            "cost_usd": round(cost_usd, 6),
        }
        if error:
            record["error"] = error

        self._open()
        line = json.dumps(record, ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()
        self._logger.debug("Logged progress: %s [%d/%d]", name, index, total)

    def log_summary(
        self,
        total: int,
        proved: int,
        total_elapsed_s: float,
        total_cost_usd: float,
        avg_S: float = 0.0,
        avg_k: float = 0.0,
    ) -> None:
        """Log a final benchmark summary."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "benchmark_summary",
            "benchmark": self.name,
            "total": total,
            "proved": proved,
            "prove_rate": round(proved / max(total, 1), 4),
            "total_elapsed_s": round(total_elapsed_s, 2),
            "total_cost_usd": round(total_cost_usd, 6),
            "avg_difficulty_S": round(avg_S, 2),
            "avg_k": round(avg_k, 1),
        }
        self._open()
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def __enter__(self) -> BenchmarkJsonLogger:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def get_benchmark_logger(name: str) -> BenchmarkJsonLogger:
    """Convenience factory — returns a ``BenchmarkJsonLogger`` instance."""
    return BenchmarkJsonLogger(name)

"""omega-plugin/commands/benchmark.py — ``/omega-benchmark`` slash command.

Runs the MiniF2F benchmark (T1 + T2) and returns a summary.
"""

from __future__ import annotations

import logging
import shlex
from contextlib import suppress

from .._bridge import OmegaBridge

logger = logging.getLogger("omega-plugin.benchmark")

_DEFAULT_TIMEOUT = 600


def register_benchmark_command(ctx) -> None:
    """Register the ``/omega-benchmark`` slash command."""

    def handle(raw_args: str) -> str:
        """Handler for ``/omega-benchmark [--split test|valid] [--max N]``."""
        args = _parse_args(raw_args)

        bridge = OmegaBridge(ctx)
        result = bridge.benchmark(
            split=args.get("split", "test"),
            limit=args.get("max", 10),
            timeout_s=args.get("timeout", _DEFAULT_TIMEOUT),
        )
        return bridge.format_result_for_chat(result)

    ctx.register_command(
        name="omega-benchmark",
        handler=handle,
        description="Run MiniF2F benchmark to evaluate proof engine performance",
        args_hint="[--split test|valid] [--max N] [--timeout N]",
    )


def _parse_args(raw: str) -> dict:
    """Parse benchmark command arguments."""
    raw = raw.strip()
    kwargs: dict = {"split": "test", "max": 10, "timeout": _DEFAULT_TIMEOUT}

    if not raw:
        return kwargs

    parts = shlex.split(raw)
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--split" and i + 1 < len(parts):
            kwargs["split"] = parts[i + 1]
            i += 2
        elif part == "--max" and i + 1 < len(parts):
            with suppress(ValueError):
                kwargs["max"] = max(1, min(int(parts[i + 1]), 244))
            i += 2
        elif part == "--timeout" and i + 1 < len(parts):
            with suppress(ValueError):
                kwargs["timeout"] = int(parts[i + 1])
            i += 2
        else:
            i += 1
    return kwargs

"""omega-plugin/commands/prove.py — ``/omega-prove`` slash command.

Proves a Lean theorem using omega-core's proof engines.
"""

from __future__ import annotations

import logging
import shlex

from .._bridge import OmegaBridge

logger = logging.getLogger("omega-plugin.prove")

# Default timeout in seconds
_DEFAULT_TIMEOUT = 300


def register_prove_command(ctx) -> None:
    """Register the ``/omega-prove`` slash command."""

    def handle(raw_args: str) -> str:
        """Handler for ``/omega-prove <theorem_header>``.

        Usage::

            /omega-prove theorem add_zero (n : ℕ) : n + 0 = n :=
            /omega-prove --mode goedel --attempts 8 theorem add_one_eq ...
            /omega-prove --file /path/to/theorem.lean
        """
        args = _parse_args(raw_args)
        if args is None:
            return _usage()

        theorem = args["theorem"]
        mode = args.get("mode", "auto")
        attempts = args.get("attempts", 6)

        bridge = OmegaBridge(ctx)
        result = bridge.prove(
            theorem,
            mode=mode,
            attempts=attempts,
        )
        return bridge.format_result_for_chat(result)

    ctx.register_command(
        name="omega-prove",
        handler=handle,
        description="Prove a Lean theorem using Ω-Architect proof engines",
        args_hint="<theorem header> | --mode auto|goedel|rethlas|archon --attempts N --file <path>",
    )


# ── argument parsing ────────────────────────────────────────────


def _parse_args(raw: str) -> dict | None:
    """Simple argument parser for the prove command.

    Recognises ``--key value`` pairs and a trailing positional theorem.
    """
    raw = raw.strip()
    if not raw:
        return None

    parts = shlex.split(raw)
    kwargs: dict = {}

    i = 0
    while i < len(parts):
        part = parts[i]

        if part == "--mode" and i + 1 < len(parts):
            kwargs["mode"] = parts[i + 1]
            i += 2
        elif part == "--attempts" and i + 1 < len(parts):
            try:
                kwargs["attempts"] = int(parts[i + 1])
            except ValueError:
                return None
            i += 2
        elif part == "--timeout" and i + 1 < len(parts):
            try:
                kwargs["timeout"] = int(parts[i + 1])
            except ValueError:
                return None
            i += 2
        elif part == "--file" and i + 1 < len(parts):
            try:
                from pathlib import Path
                file_path = Path(parts[i + 1]).expanduser().resolve()
                if not file_path.is_file():
                    return None
                kwargs["theorem"] = file_path.read_text().strip()
            except (OSError, ValueError):
                return None
            i += 2
        else:
            # First positional argument is the theorem header
            kwargs["theorem"] = " ".join(parts[i:])
            break

    if "theorem" not in kwargs:
        return None
    return kwargs


def _usage() -> str:
    return (
        "Usage:\n"
        "  /omega-prove theorem <header>\n"
        "  /omega-prove --mode goedel --attempts 8 theorem <header>\n"
        "  /omega-prove --file /path/to/theorem.lean\n"
        "\n"
        "Options:\n"
        "  --mode <str>      Prover strategy (auto|goedel|rethlas|archon, default: auto)\n"
        "  --attempts <int>  Number of proof attempts (default: 6)\n"
        "  --timeout <int>   Max seconds (default: 300)\n"
        "  --file <path>     Read theorem from file instead"
    )

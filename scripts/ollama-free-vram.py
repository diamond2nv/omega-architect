#!/usr/bin/env python3
"""Force Ollama to unload all models from GPU VRAM.

Usage:
    python scripts/ollama-free-vram.py
    python scripts/ollama-free-vram.py --host http://localhost:11434

Exit code: 0 if VRAM was freed, 1 if no loaded models found.
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError


def ollama_host() -> str:
    if "--host" in sys.argv:
        idx = sys.argv.index("--host")
        return sys.argv[idx + 1]
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _post(url: str, data: dict) -> dict:
    req = Request(url, data=json.dumps(data).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get(url: str) -> dict:
    with urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def main() -> int:
    host = ollama_host()

    # Step 1: Check what's loaded
    print(f"[Ollama] Checking loaded models at {host} ...")
    try:
        ps = _get(f"{host}/api/ps")
    except URLError as e:
        print(f"[Ollama] ERROR: Cannot reach {host} — {e}", file=sys.stderr)
        return 2

    loaded = ps.get("models", [])
    if not loaded:
        print("[Ollama] No models loaded. VRAM is free.")
        return 1

    total_freed = 0
    for m in loaded:
        name = m["name"]
        size_gb = m.get("size", 0) / 1e9
        print(f"[Ollama] Found loaded: {name} ({size_gb:.1f} GB)")

        # Step 2: Send dummy generate with keep_alive=0
        print(f"[Ollama] Unloading {name} ...")
        try:
            _post(f"{host}/api/generate", {
                "model": name,
                "prompt": "",
                "keep_alive": 0,
            })
            total_freed += size_gb
            print(f"[Ollama]    ✓ Unloaded")
        except Exception as e:
            print(f"[Ollama]    ✗ Failed: {e}", file=sys.stderr)

    # Step 3: Verify
    time.sleep(1)
    ps2 = _get(f"{host}/api/ps")
    still_loaded = ps2.get("models", [])
    if still_loaded:
        for m in still_loaded:
            print(f"[Ollama] WARNING: {m['name']} still loaded ({m.get('size',0)/1e9:.1f} GB)")
        print(f"[Ollama] Freed approx {total_freed:.1f} GB (partial)")
    else:
        print(f"[Ollama] ✓ All models unloaded. Freed ~{total_freed:.1f} GB")

    return 0


if __name__ == "__main__":
    sys.exit(main())

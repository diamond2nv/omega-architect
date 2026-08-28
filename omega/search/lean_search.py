#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lean-native search — calls lean-lsp-mcp's loogle/leansearch tools via stdio.

Provides a clean Python API for Lean theorem search, backed by:
  - loogle: search by type signature
  - leansearch: search by natural language
  - leanfinder: semantic search

Each search spawns a fresh MCP subprocess (caching in matlas_cache.py
shares the same JSONL cache, so repeated identical queries hit cache).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

MCP_BIN = os.path.expanduser("~/.local/bin/lean-lsp-mcp")
LEAN_PROJECT = os.environ.get(
    "LEAN_PROJECT_PATH",
    "<project-dir>",
)


def _mcp_request(method: str, params: dict | None = None,
                 timeout: int = 30) -> dict:
    """Spawn lean-lsp-mcp, send one JSON-RPC request, return result.

    This is SLOW (~1-2s per call due to process spawn + Lean init).
    Cache aggressively via matlas_cache.
    """
    proc = subprocess.Popen(
        [MCP_BIN, "--lean-project-path", LEAN_PROJECT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    req_id = 1
    try:
        # Step 1: Initialize
        init_req = {
            "jsonrpc": "2.0", "id": req_id, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "lean-search-client", "version": "1.0"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()

        # Read init response (read line by line until we find the matching id)
        init_resp = None
        for _ in range(20):
            line = proc.stdout.readline()
            if not line:
                break
            try:
                resp = json.loads(line.strip())
                if resp.get("id") == req_id:
                    init_resp = resp
                    break
            except json.JSONDecodeError:
                pass

        if init_resp is None:
            proc.kill()
            return {"error": "no init response"}

        # Step 2: Send tools/call
        tool_params = params or {}
        call_req = {
            "jsonrpc": "2.0", "id": req_id + 1, "method": "tools/call",
            "params": {"name": method, "arguments": tool_params},
        }
        proc.stdin.write(json.dumps(call_req) + "\n")
        proc.stdin.flush()

        # Read tools/call response
        call_resp = None
        for _ in range(20):
            line = proc.stdout.readline()
            if not line:
                break
            try:
                resp = json.loads(line.strip())
                if resp.get("id") == req_id + 1:
                    call_resp = resp
                    break
            except json.JSONDecodeError:
                pass

        proc.stdin.close()
        proc.wait(timeout=5)
        return call_resp if call_resp else {"error": "no tools/call response"}

    except Exception as e:
        proc.kill()
        return {"error": str(e)}


def loogle(query: str, num_results: int = 5) -> list[dict]:
    """Search Mathlib by type signature.

    Supports loogle's query syntax:
      - `Real.sin` — find lemmas mentioning Real.sin
      - `?a + 0 = ?a` — find lemmas matching type pattern
      - `\"differ\"` — substring match on lemma name
      - `|- _ < _ → tsum _ < tsum _` — match conclusion shape

    Returns list of dicts with keys: name, type, doc.
    """
    resp = _mcp_request("lean_loogle", {
        "query": query,
        "num_results": num_results,
    })
    return _parse_mcp_tool_result(resp)


def leansearch(query: str, num_results: int = 5) -> list[dict]:
    """Search Mathlib by natural language.

    Best for: "sum of two even numbers is even",
    "Cauchy-Schwarz inequality"
    """
    resp = _mcp_request("lean_leansearch", {
        "query": query,
        "num_results": num_results,
    })
    return _parse_mcp_tool_result(resp)


def leanfinder(query: str, num_results: int = 5) -> list[dict]:
    """Semantic search by mathematical meaning.

    Best for: proof state text, "commutativity of addition on natural numbers"
    """
    resp = _mcp_request("lean_leanfinder", {
        "query": query,
        "num_results": num_results,
    })
    return _parse_mcp_tool_result(resp)


def _parse_mcp_tool_result(resp: dict) -> list[dict]:
    """Extract content from MCP tool call response.

    The response format is:
    {
      "jsonrpc": "2.0",
      "id": N,
      "result": {
        "content": [
          {"type": "text", "text": "..."},
          {"type": "resource", "resource": {...}},
        ],
        "isError": false
      }
    }
    """
    if "error" in resp:
        return []

    result = resp.get("result", {})
    if result.get("isError"):
        return []

    content = result.get("content", [])
    parsed = []
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "").strip()
            if text:
                parsed.append({"type": "text", "text": text})
        elif item.get("type") == "resource":
            resource = item.get("resource", {})
            parsed.append({
                "type": "resource",
                "name": resource.get("name", ""),
                "text": resource.get("text", ""),
            })
    return parsed

def format_lean_results(results: list[dict], top_k: int = 5) -> str:
    """Format loogle/leansearch results as Lean lemma context for prompts.

    Results contain JSON with items array of {name, type}.
    Returns formatted string like:
    Lean lemmas from Mathlib:
      add_zero: ∀ (a : ℕ), a + 0 = a
      add_comm: ∀ (a b : ℕ), a + b = b + a
    """
    if not results:
        return ""

    # Parse the JSON items from the text response
    lemmas = []
    for r in results:
        text = r.get("text", "")
        try:
            data = json.loads(text)
            items = data.get("items", []) if isinstance(data, dict) else data if isinstance(data, list) else []
            for item in items:
                name = item.get("name", "")
                typ = item.get("type", "")
                if name:
                    lemmas.append((name, typ))
        except (json.JSONDecodeError, TypeError):
            pass

    if not lemmas:
        return ""

    lines = ["Lean lemmas from Mathlib:"]
    for name, typ in lemmas[:top_k]:
        typ_short = typ[:120] if typ else ""
        lines.append(f"  • {name}: {typ_short}")

    return "\n".join(lines)


# ── Main (CLI test) ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "add_zero"
    print(f"Searching: {query}")
    print("=" * 40)

    results = loogle(query)
    if results:
        print(format_lean_results(results))
    else:
        print("No results from loogle, trying leansearch...")
        results = leansearch(query)
        if results:
            print(format_lean_results(results))
        else:
            print("No results from any backend")

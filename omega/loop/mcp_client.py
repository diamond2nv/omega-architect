"""MCP client wrapper for lean-lsp-mcp.

Provides async call_tool that the Inner Loop can use to perform
real Mathlib searches via loogle/leansearch etc.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("omega.loop.mcp")


@dataclass
class McpToolResult:
    """Result from an MCP tool call."""
    success: bool
    content: str
    tool_name: str
    elapsed_ms: int = 0
    is_error: bool = False


# ── Tool rate limits (from lean-lsp-mcp docs) ───────────────
# lean_leansearch: 3req/30s
# lean_loogle:      ~10req/min (remote)
# lean_leanfinder:  10req/30s
# lean_state_search: 6req/30s
# lean_hammer_premise: 6req/30s

_TOOL_RATE_LIMITS: dict[str, tuple[int, float]] = {
    "lean_leansearch": (3, 30.0),
    "lean_loogle": (5, 30.0),
    "lean_leanfinder": (10, 30.0),
    "lean_state_search": (6, 30.0),
    "lean_hammer_premise": (6, 30.0),
    "lean_run_code": (10, 10.0),
    "lean_local_search": (20, 10.0),
}


class McpClient:
    """Async MCP client wrapping lean-lsp-mcp.
    
    Manages a single persistent connection to the MCP server.
    Tool calls are rate-limited per tool.
    
    Usage:
        client = McpClient()
        await client.initialize()
        result = await client.call_tool("lean_loogle", {"query": "add_comm"})
        await client.close()
    """
    
    def __init__(self, project_path: str = os.path.expanduser("~/lean-paper-plane")):
        self.project_path = project_path
        self._session: ClientSession | None = None
        self._read = None
        self._write = None
        self._client_ctx = None
        self._session_ctx = None
        
        # Rate limit tracking: tool_name -> [timestamps]
        self._rate_history: dict[str, list[float]] = {}
    
    async def initialize(self, timeout: float = 30.0) -> list[str]:
        """Connect to MCP server and initialize session.
        
        Returns list of available tool names.
        """
        # Ensure elan/lake on PATH for the MCP subprocess
        env = dict(**os.environ)
        elan_bin = os.path.expanduser("~/.elan/bin")
        if elan_bin not in env.get("PATH", ""):
            env["PATH"] = elan_bin + ":" + env.get("PATH", "")
        lean_stable_bin = os.path.expanduser("~/.elan/toolchains/stable/bin")
        if lean_stable_bin not in env.get("PATH", ""):
            env["PATH"] = lean_stable_bin + ":" + env.get("PATH", "")

        server_params = StdioServerParameters(
            command=os.environ.get("LEAN_LSP_MCP", os.path.expanduser("~/.local/bin/lean-lsp-mcp")),
            args=[
                "--transport", "stdio",
                "--lean-project-path", self.project_path,
            ],
            env=env,
        )
        
        self._client_ctx = stdio_client(server_params)
        self._read, self._write = await self._client_ctx.__aenter__()
        
        self._session_ctx = ClientSession(self._read, self._write)
        self._session = await self._session_ctx.__aenter__()
        
        await self._session.initialize()
        
        # Get available tools
        tools_result = await self._session.list_tools()
        tool_names = [t.name for t in tools_result.tools]
        
        logger.info("MCP initialized: %d tools", len(tool_names))
        return tool_names
    
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        """Call an MCP tool with rate limiting.
        
        Parameters
        ----------
        name : str
            Tool name (e.g. "lean_loogle", "lean_leansearch").
        arguments : dict
            Tool arguments (e.g. {"query": "add_comm"}).
        
        Returns
        -------
        McpToolResult
        """
        if self._session is None:
            return McpToolResult(
                success=False, content="MCP not initialized", tool_name=name, is_error=True
            )
        
        # Rate limit check
        self._wait_for_rate_limit(name)
        
        t0 = time.perf_counter()
        try:
            result = await self._session.call_tool(name, arguments)
            elapsed = int((time.perf_counter() - t0) * 1000)
            
            # Record call time for rate limiting
            self._record_call(name)
            
            content = ""
            if result.content:
                content = "\n".join(
                    item.text for item in result.content if hasattr(item, 'text') and item.text
                )
            
            return McpToolResult(
                success=not result.isError,
                content=content,
                tool_name=name,
                elapsed_ms=elapsed,
                is_error=result.isError or False,
            )
        except Exception as e:
            elapsed = int((time.perf_counter() - t0) * 1000)
            logger.warning("MCP tool %s error: %s", name, e)
            return McpToolResult(
                success=False, content=str(e), tool_name=name, elapsed_ms=elapsed, is_error=True
            )
    
    def _record_call(self, tool_name: str):
        """Record a tool call timestamp for rate limiting."""
        if tool_name not in self._rate_history:
            self._rate_history[tool_name] = []
        self._rate_history[tool_name].append(time.perf_counter())
        # Prune old entries
        limit, window = _TOOL_RATE_LIMITS.get(tool_name, (10, 10.0))
        cutoff = time.perf_counter() - window
        self._rate_history[tool_name] = [
            ts for ts in self._rate_history[tool_name] if ts > cutoff
        ]
    
    def _wait_for_rate_limit(self, tool_name: str):
        """Block until rate limit allows a call."""
        limit, window = _TOOL_RATE_LIMITS.get(tool_name, (10, 1.0))
        history = self._rate_history.get(tool_name, [])
        cutoff = time.perf_counter() - window
        recent = [ts for ts in history if ts > cutoff]
        
        if len(recent) >= limit:
            sleep_time = recent[0] + window - time.perf_counter()
            if sleep_time > 0:
                logger.debug("Rate limit for %s: sleeping %.1fs", tool_name, sleep_time)
                time.sleep(sleep_time)
    
    async def close(self):
        """Close MCP connection."""
        try:
            if self._session_ctx and self._session:
                await self._session_ctx.__aexit__(None, None, None)
            if self._client_ctx:
                await self._client_ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.warning("MCP close error: %s", e)
        self._session = None
        self._read = None
        self._write = None
    
    def __del__(self):
        if self._session is not None:
            try:
                asyncio.run(self.close())
            except RuntimeError:
                pass  # Event loop already closed


# ── Synchronous wrapper for easier integration ──────────────

class SyncMcpClient:
    """Synchronous wrapper around McpClient for use in Inner Loop.
    
    Usage:
        client = SyncMcpClient()
        client.initialize()
        result = client.call_tool("lean_loogle", {"query": "add_comm"})
    """
    
    def __init__(self, project_path: str = os.path.expanduser("~/lean-paper-plane")):
        self._client = McpClient(project_path=project_path)
    
    def initialize(self) -> list[str]:
        """Initialize MCP connection. Returns tool names."""
        return asyncio.run(self._client.initialize())
    
    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        """Call an MCP tool synchronously."""
        return asyncio.run(self._client.call_tool(name, arguments))
    
    def close(self):
        """Close MCP connection."""
        try:
            asyncio.run(self._client.close())
        except Exception:
            pass

"""Synchronous MCP client — persistent connection via dedicated thread."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from omega.loop.mcp_client import McpClient, McpToolResult

logger = logging.getLogger("omega.loop.mcp_sync")


class PersistentMcpClient:
    """Synchronous MCP client with persistent connection in a background thread.
    
    Keeps the MCP server connection alive across multiple tool calls.
    The async event loop runs in a dedicated daemon thread.
    
    Usage:
        client = PersistentMcpClient()
        client.initialize()
        result = client.call_tool("lean_loogle", {"query": "add_comm"})
        client.close()
    """
    
    def __init__(self, project_path: str = "/home/shenli/lean-paper-plane"):
        self._project_path = project_path
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mcp: McpClient | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
    
    def initialize(self, timeout: float = 30.0) -> list[str]:
        """Initialize MCP in background thread."""
        self._closed = False
        self._ready = threading.Event()
        
        # Store results
        result_holder: list[list[str] | None] = [None]
        error_holder: list[Exception | None] = [None]
        
        def _run():
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                
                self._mcp = McpClient(project_path=self._project_path)
                tools = self._loop.run_until_complete(self._mcp.initialize())
                result_holder[0] = tools
                self._ready.set()
                
                # Keep the loop alive
                self._loop.run_forever()
            except Exception as e:
                error_holder[0] = e
                self._ready.set()
            finally:
                if self._loop:
                    self._loop.close()
        
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        
        self._ready.wait(timeout=timeout)
        
        if error_holder[0]:
            raise error_holder[0]
        
        return result_holder[0] or []
    
    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        """Call an MCP tool via the persistent connection."""
        if self._mcp is None or self._loop is None:
            return McpToolResult(
                success=False, content="MCP not initialized", tool_name=name, is_error=True
            )
        
        future = asyncio.run_coroutine_threadsafe(
            self._mcp.call_tool(name, arguments), self._loop
        )
        try:
            return future.result(timeout=60)
        except Exception as e:
            logger.warning("MCP call timeout/error: %s", e)
            return McpToolResult(
                success=False, content=str(e), tool_name=name, elapsed_ms=0, is_error=True
            )
    
    def close(self):
        """Close MCP connection."""
        self._closed = True
        if self._loop and self._mcp:
            future = asyncio.run_coroutine_threadsafe(self._mcp.close(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

"""
arc_agent_pay.mcp_server — Model Context Protocol server (optional).

Exposes discovery + pay-and-fetch as MCP tools so any MCP client can use
arc-agent-pay. Requires the `[mcp]` extra. The core SDK does not import `mcp`.
"""

from .session import MCPSession

__all__ = ["MCPSession", "build_server", "main"]


def __getattr__(name):
    # Lazily expose the FastMCP-backed entry points without importing `mcp`
    # at package import time.
    if name in ("build_server", "main"):
        from .server import build_server, main

        return {"build_server": build_server, "main": main}[name]
    raise AttributeError(name)

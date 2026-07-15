"""
mcp_server/server.py — expose arc-agent-pay as a Model Context Protocol server.

Lets Claude Desktop / Cursor / any MCP client discover and pay for x402 services
through arc-agent-pay. The wallet key and budget ceiling come from the
environment only (never from tool arguments), and BudgetGuard enforces the cap.

Run:
    pip install "arc-agent-pay[mcp]"
    export AGENT_PRIVATE_KEY=...                 # funded Arc Testnet EOA
    export ARC_AGENT_PAY_BUDGET=0.50             # optional session ceiling
    arc-agent-pay-mcp                            # stdio transport

Claude Desktop config (claude_desktop_config.json):
    {
      "mcpServers": {
        "arc-agent-pay": {
          "command": "arc-agent-pay-mcp",
          "env": { "AGENT_PRIVATE_KEY": "0x...", "ARC_AGENT_PAY_BUDGET": "0.50" }
        }
      }
    }
"""

from __future__ import annotations

import os

from .session import MCPSession


def build_server(session: MCPSession | None = None):
    """Build the FastMCP server, registering tools over an MCPSession."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover - import guard
        raise ImportError(
            'The MCP server requires the mcp package.\n'
            'Install with: pip install "arc-agent-pay[mcp]"'
        ) from e

    session = session or MCPSession(
        budget_usdc=os.environ.get("ARC_AGENT_PAY_BUDGET", "0.50")
    )
    mcp = FastMCP("arc-agent-pay")

    @mcp.tool()
    def discover_services(query: str, max_results: int = 5) -> list[dict]:
        """Find paid x402 API services relevant to a query."""
        return session.discover(query, max_results)

    @mcp.tool()
    def list_registered_services() -> list[dict]:
        """List all services known to the registry."""
        return session.list_services()

    @mcp.tool()
    async def pay_and_fetch(
        service_name: str, method: str = "GET", params: dict | None = None
    ) -> dict:
        """Pay for and fetch data from a service. Spend is capped by the session budget."""
        return await session.pay_and_fetch(service_name, method, params)

    @mcp.tool()
    def get_budget_status() -> dict:
        """Return the session budget: total, spent, and remaining USDC."""
        return session.budget_status()

    @mcp.tool()
    def get_agent_identity() -> dict | None:
        """Return this wallet's ERC-8004 agent identity + reputation, if any."""
        return session.agent_identity()

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()

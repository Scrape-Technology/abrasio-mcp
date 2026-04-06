"""
abrasio-mcp — MCP server that exposes Abrasio as an agentic browser.

Transport: stdio (default) — works with Claude Desktop, Claude Code, Cursor, etc.
Transport: streamable-http — for production/cloud deployments.

Environment variables:
  ABRASIO_API_KEY    — Abrasio cloud API key (enables cloud mode; omit for local)
  ABRASIO_HEADLESS   — "true" (default) or "false"
  ABRASIO_REGION     — target region, e.g. "BR", "US"
  ABRASIO_HUMANIZE   — "true" to enable human-like interaction speed (default: false)
  ABRASIO_TRANSPORT  — "stdio" (default) or "streamable-http"
  ABRASIO_HOST       — host for streamable-http (default: 127.0.0.1)
  ABRASIO_PORT       — port for streamable-http (default: 8931)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .browser import AbrasioBrowserAgent
from .tools import navigate, observe, interact, evaluate

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("abrasio_mcp")

# ---------------------------------------------------------------------------
# Global browser agent — one per server process, lazy-started on first tool call
# ---------------------------------------------------------------------------

_agent: Optional[AbrasioBrowserAgent] = None


async def _get_agent() -> AbrasioBrowserAgent:
    """Return the shared browser agent, starting it on first call."""
    global _agent
    if _agent is None:
        _agent = AbrasioBrowserAgent(
            api_key=os.getenv("ABRASIO_API_KEY"),
            headless=os.getenv("ABRASIO_HEADLESS", "true").lower() != "false",
            region=os.getenv("ABRASIO_REGION"),
            humanize=os.getenv("ABRASIO_HUMANIZE", "false").lower() == "true",
        )
    await _agent.ensure_started()
    return _agent


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "abrasio-browser",
    instructions=(
        "You control a real web browser via the Abrasio stealth browser. "
        "The browser runs with anti-detection fingerprinting so it behaves like a real user. "
        "Use browser_screenshot to see the current page before deciding what to do. "
        "Use browser_find_elements to discover clickable/fillable elements and their selectors. "
        "Prefer browser_fill over browser_type when targeting a specific input field. "
        "Use browser_evaluate for custom data extraction that text/HTML tools can't handle. "
        "The browser session persists across tool calls — you don't need to re-navigate."
    ),
)

# Register all tool modules
navigate.register(mcp, _get_agent)
observe.register(mcp, _get_agent)
interact.register(mcp, _get_agent)
evaluate.register(mcp, _get_agent)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

async def _shutdown():
    global _agent
    if _agent:
        logger.info("Closing browser…")
        await _agent.close()
        _agent = None


def _on_signal(signum, frame):
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(_shutdown())


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    transport = os.getenv("ABRASIO_TRANSPORT", "stdio")

    if transport == "streamable-http":
        host = os.getenv("ABRASIO_HOST", "127.0.0.1")
        port = int(os.getenv("ABRASIO_PORT", 8931))
        logger.info(f"Starting abrasio-mcp HTTP server on {host}:{port}")
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        logger.info("Starting abrasio-mcp stdio server")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

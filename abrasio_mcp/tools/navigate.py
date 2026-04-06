"""Navigation tools — move the browser between pages."""

from __future__ import annotations
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..browser import AbrasioBrowserAgent


def register(mcp, get_agent):
    """Register navigation tools on the FastMCP instance."""

    @mcp.tool()
    async def browser_navigate(url: str) -> str:
        """
        Navigate to a URL and wait for the page to load.
        Returns the final URL, page title, and HTTP status code.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        result = await agent.navigate(url)
        return json.dumps(result)

    @mcp.tool()
    async def browser_go_back() -> str:
        """Go back to the previous page in browser history."""
        agent: AbrasioBrowserAgent = await get_agent()
        result = await agent.go_back()
        return json.dumps(result)

    @mcp.tool()
    async def browser_go_forward() -> str:
        """Go forward to the next page in browser history."""
        agent: AbrasioBrowserAgent = await get_agent()
        result = await agent.go_forward()
        return json.dumps(result)

    @mcp.tool()
    async def browser_reload() -> str:
        """Reload the current page."""
        agent: AbrasioBrowserAgent = await get_agent()
        result = await agent.reload()
        return json.dumps(result)

    @mcp.tool()
    async def browser_get_url() -> str:
        """Return the current page URL."""
        agent: AbrasioBrowserAgent = await get_agent()
        return await agent.get_url()

"""Observation tools — see what's on the page."""

from __future__ import annotations
import json
from typing import TYPE_CHECKING, Optional
from mcp.server.fastmcp import Image

if TYPE_CHECKING:
    from ..browser import AbrasioBrowserAgent


def register(mcp, get_agent):
    """Register observation tools on the FastMCP instance."""

    @mcp.tool()
    async def browser_screenshot() -> Image:
        """
        Take a screenshot of the current page.
        Returns a PNG image. Use this to visually understand the current state
        of the browser before deciding what to interact with.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        data = await agent.screenshot()
        return Image(data=data, format="png")

    @mcp.tool()
    async def browser_get_text() -> str:
        """
        Extract all visible text from the current page body.
        Useful for reading content, prices, articles, or any text-based information.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        return await agent.get_text()

    @mcp.tool()
    async def browser_get_html(selector: str = "body") -> str:
        """
        Get the inner HTML of an element. Defaults to the full page body.
        Use a CSS selector to target a specific element (e.g. '#main', '.content').
        """
        agent: AbrasioBrowserAgent = await get_agent()
        return await agent.get_html(selector)

    @mcp.tool()
    async def browser_find_elements() -> str:
        """
        Find all interactive elements on the page (links, buttons, inputs, etc.).
        Returns a JSON list with each element's tag, text, type, href, selector hint,
        center coordinates (x, y), and whether it's visible in the viewport.
        Use this to discover what you can click or fill before interacting.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        elements = await agent.find_elements()
        return json.dumps(elements, ensure_ascii=False)

    @mcp.tool()
    async def browser_wait_for(selector: str, timeout: int = 10000) -> str:
        """
        Wait for an element matching the CSS selector to appear in the DOM.
        Useful after triggering an action that causes a loading state.
        timeout: max milliseconds to wait (default 10000).
        Returns the element's text content when found.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        result = await agent.wait_for(selector, timeout=timeout)
        return json.dumps(result)

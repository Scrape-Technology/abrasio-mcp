"""Interaction tools — click, type, scroll, hover with human-like behaviour."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..browser import AbrasioBrowserAgent


def register(mcp, get_agent):
    """Register interaction tools on the FastMCP instance."""

    @mcp.tool()
    async def browser_click(selector: str) -> str:
        """
        Click an element using a CSS selector.
        Uses human-like WindMouse movement and realistic click timing.
        Examples: 'button#submit', '.login-btn', 'a[href="/checkout"]'
        """
        agent: AbrasioBrowserAgent = await get_agent()
        await agent.click(selector)
        return f"Clicked: {selector}"

    @mcp.tool()
    async def browser_fill(selector: str, text: str) -> str:
        """
        Click a form field and fill it with text using human-like typing.
        Clears any existing value, then types with realistic delays and occasional typos.
        selector: CSS selector for the input element.
        text: the value to type.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        await agent.fill(selector, text)
        return f"Filled '{selector}' with text ({len(text)} chars)"

    @mcp.tool()
    async def browser_type(text: str) -> str:
        """
        Type text at the current cursor position (no element targeting).
        Use this after clicking an input to type into it, or to send keyboard input
        to the focused element. Uses human-like timing and occasional typos.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        await agent.type_text(text)
        return f"Typed {len(text)} characters"

    @mcp.tool()
    async def browser_scroll(pixels: int = 800) -> str:
        """
        Scroll the page vertically with human-like easing.
        pixels: positive = scroll down, negative = scroll up (default: 800).
        """
        agent: AbrasioBrowserAgent = await get_agent()
        await agent.scroll(pixels)
        direction = "down" if pixels >= 0 else "up"
        return f"Scrolled {direction} {abs(pixels)}px"

    @mcp.tool()
    async def browser_hover(selector: str) -> str:
        """
        Move the mouse over an element using human-like WindMouse movement.
        Useful for triggering dropdown menus or tooltips before clicking.
        selector: CSS selector for the target element.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        await agent.hover(selector)
        return f"Hovered: {selector}"

    @mcp.tool()
    async def browser_press(key: str) -> str:
        """
        Press a keyboard key or key combination.
        Examples: 'Enter', 'Tab', 'Escape', 'ArrowDown', 'Control+a', 'Meta+Return'.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        await agent.press_key(key)
        return f"Pressed: {key}"

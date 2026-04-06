"""Evaluate tool — run arbitrary JavaScript in the page context."""

from __future__ import annotations
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..browser import AbrasioBrowserAgent


def register(mcp, get_agent):
    """Register the JS evaluation tool on the FastMCP instance."""

    @mcp.tool()
    async def browser_evaluate(script: str) -> str:
        """
        Execute JavaScript in the current page context and return the result.
        The script should be a JS expression or a function body that returns a value.
        Examples:
          - 'document.title'
          - 'window.location.href'
          - '() => document.querySelectorAll("h2").length'
          - '() => { const el = document.querySelector("#price"); return el ? el.innerText : null; }'
        Returns the serialized result as JSON.
        """
        agent: AbrasioBrowserAgent = await get_agent()
        result = await agent.evaluate(script)
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)

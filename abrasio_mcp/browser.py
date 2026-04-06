"""
AbrasioBrowserAgent — holds a single Abrasio session and page for the MCP server.

Wraps the Abrasio SDK (cloud or local mode) and exposes browser primitives
that the MCP tools delegate to. Lazy-started on first tool call.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from abrasio import Abrasio, AbrasioConfig
from abrasio.human.actions import (
    click_human,
    fill_human,
    type_human,
    scroll_human,
    human_mouse_move,
    _resolve_box_async,
)

logger = logging.getLogger("abrasio_mcp.browser")

# JS that extracts interactive elements with position and a unique selector hint
_FIND_ELEMENTS_JS = """
() => {
    const TAGS = 'a,button,input,select,textarea,[role="button"],[role="link"],[role="menuitem"],[role="tab"],[role="checkbox"],[role="radio"],[onclick]';
    const seen = new Set();
    const items = [];
    document.querySelectorAll(TAGS).forEach(el => {
        const box = el.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) return;
        if (seen.has(el)) return;
        seen.add(el);

        // Build a reasonably unique CSS selector hint
        let hint = el.tagName.toLowerCase();
        if (el.id) hint = '#' + el.id;
        else if (el.name) hint = `${hint}[name="${el.name}"]`;
        else if (el.getAttribute('data-testid')) hint = `[data-testid="${el.getAttribute('data-testid')}"]`;
        else if (el.className) {
            const cls = el.className.toString().trim().split(/\\s+/).slice(0, 2).join('.');
            if (cls) hint += '.' + cls;
        }

        items.push({
            tag:     el.tagName.toLowerCase(),
            type:    el.type || null,
            text:    (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 120),
            href:    el.href || null,
            selector: hint,
            x:       Math.round(box.x + box.width / 2),
            y:       Math.round(box.y + box.height / 2),
            visible: box.top >= 0 && box.bottom <= window.innerHeight,
        });
    });
    return items;
}
"""


class AbrasioBrowserAgent:
    """
    Single browser session used by all MCP tool calls.

    Supports both Abrasio cloud mode (api_key set) and local stealth mode (no key).
    Lazily started on first tool call so the MCP server starts instantly.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        headless: bool = True,
        region: Optional[str] = None,
        humanize: bool = False,
        humanize_speed: float = 1.0,
    ):
        self._config = AbrasioConfig(
            api_key=api_key or os.getenv("ABRASIO_API_KEY"),
            headless=headless,
            region=region,
            humanize=humanize,
            humanize_speed=humanize_speed,
        )
        self._abrasio: Optional[Abrasio] = None
        self._page = None
        self._lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        """Lazy-start: called before every tool invocation."""
        async with self._lock:
            if self._page is not None:
                return
            logger.info("Starting Abrasio browser…")
            self._abrasio = Abrasio(self._config)
            await self._abrasio.start()
            self._page = await self._abrasio.new_page()
            mode = "cloud" if self._config.is_cloud_mode else "local"
            logger.info(f"Browser ready ({mode} mode)")

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("Browser not started — call ensure_started() first")
        return self._page

    async def close(self) -> None:
        if self._abrasio:
            await self._abrasio.close()
            self._abrasio = None
            self._page = None

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> dict:
        response = await self._page.goto(url, wait_until="domcontentloaded")
        return {
            "url": self._page.url,
            "title": await self._page.title(),
            "status": response.status if response else None,
        }

    async def go_back(self) -> dict:
        await self._page.go_back(wait_until="domcontentloaded")
        return {"url": self._page.url, "title": await self._page.title()}

    async def go_forward(self) -> dict:
        await self._page.go_forward(wait_until="domcontentloaded")
        return {"url": self._page.url, "title": await self._page.title()}

    async def reload(self) -> dict:
        await self._page.reload(wait_until="domcontentloaded")
        return {"url": self._page.url, "title": await self._page.title()}

    async def get_url(self) -> str:
        return self._page.url

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(type="png")

    async def get_text(self) -> str:
        return await self._page.inner_text("body")

    async def get_html(self, selector: str = "body") -> str:
        return await self._page.inner_html(selector)

    async def find_elements(self) -> list:
        return await self._page.evaluate(_FIND_ELEMENTS_JS)

    async def wait_for(self, selector: str, timeout: int = 10000) -> dict:
        await self._page.wait_for_selector(selector, timeout=timeout)
        el = await self._page.query_selector(selector)
        text = await el.inner_text() if el else ""
        return {"found": True, "selector": selector, "text": text.strip()[:200]}

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    async def click(self, selector: str) -> None:
        await click_human(self._page, selector)

    async def fill(self, selector: str, text: str) -> None:
        await fill_human(self._page, selector, text)

    async def type_text(self, text: str) -> None:
        await type_human(self._page, text)

    async def scroll(self, pixels: int = 800) -> None:
        direction = 1 if pixels >= 0 else -1
        await scroll_human(self._page, pixels=abs(pixels) * direction)

    async def hover(self, selector: str) -> None:
        x, y = await _resolve_box_async(self._page, selector)
        await human_mouse_move(self._page, (x, y))

    async def press_key(self, key: str) -> None:
        await self._page.keyboard.press(key)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    async def evaluate(self, script: str):
        return await self._page.evaluate(script)

"""
AbrasioBrowserAgent — holds a single Abrasio session and page for the MCP server.

Wraps the Abrasio SDK (cloud or local mode) and exposes browser primitives
that the MCP tools delegate to. Lazy-started on first tool call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from typing import Optional
from urllib.parse import unquote, urlsplit

from abrasio import Abrasio, AbrasioConfig
from abrasio.human.actions import (
    click_human,
    type_human,
    scroll_human,
    human_mouse_move,
    _resolve_box_async,
)

# Playbook Engine recording (spec 2026-07-10, C7): instrument the agent methods every MCP
# tool funnels through. `session.record*` is a no-op unless ABRASIO_RECORD=true, so default
# behaviour is byte-for-byte unchanged.
from .recording import session as _recording

logger = logging.getLogger("abrasio_mcp.browser")

# Local mode only: "true" lets browser_navigate reach loopback/private hosts (your own dev
# server). Link-local and cloud metadata addresses stay blocked regardless.
ALLOW_PRIVATE_HOSTS_ENV = "ABRASIO_ALLOW_PRIVATE_HOSTS"
_METADATA_IPS = {ipaddress.ip_address("fd00:ec2::254")}  # AWS IMDS over IPv6 (ULA, not link-local)
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")


def parse_host(host: str) -> tuple[str, Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    """Normalise a URL host the way Chromium does (WHATWG URL host parser) and return
    (host, address). address is None for a DNS name; localhost and *.localhost map to
    127.0.0.1, like the browser. A host whose last label is numeric is an IPv4 literal
    in decimal, 0x-hex or leading-zero octal parts (1-4 parts, the last one filling the
    remaining bytes): "127.1", "0x7f.1", "017700000001", "2130706433". Raises ValueError
    for hosts the browser would reject too (bad IDNA, malformed numeric host)."""
    host = unquote(host)
    if ":" in host:  # IPv6 literal (urlsplit already removed the brackets)
        return host, ipaddress.ip_address(host.split("%")[0])
    try:
        host = (host if host.isascii() else host.encode("idna").decode("ascii")).lower()
    except UnicodeError:
        raise ValueError(f"Refusing to navigate: invalid host {host!r}.")
    host = host.rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return host, ipaddress.ip_address("127.0.0.1")
    labels = host.split(".")
    if not _NUMERIC_LABEL.fullmatch(labels[-1]):
        return host, None
    nums = []
    for label in labels:
        if re.fullmatch(r"0[xX][0-9a-fA-F]*", label):
            nums.append(int(label[2:] or "0", 16))
        elif re.fullmatch(r"0[0-7]+", label):
            nums.append(int(label, 8))
        elif re.fullmatch(r"0|[1-9][0-9]*", label):
            nums.append(int(label))
        else:
            raise ValueError(f"Refusing to navigate: malformed numeric host {host!r}.")
    if len(nums) > 4 or any(n > 255 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
        raise ValueError(f"Refusing to navigate: malformed numeric host {host!r}.")
    value = nums[-1] + sum(n << 8 * (3 - i) for i, n in enumerate(nums[:-1]))
    return host, ipaddress.IPv4Address(value)


async def check_navigation_target(url: str, cloud: bool) -> None:
    """Raise ValueError unless `url` is an http(s) URL whose host is safe to open.

    Local mode (the browser runs on this machine): the host must not be, or resolve to,
    a loopback/private/link-local/unspecified address, unless ABRASIO_ALLOW_PRIVATE_HOSTS
    is "true" (link-local and metadata stay blocked). Cloud mode: only IP literals and
    *.localhost are checked; names resolve on the cloud worker, not here.

    ponytail: DNS rebinding between this check and the browser's own connection is not
    covered at this layer; that needs enforcement where the connection is made.
    """
    if "\\" in url or any(ord(c) <= 0x20 for c in url):
        raise ValueError("Refusing to navigate: URL contains backslashes, spaces or control characters.")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("Refusing to navigate: only http:// and https:// URLs are allowed.")
    allow_private = not cloud and os.getenv(ALLOW_PRIVATE_HOSTS_ENV, "").lower() == "true"
    host, literal = parse_host(parts.hostname)
    if literal is not None:
        ips = [literal]
    elif cloud:
        return  # a hostname, resolved by the cloud worker
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)  # off the event loop
        except socket.gaierror:
            raise ValueError(f"Refusing to navigate: cannot resolve host {host!r}.")
        ips = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    for ip in ips:
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_link_local or ip in _METADATA_IPS:
            raise ValueError(f"Refusing to navigate to link-local/metadata address {ip}.")
        if not allow_private and not ip.is_global:
            hint = "" if cloud else f" Set {ALLOW_PRIVATE_HOSTS_ENV}=true to allow your own dev server."
            raise ValueError(f"Refusing to navigate to non-public address {ip}.{hint}")


MAX_OUTPUT_CHARS = 200_000  # get_text/get_html cap, keeps one page from flooding the model


def _cap_output(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[truncated: {len(text) - MAX_OUTPUT_CHARS} more characters]"


def _log_url(url: str) -> str:
    """A URL for the logs: no userinfo, query string or fragment (tokens live there)."""
    try:
        p = urlsplit(url)
        return p._replace(netloc=p.netloc.rpartition("@")[2], query="", fragment="").geturl()
    except ValueError:
        return "<unparseable url>"


async def check_current_url(url: str, cloud: bool) -> None:
    """check_navigation_target for a URL a page is already on. Lets through only content
    that involves no network fetch: about:blank, about:srcdoc and data: (top-level data:
    navigations are blocked by Chromium anyway); a blob: URL is checked by its origin."""
    if url in ("about:blank", "about:srcdoc") or url.startswith("data:"):
        return
    await check_navigation_target(url[len("blob:"):] if url.startswith("blob:") else url, cloud)


async def _guard_request(route, request) -> None:
    """context.route handler (local mode). Navigation requests (main frame, subframes,
    popups) get the full check, DNS included. Other requests are checked only when the
    host is an address or a localhost name, i.e. parse_host needs no DNS lookup; anything
    else falls back untouched to the next handler, so the recording `response` listener
    sees what it saw before.

    ponytail: still open — subresource hostnames that resolve to private addresses (no
    per-request DNS on purpose), WebSocket and service-worker traffic (not seen by
    page/context routes), and DNS rebinding.
    """
    if not request.is_navigation_request():
        try:
            if parse_host(urlsplit(request.url).hostname or "")[1] is None:
                await route.fallback()  # a hostname: no per-request DNS lookup
                return
        except ValueError:
            pass  # malformed numeric host: the check below refuses it
    try:
        await check_navigation_target(request.url, cloud=False)
    except ValueError as exc:
        logger.warning("Blocked request to %s: %s", _log_url(request.url), exc)
        await route.abort("blockedbyclient")
        return
    await route.fallback()

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

    _refusals = 0  # frames the framenavigated listener refused (see _read)

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
            # Local mode: guard every request of the context (this page and any popup the
            # site opens). Not in cloud mode: the worker resolves and fetches, this host
            # cannot see the real destination. Routing turns off the browser HTTP cache.
            if not self._config.is_cloud_mode:
                await self._page.context.route("**/*", _guard_request)
            # Playwright auto-continues 3xx hops without calling routes: re-check where the
            # main frame lands, and the read methods re-check before returning content.
            self._page.on("framenavigated", self._leave_if_unsafe)
            # Attach the recording network-capture listener (no-op unless ABRASIO_RECORD=true).
            _recording.attach(self._page)
            mode = "cloud" if self._config.is_cloud_mode else "local"
            logger.info(f"Browser ready ({mode} mode)")

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("Browser not started — call ensure_started() first")
        return self._page

    async def _leave_if_unsafe(self, frame) -> None:
        """framenavigated listener: a frame (main or sub) that landed on a refused URL
        (e.g. via a redirect after a click, or an iframe redirected to loopback) is sent
        to about:blank; a frame already detached is left alone."""
        try:
            await check_current_url(frame.url, cloud=self._config.is_cloud_mode)
        except ValueError as exc:
            self._refusals += 1  # before neutralising: lets an in-flight _read notice
            logger.warning("Leaving %s: %s", _log_url(frame.url), exc)
            try:
                await frame.goto("about:blank")
            except Exception:
                logger.debug("could not leave %s", _log_url(frame.url), exc_info=True)

    async def _check_frames(self) -> None:
        """check_current_url over every frame of the page, one check per (scheme, host)."""
        seen = set()
        for frame in self._page.frames:
            url = frame.url
            parts = urlsplit(url[len("blob:"):] if url.startswith("blob:") else url)
            key = (parts.scheme, parts.hostname) if parts.hostname else url
            if key in seen:
                continue
            seen.add(key)
            await check_current_url(url, cloud=self._config.is_cloud_mode)

    async def _read(self, fn):
        """Run a page read only while every frame is on an allowed URL, checked before
        and after, so content from a refused frame never returns (the listener is async;
        screenshots render iframes). A listener refusal during the read discards the
        result, since the frame may already be back on about:blank by the second check."""
        refusals = self._refusals
        await self._check_frames()
        result = await fn()
        if self._refusals != refusals:
            raise ValueError("Refusing to return content: a frame navigated to a refused address during the read.")
        await self._check_frames()
        return result

    async def close(self) -> None:
        if self._abrasio:
            await self._abrasio.close()
            self._abrasio = None
            self._page = None

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> dict:
        await check_navigation_target(url, cloud=self._config.is_cloud_mode)
        response = await self._page.goto(url, wait_until="domcontentloaded")
        try:  # 3xx hops never reach the route: re-check where the redirects ended up
            await check_navigation_target(self._page.url, cloud=self._config.is_cloud_mode)
        except ValueError:
            await self._page.goto("about:blank")
            raise
        _recording.record("navigate", url=url)
        return {
            "url": self._page.url,
            "title": await self._page.title(),
            "status": response.status if response else None,
        }

    async def go_back(self) -> dict:
        await self._page.go_back(wait_until="domcontentloaded")
        _recording.record("navigate", url=self._page.url)
        return {"url": self._page.url, "title": await self._page.title()}

    async def go_forward(self) -> dict:
        await self._page.go_forward(wait_until="domcontentloaded")
        _recording.record("navigate", url=self._page.url)
        return {"url": self._page.url, "title": await self._page.title()}

    async def reload(self) -> dict:
        await self._page.reload(wait_until="domcontentloaded")
        _recording.record("navigate", url=self._page.url)
        return {"url": self._page.url, "title": await self._page.title()}

    async def get_url(self) -> str:
        return self._page.url

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def screenshot(self) -> bytes:
        return await self._read(lambda: self._page.screenshot(type="png"))

    async def get_text(self) -> str:
        return _cap_output(await self._read(lambda: self._page.inner_text("body")))

    async def get_html(self, selector: str = "body") -> str:
        return _cap_output(await self._read(lambda: self._page.inner_html(selector)))

    async def find_elements(self) -> list:
        return await self._read(lambda: self._page.evaluate(_FIND_ELEMENTS_JS))

    async def wait_for(self, selector: str, timeout: int = 10000) -> dict:
        timeout = max(1, min(int(timeout), 60000))  # Playwright treats 0 as "no timeout"
        await self._page.wait_for_selector(selector, timeout=timeout)
        _recording.record("wait_for", selector=selector, timeout=timeout)
        el = await self._page.query_selector(selector)
        text = await self._read(el.inner_text) if el else ""
        return {"found": True, "selector": selector, "text": text.strip()[:200]}

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    async def click(self, selector: str) -> None:
        await click_human(self._page, selector)
        _recording.record("click", selector=selector)

    async def fill(self, selector: str, text: str) -> None:
        # fill_human split open: the C5 guard runs after the click that focuses and
        # before any keystroke, against the element that will actually receive the keys.
        await click_human(self._page, selector)
        step = await _recording.fill_step(self._page, selector, text)
        await asyncio.sleep(0.15)
        mac = "mac" in (getattr(self._page, "_abr_platform", "") or "").lower()
        await self._page.keyboard.press(f"{'Meta' if mac else 'Control'}+a")
        await type_human(self._page, text)
        _recording.add(step)

    async def type_text(self, text: str) -> None:
        # Same secret_ref / credential-field guard as fill, before typing.
        step = await _recording.fill_step(self._page, None, text)
        await type_human(self._page, text)
        _recording.add(step)

    async def scroll(self, pixels: int = 800) -> None:
        direction = 1 if pixels >= 0 else -1
        await scroll_human(self._page, pixels=abs(pixels) * direction)
        _recording.record("scroll", pixels=pixels)

    async def hover(self, selector: str) -> None:
        x, y = await _resolve_box_async(self._page, selector)
        await human_mouse_move(self._page, (x, y))
        _recording.record("hover", selector=selector)

    async def press_key(self, key: str) -> None:
        await self._page.keyboard.press(key)
        _recording.record("press", key=key)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    async def evaluate(self, script: str):
        result = await self._read(lambda: self._page.evaluate(script))
        _recording.record("evaluate", script=script)
        return result

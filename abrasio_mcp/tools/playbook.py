"""Playbook tools — compile the current recording into a deterministic trajectory.

Registered only when the server runs with ABRASIO_RECORD=true: the agent explores a site
once, marks the data it wants, optionally captures the internal API call behind the page
(T0), and finalizes a playbook the deterministic runtime replays with no LLM
(spec 2026-07-10, Component 1). `playbook_finalize` uploads the playbook to the
ScrapeTech API itself (SCRAPETECH_API_URL / SCRAPETECH_API_KEY), so secret values never
pass through the model and never touch the disk.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlsplit

import httpx

from ..browser import _cap_output
from ..recording import session

if TYPE_CHECKING:
    from ..browser import AbrasioBrowserAgent  # noqa: F401

API_URL_ENV = "SCRAPETECH_API_URL"
API_KEY_ENV = "SCRAPETECH_API_KEY"
DEFAULT_API_URL = "https://api.scrapetechnology.com"


def _api_endpoint() -> tuple[str, str]:
    """(POST url, api key) from the env. https only, except a loopback dev API."""
    key = os.getenv(API_KEY_ENV, "").strip()
    if not key:
        raise ValueError(f"{API_KEY_ENV} is not set: playbook_finalize uploads the playbook itself "
                         f"and needs a ScrapeTech API key in the MCP server's environment.")
    base = os.getenv(API_URL_ENV, "").strip() or DEFAULT_API_URL
    p = urlsplit(base)
    if not p.hostname or not (p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"))):
        raise ValueError(f"{API_URL_ENV} must be an https:// URL (http:// only for localhost/127.0.0.1).")
    return base.rstrip("/") + "/api/playbooks", key


def register(mcp, get_agent):
    """Register playbook + network-capture tools on the FastMCP instance."""

    @mcp.tool()
    async def browser_list_requests() -> str:
        """
        List the XHR/fetch calls captured while recording (method, url, status,
        request headers, content-type). Use this to find the internal JSON/API feed behind
        a page — the data endpoint you can replay directly over HTTP (T0), with no
        browser. Header values other than benign ones (accept*, content-type, origin,
        referer, user-agent, sec-*, …) are shown as "[redacted]".
        """
        return _cap_output(json.dumps(session.list_requests(), ensure_ascii=False))

    @mcp.tool()
    async def playbook_register_secret(ref: str, value: str) -> str:
        """
        Register a credential BEFORE filling it, so the playbook stores a reference
        (secret_ref) instead of the raw value. Call this, then browser_fill with the same
        value — the recorded step will carry `secret_ref: <ref>`, never the plaintext.
        Typing an unregistered value into a password / one-time-code / card field (or an
        iframe or unknown target) is a hard error. Refs starting with "header:" are reserved.
        Example: playbook_register_secret("vault://example/pw", "hunter2").
        """
        session.register_secret(ref, value)
        return f"Registered secret ref '{ref}' ({len(value)} chars)"

    @mcp.tool()
    async def playbook_mark_extract(
        schema: dict, selector: Optional[str] = None, script: Optional[str] = None
    ) -> str:
        """
        Declare the structured schema you extracted at this point in the trajectory.
        Appends a terminal `extract` step and stashes the schema; on finalize it is
        write-through upserted into `page_schemas` and the playbook stores only a pointer
        (schema_domain/schema_mode). `schema` is a {field: type} dict.

        Pass `selector` — the CSS selector for the element(s) holding the data you want
        (found via browser_find_elements / browser_get_html). Without it the browser
        replay has nothing to extract and every run fails with reason:"response_shape".
        """
        session.mark_extract(schema, selector=selector, script=script)
        return f"Marked extract step with {len(schema)} schema field(s)" + (
            f", selector='{selector}'" if selector else ""
        )

    @mcp.tool()
    async def playbook_capture_request(url: str) -> str:
        """
        Compile the page's internal API call into a T0 (`http`) request step. Pass a url
        that appeared in browser_list_requests(). Emits an `op:"request"` step and switches
        the playbook transport to `http`, so replay is a plain HTTP poll with no browser.
        Every header except benign ones (accept*, content-type, origin, referer,
        user-agent, sec-*, …) gets its own ref, e.g. `secret_ref:header:cookie`, never raw.
        """
        step = session.capture_request(url)
        return json.dumps({"captured": True, "transport": "http", "step": step}, ensure_ascii=False)

    @mcp.tool()
    async def playbook_finalize(name: str, domain: str) -> str:
        """
        Finalize the recorded trajectory and upload it to the ScrapeTech API
        (POST /api/playbooks, which seals the secrets). `domain` must be the host of the
        first page you navigated to, or a parent domain of it. Fails if a registered
        secret value shows up in any step. Returns the stored playbook's id, group_id,
        version, health and step count — secret values never come back. On success the
        recording is cleared, ready for the next one.
        """
        url, key = _api_endpoint()
        body = session.finalize(name, domain)
        secret_values = list(body["secrets"].values()) + [key]
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
                resp = await client.post(url, json=body, headers={"X-API-KEY": key})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Playbook upload failed: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200 or not isinstance(data, dict) or not isinstance(data.get("playbook"), dict):
            # Only a plain-string `detail` is echoed, and never if it carries a secret
            # (a 422 echoes the request body, secrets included).
            detail = data.get("detail") if isinstance(data, dict) else None
            if not isinstance(detail, str) or any(v in detail for v in secret_values):
                detail = ""
            raise RuntimeError(f"Playbook upload failed: HTTP {resp.status_code} {detail[:300]}".rstrip())
        pb = data["playbook"]
        session.reset()
        return json.dumps({
            "uploaded": True,
            "id": pb.get("id"),
            "group_id": pb.get("group_id"),
            "name": pb.get("name"),
            "version": pb.get("version"),
            "health": pb.get("health"),
            "steps": len(body["steps"]),
            "secret_refs": sorted(body["secrets"]),
        }, ensure_ascii=False)

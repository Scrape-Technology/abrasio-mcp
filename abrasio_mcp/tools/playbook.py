"""Playbook tools — compile the current recording into a deterministic trajectory.

These tools are meaningful during a RECORD run (ABRASIO_RECORD=true): the agent explores a
site once, marks the data it wants, optionally captures the internal API call behind the
page (T0), and finalizes a playbook the deterministic runtime replays with no LLM
(spec 2026-07-10, Component 1).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

from ..recording import session

if TYPE_CHECKING:
    from ..browser import AbrasioBrowserAgent  # noqa: F401


def register(mcp, get_agent):
    """Register playbook + network-capture tools on the FastMCP instance."""

    @mcp.tool()
    async def browser_list_requests() -> str:
        """
        List the XHR/fetch calls captured while recording (method, url, status,
        request headers, content-type). Use this to find the internal JSON/API feed behind
        a page — the odds/price endpoint you can replay directly over HTTP (T0), with no
        browser. Only populated when the server runs with ABRASIO_RECORD=true.
        """
        return json.dumps(session.list_requests(), ensure_ascii=False)

    @mcp.tool()
    async def playbook_register_secret(ref: str, value: str) -> str:
        """
        Register a credential BEFORE filling it, so the playbook stores a reference
        (secret_ref) instead of the raw value. Call this, then browser_fill with the same
        value — the recorded step will carry `secret_ref: <ref>`, never the plaintext.
        Filling a password field with an unregistered value is a hard error.
        Example: playbook_register_secret("vault://bet365/pw", "hunter2").
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
        (found via browser_find_elements / browser_get_html). Without it the T1/T2 replay
        runner (playbook-runner.ts) has nothing to extract and every run breaks
        immediately with reason:"response_shape" (bug found in review, 2026-07-15 — this
        tool used to silently drop the selector even though RecordingSession.mark_extract
        always supported one).
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
        Token-looking headers (Authorization, Cookie, …) are stored as
        `secret_ref:session_token`, never raw.
        """
        step = session.capture_request(url)
        return json.dumps({"captured": True, "transport": "http", "step": step}, ensure_ascii=False)

    @mcp.tool()
    async def playbook_finalize(name: str, domain: str) -> str:
        """
        Finalize the recorded trajectory into a playbook JSON:
        {name, domain, transport, steps, schema, profile_domain, secrets}.
        POST this to the api `POST /playbooks`, which write-throughs the schema to
        `page_schemas` and seals `secrets` into `secrets_enc`. Requires ABRASIO_RECORD=true
        to have captured any steps.
        """
        return json.dumps(session.finalize(name, domain), ensure_ascii=False)

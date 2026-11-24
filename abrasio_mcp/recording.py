"""
recording.py — compiles the agent's browser actions into a deterministic PlaybookStep[]
(Playbook Engine, spec 2026-07-10).

The MCP tool vocabulary *is* the playbook vocabulary: every mutating action the agent
takes (navigate/click/fill/type/press/scroll/hover/wait_for/evaluate) already carries the
exact selector/args a replay needs. Rather than hook MCP dispatch (FastMCP exposes no such
hook — spec C7), we instrument the shared `AbrasioBrowserAgent` methods that every tool
funnels through, so recording sees the real selectors/args.

Scope guard: everything here is a no-op unless ``ABRASIO_RECORD=true``. When off, the
`record*` calls return before doing anything and no response listener is attached, so
default MCP behaviour is byte-for-byte unchanged.

Secrets (spec C5): raw credentials NEVER enter ``steps``. The agent calls
``playbook_register_secret(ref, value)`` first; a `fill` whose value is registered records
a ``secret_ref``; a `fill` on a password-type input whose value is NOT registered is a hard
error. ``finalize`` returns registered values under ``secrets`` for the API to seal; the
``playbook_finalize`` tool writes that body to a private file and never returns the values.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger("abrasio_mcp.recording")

# Request-header names whose values are treated as session tokens when a T0 request is
# captured — stored as `secret_ref:header:<name>`, never raw (spec network-capture note).
_TOKEN_HEADER_NAMES = {
    "authorization",
    "cookie",
    "x-auth-token",
    "x-access-token",
    "x-session-token",
    "x-api-key",
    "x-csrf-token",
}

MAX_CAPTURED_REQUESTS = 500  # oldest entries are dropped beyond this


def _enabled() -> bool:
    return os.getenv("ABRASIO_RECORD", "false").lower() == "true"


class RecordingSession:
    """In-memory recorder for a single agent RECORD run. One module-level singleton
    (`session`) is shared by the instrumented agent and the playbook tools."""

    def __init__(self) -> None:
        self._steps: list[dict] = []
        self._requests: list[dict] = []          # captured XHR/fetch log (T0 discovery)
        self._requests_dropped = 0               # entries evicted by MAX_CAPTURED_REQUESTS
        self._secrets: dict[str, str] = {}        # secret_ref -> raw value
        self._value_to_ref: dict[str, str] = {}   # raw value -> secret_ref
        self._schema: Optional[dict] = None
        self._transport: str = "browser"          # T2 default; capture_request → "http" (T0)
        # Tracks the page object the response listener is wired to (not a plain bool —
        # bug found in review, 2026-07-15: a bool never resets on browser restart, so a
        # session that closes and reopens its page mid-RECORD silently loses network
        # capture for the rest of the process; comparing page identity re-attaches
        # automatically whenever the actual page changes, no reset hook required).
        self._attached_page: Any = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return _enabled()

    # ── Secrets (C5) ─────────────────────────────────────────────────────────

    def register_secret(self, ref: str, value: str) -> None:
        with self._lock:
            self._secrets[ref] = value
            self._value_to_ref[value] = ref

    def _ref_for_value(self, value: str) -> Optional[str]:
        with self._lock:
            return self._value_to_ref.get(value)

    # ── Step recording (called from the instrumented agent methods) ──────────

    def _append(self, step: dict) -> None:
        with self._lock:
            self._steps.append(step)

    def record(self, op: str, **fields: Any) -> None:
        """Append a step for a mutating action. No-op unless recording is enabled."""
        if not self.enabled:
            return
        step: dict = {"op": op}
        for k, v in fields.items():
            if v is not None:
                step[k] = v
        self._append(step)

    async def record_fill(self, page: Any, selector: Optional[str], text: str) -> None:
        """Record a `fill`, substituting a `secret_ref` for registered values. No-op unless
        recording is enabled. Raises if a password field is filled with an unregistered
        value (C5). `selector=None` records a `type` into the focused element
        (browser_type) under the same rules."""
        if not self.enabled:
            return
        step: dict = {"op": "fill", "selector": selector} if selector else {"op": "type"}
        ref = self._ref_for_value(text)
        if ref:
            self._append({**step, "secret_ref": ref})
            return
        # ponytail: ":focus" misses focus inside iframes and, with nothing focused, waits
        # Playwright's default timeout; both fail closed (treated as a password field).
        if await self._is_password_field(page, selector or ":focus"):
            raise ValueError(
                f"Refusing to record a raw secret for password field '{selector or ':focus'}'. "
                f"Call playbook_register_secret(ref, value) before browser_fill/browser_type "
                f"so the playbook stores a secret_ref, never the plaintext."
            )
        self._append({**step, "text": text})

    async def _is_password_field(self, page: Any, selector: str) -> bool:
        """Fail CLOSED (bug found in review, 2026-07-15): if we can't determine the
        field type — a transient Playwright error, a stale handle, the page mid-
        navigation — we must NOT silently conclude "not a password field" and let a
        raw, unregistered value get recorded as plaintext. Treating the unknown case
        as "is a password field" only ever costs the agent one extra
        playbook_register_secret call; treating it as "not" risks leaking a real
        credential into `steps` (C5's whole purpose)."""
        try:
            t = await page.get_attribute(selector, "type")
            return (t or "").lower() == "password"
        except Exception:
            logger.warning(
                "Could not determine field type for '%s' — treating as a password "
                "field to avoid recording a raw value we can't verify is safe.",
                selector,
            )
            return True

    # ── Network capture (enables T0 compilation) ─────────────────────────────

    def attach(self, page: Any) -> None:
        """Attach the response listener, only while recording (keeps default MCP
        behaviour untouched when off). Safe to call every time a page is (re)created —
        re-attaches automatically if `page` differs from whatever we last attached to
        (e.g. after the browser closed and reopened mid-RECORD), and is a no-op if
        called again with the SAME page."""
        if not self.enabled or page is self._attached_page:
            return
        page.on("response", self._on_response)
        self._attached_page = page
        logger.info("RecordingSession: network capture attached")

    def _on_response(self, response: Any) -> None:
        # Sync handler using only sync Playwright properties — safe inside the event loop.
        try:
            req = response.request
            rtype = req.resource_type
            if rtype not in ("xhr", "fetch"):
                return
            with self._lock:
                self._requests.append({
                    "method": req.method,
                    "url": req.url,
                    "resource_type": rtype,
                    "status": response.status,
                    "request_headers": dict(req.headers),
                    "content_type": response.headers.get("content-type"),
                })
                if len(self._requests) > MAX_CAPTURED_REQUESTS:
                    del self._requests[0]
                    self._requests_dropped += 1
        except Exception:
            logger.debug("network capture failed for a response", exc_info=True)

    def list_requests(self) -> list[dict]:
        """Captured requests for the model, with token header values redacted. The raw
        values stay in `_requests` for capture_request, which stores them as secret_refs."""
        with self._lock:
            out = [
                {**e, "request_headers": {
                    k: "[redacted]" if k.lower() in _TOKEN_HEADER_NAMES else v
                    for k, v in (e.get("request_headers") or {}).items()
                }}
                for e in self._requests
            ]
            if self._requests_dropped:
                out.insert(0, {"truncated": f"{self._requests_dropped} older requests dropped"})
            return out

    # ── Playbook tool ops ────────────────────────────────────────────────────

    def mark_extract(self, schema: dict, selector: Optional[str] = None,
                     script: Optional[str] = None) -> None:
        """Append a terminal `extract` step and stash the schema for finalize (DECISION 4)."""
        with self._lock:
            self._schema = schema
        step: dict = {"op": "extract", "schema": schema}
        if selector:
            step["selector"] = selector
        if script:
            step["script"] = script
        self._append(step)

    def _header_ref(self, name: str, value: str) -> str:
        """One ref per distinct captured token value: `header:<name>` (lowercased), then
        `header:<name>:2`, … when the same header carried a different value on another
        captured request. A value already registered reuses its ref."""
        with self._lock:
            ref = self._value_to_ref.get(value)
            if ref:
                return ref
            base = ref = f"header:{name.lower()}"
            n = 2
            while ref in self._secrets:
                ref = f"{base}:{n}"
                n += 1
            self._secrets[ref] = value
            self._value_to_ref[value] = ref
            return ref

    def capture_request(self, url: str) -> dict:
        """Emit an `op:"request"` step from a captured XHR/fetch and switch the playbook to
        T0 (`transport="http"`). Each token-looking header gets its own secret ref
        (`secret_ref:header:cookie`, `secret_ref:header:authorization`, …), never raw."""
        match = None
        with self._lock:
            for entry in reversed(self._requests):
                if entry["url"] == url:
                    match = entry
                    break
        if match is None:
            raise ValueError(
                f"No captured XHR/fetch request for url: {url}. "
                f"Call browser_list_requests() to see the captured internal API calls."
            )
        headers: dict[str, str] = {}
        for name, value in (match.get("request_headers") or {}).items():
            if name.lower() in _TOKEN_HEADER_NAMES:
                headers[name] = f"secret_ref:{self._header_ref(name, value)}"
            else:
                headers[name] = value
        self._transport = "http"  # T0
        step = {
            "op": "request",
            "url": url,
            "request": {
                "method": match["method"],
                "headers": headers,
                "body_template": None,
                "response_path": None,  # agent refines later; null in M1
            },
        }
        self._append(step)
        return step

    def finalize(self, name: str, domain: str) -> dict:
        """Return the recorded trajectory JSON to POST to `api` `POST /playbooks`."""
        with self._lock:
            return {
                "name": name,
                "domain": domain,
                "transport": self._transport,
                "steps": list(self._steps),
                "schema": self._schema,
                "profile_domain": domain,  # DECISION 3: pool key is the domain, not a profile id
                "secrets": dict(self._secrets),
            }

    def reset(self) -> None:
        """Clear the recording (start a fresh trajectory)."""
        with self._lock:
            self._steps.clear()
            self._requests.clear()
            self._requests_dropped = 0
            self._secrets.clear()
            self._value_to_ref.clear()
            self._schema = None
            self._transport = "browser"


# Module-level singleton shared by the instrumented agent and the playbook tools.
session = RecordingSession()

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
``playbook_register_secret(ref, value)`` first; a `fill`/`type` whose value is registered
records a ``secret_ref``; typing an unregistered value into an element that looks like a
credential field (checked on the focused element, before any keystroke) is a hard error.
``finalize`` refuses if a registered value still shows up anywhere in ``steps`` and returns
the values under ``secrets`` for the API to seal; the ``playbook_finalize`` tool uploads
that body itself and never returns the values.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("abrasio_mcp.recording")

# Request headers that stay in clear in captures and listings. Every other header is
# treated as a possible credential: a secret ref in captured steps, "[redacted]" in
# browser_list_requests (an allowlist, so unknown token headers fail closed).
_CLEAR_HEADER_NAMES = {
    "content-type", "content-length", "origin", "referer", "user-agent",
    "x-requested-with", "cache-control", "pragma", "dnt",
}
_CLEAR_HEADER_PREFIXES = ("accept", "sec-")


def _is_secret_header(name: str) -> bool:
    name = name.lower()
    return name not in _CLEAR_HEADER_NAMES and not name.startswith(_CLEAR_HEADER_PREFIXES)


# The element that will receive the keys: document.activeElement, through shadow roots.
# Focus inside an iframe shows up here as the IFRAME element itself.
_ACTIVE_ELEMENT_JS = """
() => {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    if (!el) return null;
    return {
        tag: el.tagName,
        type: String(el.type || el.getAttribute('type') || '').toLowerCase(),
        autocomplete: String(el.getAttribute('autocomplete') || '').toLowerCase(),
    };
}
"""

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
        if not ref or not value:
            raise ValueError("playbook_register_secret needs a non-empty ref and value.")
        if ref.startswith("header:"):
            raise ValueError("Secret refs starting with 'header:' are reserved for captured headers.")
        with self._lock:
            old = self._secrets.get(ref)
            if old is not None and self._value_to_ref.get(old) == ref:
                del self._value_to_ref[old]  # re-registering a ref drops its old value's mapping
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

    async def fill_step(self, page: Any, selector: Optional[str], text: str) -> Optional[dict]:
        """Build the step for a `fill` (or a `type` when `selector` is None) BEFORE any
        keystroke, with the target already focused. Registered values become a
        `secret_ref`. Raises if the focused element looks like a credential field and the
        value is not registered (C5). None when recording is off. Pass the result to
        `add` once the typing succeeded."""
        if not self.enabled:
            return None
        step: dict = {"op": "fill", "selector": selector} if selector else {"op": "type"}
        ref = self._ref_for_value(text)
        if ref:
            return {**step, "secret_ref": ref}
        if await self._focused_is_secret(page):
            raise ValueError(
                "Refusing to type an unregistered value into a credential field (password, "
                "one-time code, card, iframe or unknown target). Call "
                "playbook_register_secret(ref, value) before browser_fill/browser_type "
                "so the playbook stores a secret_ref, never the plaintext."
            )
        return {**step, "text": text}

    def add(self, step: Optional[dict]) -> None:
        if step is not None:
            self._append(step)

    async def _focused_is_secret(self, page: Any) -> bool:
        """Inspect the element that will receive the keys (document.activeElement,
        through shadow roots). Fails CLOSED: an iframe (can't see inside), nothing
        focused, or any evaluate error counts as a credential field."""
        try:
            info = await page.evaluate(_ACTIVE_ELEMENT_JS)
        except Exception:
            logger.warning("Could not inspect the focused element; treating it as a credential field.")
            return True
        if not isinstance(info, dict):
            return True
        tokens = (info.get("autocomplete") or "").split()
        field = tokens[-1] if tokens else ""
        return (
            info.get("tag") in ("IFRAME", "BODY", "HTML")  # iframe, or nothing focused
            or info.get("type") == "password"
            or field.endswith("password")
            or field == "one-time-code"
            or field.startswith("cc-")
        )

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

    async def _on_response(self, response: Any) -> None:
        try:
            req = response.request
            rtype = req.resource_type
            if rtype not in ("xhr", "fetch"):
                return
            # all_headers(): `request.headers` omits Cookie; drop HTTP/2 pseudo-headers.
            headers = {k: v for k, v in (await req.all_headers()).items() if not k.startswith(":")}
            with self._lock:
                self._requests.append({
                    "method": req.method,
                    "url": req.url,
                    "resource_type": rtype,
                    "status": response.status,
                    "request_headers": headers,
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
                    k: "[redacted]" if _is_secret_header(k) else v
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
            if _is_secret_header(name):
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
        """Return the recorded trajectory JSON to POST to `api` `POST /playbooks`.
        Raises if `domain` does not match the first navigation, or if a registered
        secret value leaked into `steps` (evaluate script, press keys, URL, schema…)."""
        with self._lock:
            steps = list(self._steps)
            secrets = dict(self._secrets)
            schema, transport = self._schema, self._transport
        _check_domain(steps, domain)
        _check_no_secret_values(steps, secrets)
        return {
            "name": name,
            "domain": domain,
            "transport": transport,
            "steps": steps,
            "schema": schema,
            "profile_domain": domain,  # DECISION 3: pool key is the domain, not a profile id
            "secrets": secrets,
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


def _check_domain(steps: list[dict], domain: str) -> None:
    """The model names the domain, so check it against what was actually opened: the
    host of the first recorded navigate step must equal `domain` or be a subdomain of it.
    ponytail: no public-suffix list, so a 'co.uk'-style domain passes; a single label
    ('com') does not."""
    d = (domain or "").lower().rstrip(".")
    first = next((s for s in steps if s.get("op") == "navigate"), None)
    if first is None:
        raise ValueError("Refusing to finalize: no navigation was recorded, so the domain cannot be checked.")
    host = (urlsplit(str(first.get("url", ""))).hostname or "").rstrip(".")
    if "." not in d or not (host == d or host.endswith("." + d)):
        raise ValueError(
            f"Refusing to finalize: domain {domain!r} does not match the first navigated host {host!r}."
        )


def _check_no_secret_values(steps: list[dict], secrets: dict[str, str]) -> None:
    """Refuse if a secret value appears in any serialized step, or in a run of
    consecutive single-character `press` steps. Names the step, never the value.
    ponytail: captured-header values shorter than 8 chars are skipped (tiny header values
    like 'en' or '2' would match unrelated steps); user-registered values are always checked."""
    values = [v for ref, v in secrets.items() if v and (not ref.startswith("header:") or len(v) >= 8)]

    def _fail(i: int, op: Any) -> None:
        raise ValueError(
            f"Refusing to finalize: step {i} (op {op!r}) contains a registered secret value. "
            "Re-record that step without the secret (use browser_fill/browser_type with a "
            "registered value; secrets are injected at replay through secret_ref)."
        )

    for i, step in enumerate(steps):
        blob = json.dumps(step, ensure_ascii=False)
        if any(v in blob or json.dumps(v, ensure_ascii=False)[1:-1] in blob for v in values):
            _fail(i, step.get("op"))
    run, start = "", 0
    for i, step in enumerate(steps + [{}]):  # sentinel flushes the last run
        key = step.get("key") if step.get("op") == "press" else None
        if isinstance(key, str) and len(key) == 1:
            start = i if not run else start
            run += key
            continue
        if run and any(v in run for v in values):
            _fail(start, "press")
        run = ""


# Module-level singleton shared by the instrumented agent and the playbook tools.
session = RecordingSession()

"""
Tests for the RECORD middleware (abrasio_mcp/recording.py + tools/playbook.py).

Spec Component 1 + corrections C5/C7:
  * C7 — recording instruments the shared AbrasioBrowserAgent methods (every
    tool funnels through them), NOT MCP dispatch. `RecordingSession` is the
    unit-testable core: given a recorded call, it appends a normalized
    PlaybookStep. These tests drive that core directly, against its real API
    (`record`, `record_fill`, `register_secret`, `capture_request`, `finalize`).
  * C5 — a `fill` emits `secret_ref` only for a value registered via
    `register_secret`; a `fill` on a password-type input whose value is NOT
    registered is a hard error (never stored raw).

Behaviors covered:
  * ABRASIO_RECORD unset/false → RecordingSession is a no-op (records nothing).
  * ABRASIO_RECORD=true → each mutating call maps to the correct op.
  * Observation methods (get_text/get_html/find_elements/screenshot) in
    browser.py never call into the recorder at all (static source check).
  * fill: registered value → secret_ref (no raw); unregistered password → hard error.
  * capture_request → transport="http" + op:"request"; each token-like header
    becomes its own secret_ref:header:<name>, never raw.
  * finalize → documented shape.

`enabled` is a live property (reads ABRASIO_RECORD from the env on every check), so
tests just monkeypatch the env var per-case — no module reload needed.
"""

import asyncio
import inspect
import json

import pytest

from abrasio_mcp.recording import RecordingSession


# ── Fakes for the network-capture / password-detection paths ────────────────


class _FakeRequest:
    def __init__(self, method, url, resource_type, headers):
        self.method = method
        self.url = url
        self.resource_type = resource_type
        self.headers = headers


class _FakeResponse:
    def __init__(self, request, status, headers):
        self.request = request
        self.status = status
        self.headers = headers  # plain dict supports .get


def _seed_captured_request(s: RecordingSession, url: str, method="GET", headers=None):
    """Feed a fake XHR/fetch response through the same path `attach()` wires to
    `page.on("response", ...)`, without needing a real Playwright page."""
    req = _FakeRequest(method, url, "xhr", headers or {})
    resp = _FakeResponse(req, 200, {"content-type": "application/json"})
    s._on_response(resp)  # the sync capture hook `attach()` registers


class _FakePage:
    """Minimal async page stub for record_fill's password-type detection."""

    def __init__(self, field_types: dict[str, str] | None = None):
        self._field_types = field_types or {}

    async def get_attribute(self, selector, attr):
        if attr == "type":
            return self._field_types.get(selector)
        return None


# ── No-op when disabled ─────────────────────────────────────────────────────


def test_disabled_records_nothing(monkeypatch):
    monkeypatch.delenv("ABRASIO_RECORD", raising=False)
    s = RecordingSession()
    assert s.enabled is False

    s.record("navigate", url="https://book.example/")
    s.record("click", selector="button#go")

    assert s.finalize("x", "book.example")["steps"] == []


def test_disabled_record_fill_is_also_a_no_op(monkeypatch):
    monkeypatch.delenv("ABRASIO_RECORD", raising=False)
    s = RecordingSession()
    asyncio.run(s.record_fill(_FakePage(), "#pw", "whatever"))
    assert s.finalize("x", "book.example")["steps"] == []


# ── Tool → op mapping ────────────────────────────────────────────────────────


def test_navigate_maps_to_navigate_step(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.record("navigate", url="https://book.example/odds")

    step = s.finalize("x", "book.example")["steps"][-1]
    assert step["op"] == "navigate"
    assert step["url"] == "https://book.example/odds"


def test_click_maps_to_click_step(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.record("click", selector="button#login")

    step = s.finalize("x", "book.example")["steps"][-1]
    assert step["op"] == "click"
    assert step["selector"] == "button#login"


def test_type_press_scroll_hover_wait_evaluate_ops(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.record("type", text="hello")
    s.record("press", key="Enter")
    s.record("scroll", pixels=800)
    s.record("hover", selector=".menu")
    s.record("wait_for", selector=".ready", timeout=5000)
    s.record("evaluate", script="() => document.title")

    steps = s.finalize("x", "book.example")["steps"]
    assert [st["op"] for st in steps] == [
        "type", "press", "scroll", "hover", "wait_for", "evaluate",
    ]
    by_op = {st["op"]: st for st in steps}
    assert by_op["type"]["text"] == "hello"
    assert by_op["press"]["key"] == "Enter"
    assert by_op["scroll"]["pixels"] == 800
    assert by_op["hover"]["selector"] == ".menu"
    assert by_op["wait_for"]["selector"] == ".ready"
    assert by_op["wait_for"]["timeout"] == 5000
    assert by_op["evaluate"]["script"] == "() => document.title"


# ── Observation methods never call into the recorder (C7) ──────────────────
#
# RecordingSession.record() is a dumb generic recorder — it has no allowlist of
# ops, so this invariant lives at the call site: browser.py's observation
# methods (get_text/get_html/find_elements/screenshot) must never reference
# `_recording` at all. Verified by inspecting their source, matching how
# `interact.py`/`navigate.py` methods (which DO call `_recording.record`) were
# instrumented per the browser.py diff.

def test_observation_methods_not_recorded():
    from abrasio_mcp.browser import AbrasioBrowserAgent

    for name in ("get_text", "get_html", "find_elements", "screenshot"):
        src = inspect.getsource(getattr(AbrasioBrowserAgent, name))
        assert "_recording" not in src, f"{name} must not call the recorder"


# ── Secret handling on fill (C5) ─────────────────────────────────────────────


def test_fill_with_registered_secret_emits_secret_ref(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.register_secret("vault://bet365/pw", "hunter2-raw-password")

    page = _FakePage({"#pw": "password"})
    asyncio.run(s.record_fill(page, "#pw", "hunter2-raw-password"))

    step = s.finalize("x", "book.example")["steps"][-1]
    assert step["op"] == "fill"
    assert step["selector"] == "#pw"
    assert step.get("secret_ref") == "vault://bet365/pw"
    # The raw secret must never be stored.
    assert "text" not in step
    assert "hunter2-raw-password" not in json.dumps(step)


def test_fill_non_secret_keeps_raw_text(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    page = _FakePage()  # not a password field
    asyncio.run(s.record_fill(page, "#search", "laptop deals"))

    step = s.finalize("x", "book.example")["steps"][-1]
    assert step["op"] == "fill"
    assert step.get("secret_ref") is None
    assert step.get("text") == "laptop deals"


def test_fill_unregistered_password_is_hard_error(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    page = _FakePage({"#pw": "password"})

    with pytest.raises(ValueError):
        asyncio.run(s.record_fill(page, "#pw", "leaked-secret"))

    # And nothing raw was recorded — the raise happens before the append.
    assert "leaked-secret" not in json.dumps(s.finalize("x", "book.example")["steps"])


def test_fill_fails_closed_when_field_type_cannot_be_determined(monkeypatch):
    """Bug found in review, 2026-07-15: _is_password_field used to swallow ANY
    exception from get_attribute (a transient Playwright error, a stale handle, the
    page mid-navigation) and silently return False — recording the raw, unregistered
    value as plaintext. It must fail CLOSED: treat "can't tell" as "assume password"
    so an unregistered value is refused, never silently recorded."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()

    class _ThrowingPage:
        async def get_attribute(self, selector, attr):
            raise RuntimeError("page navigated away mid-check")

    with pytest.raises(ValueError):
        asyncio.run(s.record_fill(_ThrowingPage(), "#unknown", "could-be-a-real-password"))

    assert "could-be-a-real-password" not in json.dumps(s.finalize("x", "book.example")["steps"])


def test_type_into_focused_password_field_is_guarded_like_fill(monkeypatch):
    """browser_type records via record_fill(page, None, text): the focused element
    (":focus") gets the same password-field guard and secret_ref substitution."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    page = _FakePage({":focus": "password"})

    with pytest.raises(ValueError):
        asyncio.run(s.record_fill(page, None, "typed-secret"))

    s.register_secret("vault://x/pw", "typed-secret")
    asyncio.run(s.record_fill(page, None, "typed-secret"))
    asyncio.run(s.record_fill(_FakePage(), None, "plain words"))

    steps = s.finalize("x", "book.example")["steps"]
    assert steps == [
        {"op": "type", "secret_ref": "vault://x/pw"},
        {"op": "type", "text": "plain words"},
    ]

    from abrasio_mcp.browser import AbrasioBrowserAgent
    assert "record_fill(" in inspect.getsource(AbrasioBrowserAgent.type_text)


# ── Network capture → T0 request step ───────────────────────────────────────


def test_capture_request_sets_http_transport_and_request_step(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    _seed_captured_request(
        s,
        "https://book.example/api/odds",
        method="GET",
        headers={"authorization": "Bearer live-session-token-value"},
    )

    s.capture_request("https://book.example/api/odds")

    out = s.finalize("x", "book.example")
    assert out["transport"] == "http"
    step = out["steps"][-1]
    assert step["op"] == "request"
    blob = json.dumps(step)
    # token-like header stored as a ref, never raw
    assert "live-session-token-value" not in blob
    assert "secret_ref" in blob


def test_list_requests_redacts_tokens_but_capture_still_gets_them(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    _seed_captured_request(
        s,
        "https://book.example/api/odds",
        headers={"Cookie": "sid=raw-cookie", "authorization": "Bearer raw-token", "accept": "json"},
    )

    listed = s.list_requests()[0]["request_headers"]
    assert listed == {"Cookie": "[redacted]", "authorization": "[redacted]", "accept": "json"}

    s.capture_request("https://book.example/api/odds")
    out = s.finalize("x", "book.example")
    # Each token header gets its own ref — Cookie and Authorization used to collide.
    assert out["steps"][-1]["request"]["headers"] == {
        "Cookie": "secret_ref:header:cookie",
        "authorization": "secret_ref:header:authorization",
        "accept": "json",
    }
    assert out["secrets"] == {"header:cookie": "sid=raw-cookie", "header:authorization": "Bearer raw-token"}

    # Same header, different value on another request → a new ref, the first one intact.
    _seed_captured_request(s, "https://book.example/api/other", headers={"Cookie": "sid=other"})
    s.capture_request("https://book.example/api/other")
    out = s.finalize("x", "book.example")
    assert out["steps"][-1]["request"]["headers"] == {"Cookie": "secret_ref:header:cookie:2"}
    assert out["secrets"]["header:cookie"] == "sid=raw-cookie"
    assert out["secrets"]["header:cookie:2"] == "sid=other"


def test_captured_requests_are_capped_with_marker(monkeypatch):
    from abrasio_mcp.recording import MAX_CAPTURED_REQUESTS

    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    for i in range(MAX_CAPTURED_REQUESTS + 3):
        _seed_captured_request(s, f"https://book.example/api/{i}")

    listed = s.list_requests()
    assert listed[0] == {"truncated": "3 older requests dropped"}
    assert len(listed) == MAX_CAPTURED_REQUESTS + 1
    assert listed[-1]["url"] == f"https://book.example/api/{MAX_CAPTURED_REQUESTS + 2}"


def test_capture_request_without_a_seen_request_raises(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    with pytest.raises(ValueError):
        s.capture_request("https://never-seen.example/api")


# ── attach() re-attaches after a browser restart ────────────────────────────


class _FakePageWithOn:
    """Minimal page stub for attach() — just records .on() registrations."""

    def __init__(self):
        self.on_calls: list[tuple[str, object]] = []

    def on(self, event, handler):
        self.on_calls.append((event, handler))


def test_attach_reattaches_to_a_new_page_after_browser_restart(monkeypatch):
    """Bug found in review, 2026-07-15: attach() used to gate on a plain bool that
    never reset, so a browser close+reopen mid-RECORD (ensure_started() creates a new
    page and calls attach() again) silently left the NEW page with no listener —
    browser_list_requests() would return [] and T0 capture would be lost for the rest
    of the process. Re-attaching must be keyed on page identity, not a one-shot flag."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()

    page_1 = _FakePageWithOn()
    s.attach(page_1)
    assert len(page_1.on_calls) == 1

    # Calling attach() again with the SAME page must stay a no-op.
    s.attach(page_1)
    assert len(page_1.on_calls) == 1

    # A browser restart hands attach() a genuinely different page object — must
    # re-attach.
    page_2 = _FakePageWithOn()
    s.attach(page_2)
    assert len(page_2.on_calls) == 1
    assert page_2.on_calls[0][0] == "response"


# ── finalize shape ───────────────────────────────────────────────────────────


def test_finalize_returns_documented_shape(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.record("navigate", url="https://book.example/")

    out = s.finalize("bet365-odds-br", "book.example")

    for key in ("name", "domain", "transport", "steps", "schema", "profile_domain", "secrets"):
        assert key in out, f"finalize output missing '{key}'"
    assert out["name"] == "bet365-odds-br"
    assert out["domain"] == "book.example"
    assert out["profile_domain"] == "book.example"  # DECISION 3: pool key = domain
    assert isinstance(out["steps"], list) and len(out["steps"]) >= 1

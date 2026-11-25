"""
Tests for the RECORD middleware (abrasio_mcp/recording.py + tools/playbook.py).

Spec Component 1 + corrections C5/C7:
  * C7 — recording instruments the shared AbrasioBrowserAgent methods (every
    tool funnels through them), NOT MCP dispatch. `RecordingSession` is the
    unit-testable core: given a recorded call, it appends a normalized
    PlaybookStep. These tests drive that core directly, against its real API
    (`record`, `fill_step`/`add`, `register_secret`, `capture_request`, `finalize`).
  * C5 — a `fill` emits `secret_ref` only for a value registered via
    `register_secret`; typing an unregistered value into a focused credential field
    is a hard error, decided before any keystroke (never stored raw).

`enabled` is a live property (reads ABRASIO_RECORD from the env on every check), so
tests just monkeypatch the env var per-case — no module reload needed. Tests that only
inspect what was recorded read `s._steps` / `s._secrets`; `finalize` is exercised where
its own checks (domain, leaked secret values) are the subject.
"""

import asyncio
import inspect
import json
import types

import pytest

from abrasio_mcp.recording import RecordingSession


# ── Fakes for the network-capture / credential-field detection paths ────────


class _FakeRequest:
    def __init__(self, method, url, resource_type, headers, all_headers=None):
        self.method = method
        self.url = url
        self.resource_type = resource_type
        self.headers = headers  # Playwright's `request.headers`: no Cookie
        self._all = headers if all_headers is None else all_headers

    async def all_headers(self):
        return self._all


class _FakeResponse:
    def __init__(self, request, status, headers):
        self.request = request
        self.status = status
        self.headers = headers  # plain dict supports .get


def _seed_captured_request(s: RecordingSession, url: str, method="GET", headers=None, all_headers=None):
    """Feed a fake XHR/fetch response through the same handler `attach()` wires to
    `page.on("response", ...)`, without needing a real Playwright page."""
    req = _FakeRequest(method, url, "xhr", headers or {}, all_headers)
    resp = _FakeResponse(req, 200, {"content-type": "application/json"})
    asyncio.run(s._on_response(resp))


_TEXT = {"tag": "INPUT", "type": "text", "autocomplete": ""}
_PASSWORD = {"tag": "INPUT", "type": "password", "autocomplete": ""}


class _FakePage:
    """page.evaluate(_ACTIVE_ELEMENT_JS) stub: returns the focused element's info."""

    def __init__(self, active=_TEXT):
        self.active = active

    async def evaluate(self, script):
        return self.active


def _fill(s, page, selector, text):
    s.add(asyncio.run(s.fill_step(page, selector, text)))


# ── No-op when disabled ─────────────────────────────────────────────────────


def test_disabled_records_nothing(monkeypatch):
    monkeypatch.delenv("ABRASIO_RECORD", raising=False)
    s = RecordingSession()
    assert s.enabled is False

    s.record("navigate", url="https://book.example/")
    s.record("click", selector="button#go")

    assert s._steps == []


def test_disabled_fill_step_is_also_a_no_op(monkeypatch):
    monkeypatch.delenv("ABRASIO_RECORD", raising=False)
    s = RecordingSession()
    assert asyncio.run(s.fill_step(_FakePage(_PASSWORD), "#pw", "whatever")) is None
    assert s._steps == []


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

    step = s._steps[-1]
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

    steps = s._steps
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


def test_observation_methods_not_recorded():
    from abrasio_mcp.browser import AbrasioBrowserAgent

    for name in ("get_text", "get_html", "find_elements", "screenshot"):
        src = inspect.getsource(getattr(AbrasioBrowserAgent, name))
        assert "_recording" not in src, f"{name} must not call the recorder"


def test_history_navigation_is_recorded_as_navigate(monkeypatch):
    from abrasio_mcp.browser import AbrasioBrowserAgent
    from abrasio_mcp import browser as browser_mod

    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    monkeypatch.setattr(browser_mod, "_recording", s)

    class _Page:
        url = "https://book.example/prev"

        async def go_back(self, **kw):
            pass

        go_forward = reload = go_back

        async def title(self):
            return "t"

    agent = object.__new__(AbrasioBrowserAgent)
    agent._page = _Page()
    for m in (agent.go_back, agent.go_forward, agent.reload):
        asyncio.run(m())
    assert s._steps == [{"op": "navigate", "url": "https://book.example/prev"}] * 3


# ── Secret handling on fill (C5) ─────────────────────────────────────────────


def test_fill_with_registered_secret_emits_secret_ref(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.register_secret("vault://bet365/pw", "hunter2-raw-password")

    _fill(s, _FakePage(_PASSWORD), "#pw", "hunter2-raw-password")

    step = s._steps[-1]
    assert step == {"op": "fill", "selector": "#pw", "secret_ref": "vault://bet365/pw"}


def test_fill_non_secret_keeps_raw_text(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    _fill(s, _FakePage(), "#search", "laptop deals")

    assert s._steps[-1] == {"op": "fill", "selector": "#search", "text": "laptop deals"}


@pytest.mark.parametrize("active", [
    _PASSWORD,
    {"tag": "INPUT", "type": "text", "autocomplete": "current-password"},
    {"tag": "INPUT", "type": "text", "autocomplete": "section-login new-password"},
    {"tag": "INPUT", "type": "tel", "autocomplete": "one-time-code"},
    {"tag": "INPUT", "type": "text", "autocomplete": "cc-number"},
    {"tag": "IFRAME", "type": "", "autocomplete": ""},   # focus inside a frame: can't see
    {"tag": "BODY", "type": "", "autocomplete": ""},     # nothing focused
    None,                                                  # unknown
])
def test_unregistered_value_into_credential_field_is_refused(monkeypatch, active):
    """The guard looks at the focused element, not the selector the agent passed: a
    label/wrapper selector that focuses a password input is still caught."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    with pytest.raises(ValueError):
        asyncio.run(s.fill_step(_FakePage(active), "label.password-wrapper", "leaked-secret"))
    assert s._steps == []


def test_fill_fails_closed_when_focused_element_cannot_be_inspected(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()

    class _ThrowingPage:
        async def evaluate(self, script):
            raise RuntimeError("page navigated away mid-check")

    with pytest.raises(ValueError):
        asyncio.run(s.fill_step(_ThrowingPage(), "#unknown", "could-be-a-real-password"))
    assert s._steps == []


def test_type_into_focused_password_field_is_guarded_like_fill(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()

    with pytest.raises(ValueError):
        asyncio.run(s.fill_step(_FakePage(_PASSWORD), None, "typed-secret"))

    s.register_secret("vault://x/pw", "typed-secret")
    _fill(s, _FakePage(_PASSWORD), None, "typed-secret")
    _fill(s, _FakePage(), None, "plain words")

    assert s._steps == [
        {"op": "type", "secret_ref": "vault://x/pw"},
        {"op": "type", "text": "plain words"},
    ]


def _agent_with_keyboard(monkeypatch, s, active):
    """An agent whose page logs every keystroke; click/type helpers are fakes."""
    from abrasio_mcp import browser as browser_mod
    from abrasio_mcp.browser import AbrasioBrowserAgent

    keys = []

    async def _click(page, selector):
        keys.append(("click", selector))

    async def _type(page, text):
        keys.append(("type", text))

    async def _press(key):
        keys.append(("press", key))

    monkeypatch.setattr(browser_mod, "_recording", s)
    monkeypatch.setattr(browser_mod, "click_human", _click)
    monkeypatch.setattr(browser_mod, "type_human", _type)
    agent = object.__new__(AbrasioBrowserAgent)
    agent._page = _FakePage(active)
    agent._page.keyboard = types.SimpleNamespace(press=_press)
    return agent, keys


def test_fill_refuses_before_any_keystroke(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    agent, keys = _agent_with_keyboard(monkeypatch, s, _PASSWORD)

    with pytest.raises(ValueError):
        asyncio.run(agent.fill("label[for=pw]", "hunter2"))
    assert keys == [("click", "label[for=pw]")]  # focused, then refused: nothing typed

    with pytest.raises(ValueError):
        asyncio.run(agent.type_text("hunter2"))
    assert keys == [("click", "label[for=pw]")]

    agent._page.active = _TEXT
    asyncio.run(agent.fill("#q", "laptops"))
    assert keys[1:] == [("click", "#q"), ("press", "Control+a"), ("type", "laptops")]
    assert s._steps == [{"op": "fill", "selector": "#q", "text": "laptops"}]


# ── register_secret validation ──────────────────────────────────────────────


def test_register_secret_validates_and_drops_stale_mapping(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    for ref, value in (("", "v"), ("r", ""), ("header:cookie", "v")):
        with pytest.raises(ValueError):
            s.register_secret(ref, value)

    s.register_secret("vault://pw", "old-pass")
    s.register_secret("vault://pw", "new-pass")
    assert s._ref_for_value("old-pass") is None
    assert s._ref_for_value("new-pass") == "vault://pw"
    assert s._secrets == {"vault://pw": "new-pass"}


# ── finalize: leaked secret values and domain check ─────────────────────────


@pytest.mark.parametrize("leak", [
    {"op": "evaluate", "script": "() => login('hunter2-raw')"},
    {"op": "navigate", "url": "https://book.example/?pw=hunter2-raw"},
    {"op": "extract", "schema": {"x": "hunter2-raw"}},
    {"op": "evaluate", "script": "say('hunter\"2')"},  # JSON-escaped in the blob
])
def test_finalize_refuses_secret_value_in_any_step(monkeypatch, leak):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.register_secret("vault://pw", "hunter2-raw" if "hunter2-raw" in json.dumps(leak) else 'hunter"2')
    s.record("navigate", url="https://book.example/")
    s._append(leak)

    with pytest.raises(ValueError) as exc:
        s.finalize("x", "book.example")
    msg = str(exc.value)
    assert "step 1" in msg and leak["op"] in msg
    assert "hunter" not in msg


def test_finalize_refuses_secret_typed_key_by_key(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.register_secret("vault://pin", "4821")
    s.record("navigate", url="https://book.example/")
    s.record("press", key="Tab")
    for k in "4821":
        s.record("press", key=k)
    s.record("press", key="Enter")

    with pytest.raises(ValueError) as exc:
        s.finalize("x", "book.example")
    assert "step 2" in str(exc.value) and "4821" not in str(exc.value)


@pytest.mark.parametrize("domain,ok", [
    ("book.example", True),
    ("www.book.example", True),   # exact host
    ("BOOK.example.", True),
    ("attacker.example", False),
    ("example", False),            # single label
    ("ook.example", False),        # suffix but not a parent domain
])
def test_finalize_domain_must_match_first_navigation(monkeypatch, domain, ok):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.record("navigate", url="https://www.book.example/login")
    s.record("navigate", url="https://attacker.example/")
    if ok:
        s.finalize("x", domain)
    else:
        with pytest.raises(ValueError):
            s.finalize("x", domain)


def test_finalize_without_navigation_is_refused(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    s.record("click", selector="#a")
    with pytest.raises(ValueError):
        s.finalize("x", "book.example")


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

    assert s._transport == "http"
    step = s._steps[-1]
    assert step["op"] == "request"
    blob = json.dumps(step)
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
    # Each token header gets its own ref — Cookie and Authorization used to collide.
    assert s._steps[-1]["request"]["headers"] == {
        "Cookie": "secret_ref:header:cookie",
        "authorization": "secret_ref:header:authorization",
        "accept": "json",
    }
    assert s._secrets == {"header:cookie": "sid=raw-cookie", "header:authorization": "Bearer raw-token"}

    # Same header, different value on another request → a new ref, the first one intact.
    _seed_captured_request(s, "https://book.example/api/other", headers={"Cookie": "sid=other"})
    s.capture_request("https://book.example/api/other")
    assert s._steps[-1]["request"]["headers"] == {"Cookie": "secret_ref:header:cookie:2"}
    assert s._secrets["header:cookie"] == "sid=raw-cookie"
    assert s._secrets["header:cookie:2"] == "sid=other"


def test_only_benign_headers_stay_in_clear(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    tokens = {
        "x-xsrf-token": "t1", "proxy-authorization": "t2", "x-amz-security-token": "t3",
        "x-goog-api-key": "t4", "apikey": "t5",
    }
    benign = {
        "Accept-Language": "pt-BR", "content-type": "application/json", "origin": "https://book.example",
        "referer": "https://book.example/", "user-agent": "UA", "sec-ch-ua": '"Chromium"',
        "x-requested-with": "XMLHttpRequest", "cache-control": "no-cache",
    }
    _seed_captured_request(s, "https://book.example/api", headers={**tokens, **benign})

    listed = s.list_requests()[0]["request_headers"]
    assert {k: listed[k] for k in tokens} == {k: "[redacted]" for k in tokens}
    assert {k: listed[k] for k in benign} == benign

    s.capture_request("https://book.example/api")
    headers = s._steps[-1]["request"]["headers"]
    assert {k: headers[k] for k in tokens} == {k: f"secret_ref:header:{k}" for k in tokens}
    assert {k: headers[k] for k in benign} == benign


def test_capture_uses_all_headers_so_cookie_is_seen(monkeypatch):
    """Patchright's `request.headers` omits Cookie; all_headers() has it (plus HTTP/2
    pseudo-headers, which are dropped)."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()
    _seed_captured_request(
        s, "https://book.example/api",
        headers={"accept": "json"},
        all_headers={"accept": "json", "cookie": "sid=from-all-headers", ":authority": "book.example"},
    )
    s.capture_request("https://book.example/api")
    assert s._steps[-1]["request"]["headers"] == {"accept": "json", "cookie": "secret_ref:header:cookie"}
    assert s._secrets == {"header:cookie": "sid=from-all-headers"}


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
    never reset, so a browser close+reopen mid-RECORD silently left the NEW page with no
    listener. Re-attaching must be keyed on page identity, not a one-shot flag."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    s = RecordingSession()

    page_1 = _FakePageWithOn()
    s.attach(page_1)
    assert len(page_1.on_calls) == 1

    s.attach(page_1)
    assert len(page_1.on_calls) == 1

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

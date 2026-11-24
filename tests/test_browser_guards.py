"""Input guards on AbrasioBrowserAgent: navigation targets, timeouts and output caps."""

import asyncio
import types

import pytest

from abrasio_mcp import browser as browser_mod
from abrasio_mcp.browser import (
    ALLOW_PRIVATE_HOSTS_ENV,
    MAX_OUTPUT_CHARS,
    AbrasioBrowserAgent,
    _cap_output,
    _guard_request,
    check_current_url,
    check_navigation_target,
    parse_host,
)


def _check(url, cloud=False):
    asyncio.run(check_navigation_target(url, cloud=cloud))


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "http://localhost:8080/",
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::ffff:127.0.0.1]/",
    "http://127.0.0.1\\@example.com/",
])
def test_local_mode_blocks_unsafe_targets(monkeypatch, url):
    monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)
    with pytest.raises(ValueError):
        _check(url)


def test_local_opt_in_allows_loopback_but_never_metadata(monkeypatch):
    monkeypatch.setenv(ALLOW_PRIVATE_HOSTS_ENV, "true")
    _check("http://127.0.0.1:3000/")
    with pytest.raises(ValueError):
        _check("http://169.254.169.254/")
    with pytest.raises(ValueError):
        _check("http://[fe80::1]/")


def test_public_ip_literal_is_allowed():
    _check("https://93.184.216.34/")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://localhost/",
    "http://127.0.0.1/",
    "http://169.254.169.254/",
    "http://[fd00:ec2::254]/",
])
def test_cloud_mode_blocks_scheme_and_ip_literals(monkeypatch, url):
    monkeypatch.setenv(ALLOW_PRIVATE_HOSTS_ENV, "true")  # ignored in cloud mode
    with pytest.raises(ValueError):
        _check(url, cloud=True)


def test_cloud_mode_does_not_resolve_hostnames():
    _check("https://example.com/", cloud=True)  # names are left to the cloud worker


@pytest.mark.parametrize("host,expected", [
    ("127.1", "127.0.0.1"),
    ("0x7f.1", "127.0.0.1"),
    ("017700000001", "127.0.0.1"),
    ("2130706433", "127.0.0.1"),
    ("0x7f000001", "127.0.0.1"),
    ("0251.0376.0251.0376", "169.254.169.254"),
    ("%31%32%37.0.0.1", "127.0.0.1"),
    ("127.0.0.1.", "127.0.0.1"),
    ("１２７.0.0.1", "127.0.0.1"),  # fullwidth digits, IDNA-normalised
    ("localhost.", "127.0.0.1"),
    ("api.localhost", "127.0.0.1"),
    ("example.com", None),
    ("cafe.bad", None),  # hex letters only, but the last label is not a number
])
def test_parse_host_matches_browser(host, expected):
    _, ip = parse_host(host)
    assert (str(ip) if ip else None) == expected


@pytest.mark.parametrize("host", ["1.2.3.4.5", "256.1.1.1", "1.0x100.1", "08.0.0.1", "1..1", "1.0xg.1"])
def test_parse_host_rejects_malformed_numeric(host):
    with pytest.raises(ValueError):
        parse_host(host)


@pytest.mark.parametrize("url", [
    "http://0x7f.1/", "http://017700000001/", "http://127.1/", "http://2130706433/",
    "http://%31%32%37.0.0.1/", "http://169.254.169.254./", "http://dev.localhost/",
    "http://08.0.0.1/",
])
def test_cloud_mode_blocks_browser_numeric_forms(url):
    with pytest.raises(ValueError):
        _check(url, cloud=True)


class _FakeRequest:
    def __init__(self, url, navigation):
        self.url = url
        self._nav = navigation

    def is_navigation_request(self):
        return self._nav


class _FakeRoute:
    def __init__(self):
        self.calls = []

    async def abort(self, code=None):
        self.calls.append("abort")

    async def fallback(self):
        self.calls.append("fallback")


@pytest.mark.parametrize("url,navigation,opt_in,expected", [
    ("http://127.0.0.1:9/", True, False, "abort"),
    ("http://127.0.0.1:9/", True, True, "fallback"),        # opt-in applies to the route too
    ("http://169.254.169.254/", True, True, "abort"),       # metadata blocked regardless
    ("https://93.184.216.34/", True, False, "fallback"),
    # non-navigation (subresource) requests: only literal / localhost hosts are checked
    ("http://127.0.0.1:9/api", False, False, "abort"),
    ("http://dev.localhost/app.js", False, False, "abort"),
    ("http://10.0.0.5/img.png", False, True, "fallback"),   # literal + opt-in: allowed
    ("http://169.254.169.254/latest", False, True, "abort"),  # metadata even with opt-in
    ("https://93.184.216.34/x.js", False, False, "fallback"),
])
def test_request_route_guard(monkeypatch, url, navigation, opt_in, expected):
    if opt_in:
        monkeypatch.setenv(ALLOW_PRIVATE_HOSTS_ENV, "true")
    else:
        monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)
    route = _FakeRoute()
    asyncio.run(_guard_request(route, _FakeRequest(url, navigation)))
    assert route.calls == [expected]


def test_hostname_subresource_untouched_without_dns(monkeypatch):
    def _no_dns(*a, **k):
        raise AssertionError("subresource hostnames must not be resolved")

    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", _no_dns)
    route = _FakeRoute()
    asyncio.run(_guard_request(route, _FakeRequest("https://cdn.example.com/a.js", False)))
    assert route.calls == ["fallback"]


def test_guard_is_registered_on_the_context_so_popups_are_covered(monkeypatch):
    class _Context:
        def __init__(self):
            self.routes = []

        async def route(self, pattern, handler):
            self.routes.append((pattern, handler))

    class _Page:
        def __init__(self):
            self.context = _Context()
            self.events = []

        async def route(self, *a):
            raise AssertionError("guard must be context-scoped, not page-scoped")

        def on(self, event, handler):
            self.events.append(event)

    page = _Page()

    class _Abrasio:
        def __init__(self, config):
            pass

        async def start(self):
            pass

        async def new_page(self):
            return page

    monkeypatch.setattr(browser_mod, "Abrasio", _Abrasio)
    agent = object.__new__(AbrasioBrowserAgent)
    agent._config = types.SimpleNamespace(is_cloud_mode=False)
    agent._page = None
    agent._lock = asyncio.Lock()
    asyncio.run(agent.ensure_started())

    assert page.context.routes == [("**/*", _guard_request)]
    assert "framenavigated" in page.events


@pytest.mark.parametrize("url,ok", [
    ("about:blank", True), ("about:srcdoc", True), ("data:text/html,hi", True),
    ("blob:https://93.184.216.34/uuid", True), ("blob:http://127.0.0.1/uuid", False),
    ("http://127.0.0.1/", False), ("chrome-error://chromewebdata/", False),
])
def test_check_current_url_allowed_schemes(monkeypatch, url, ok):
    monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)
    if ok:
        asyncio.run(check_current_url(url, cloud=False))
    else:
        with pytest.raises(ValueError):
            asyncio.run(check_current_url(url, cloud=False))


def test_read_tools_refuse_when_current_url_fails(monkeypatch):
    monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)
    read = []

    class _Page:
        url = "http://127.0.0.1:8080/admin"  # e.g. landed here via a redirect after a click
        frames = [types.SimpleNamespace(url=url)]

        async def inner_text(self, sel):
            read.append(sel)
            return "secret admin page"

    agent = object.__new__(AbrasioBrowserAgent)
    agent._config = types.SimpleNamespace(is_cloud_mode=False)
    agent._page = _Page()
    with pytest.raises(ValueError):
        asyncio.run(agent.get_text())
    assert read == []


def test_read_refuses_while_a_refused_subframe_is_present(monkeypatch):
    monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)
    shots = []

    class _Page:
        url = "https://93.184.216.34/"
        frames = [
            types.SimpleNamespace(url=url),
            types.SimpleNamespace(url="https://93.184.216.34/ad"),  # same host: one check
            types.SimpleNamespace(url="about:blank"),
            types.SimpleNamespace(url="http://169.254.169.254/latest/meta-data/"),  # redirected iframe
        ]

        async def screenshot(self, type):
            shots.append(type)
            return b"png"

    agent = object.__new__(AbrasioBrowserAgent)
    agent._config = types.SimpleNamespace(is_cloud_mode=False)
    agent._page = _Page()
    with pytest.raises(ValueError):
        asyncio.run(agent.screenshot())
    assert shots == []


def test_read_discards_result_when_listener_refuses_mid_read(monkeypatch):
    """The page hops to a refused address during the read and the listener sends it
    back to about:blank, so the after-check alone would pass: the counter catches it."""
    monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)

    class _Page:
        url = "https://93.184.216.34/"
        frames = [types.SimpleNamespace(url=url)]

    agent = object.__new__(AbrasioBrowserAgent)
    agent._config = types.SimpleNamespace(is_cloud_mode=False)
    agent._page = _Page()

    async def _goto(url):
        pass

    async def _fn():
        await agent._leave_if_unsafe(types.SimpleNamespace(url="http://127.0.0.1/", goto=_goto))
        return "refused page content"

    with pytest.raises(ValueError):
        asyncio.run(agent._read(_fn))


@pytest.mark.parametrize("parent", [None, "main-frame"])
def test_framenavigated_listener_leaves_refused_frame(monkeypatch, parent):
    monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)
    gone = []

    async def _goto(url):
        gone.append(url)

    frame = types.SimpleNamespace(parent_frame=parent, url="http://127.0.0.1/", goto=_goto)
    agent = object.__new__(AbrasioBrowserAgent)
    agent._config = types.SimpleNamespace(is_cloud_mode=False)
    asyncio.run(agent._leave_if_unsafe(frame))
    assert gone == ["about:blank"]  # main frame and subframe alike


def test_navigate_rechecks_final_url_after_redirect(monkeypatch):
    """3xx hops never reach page.route, so navigate() re-checks where it landed."""
    monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)

    class _RedirectingPage:
        url = "about:blank"

        async def goto(self, url, **kw):
            # a public page answering 302 -> loopback
            self.url = "http://127.0.0.1:8080/admin" if url.startswith("https://93.") else url

    agent = object.__new__(AbrasioBrowserAgent)
    agent._config = types.SimpleNamespace(is_cloud_mode=False)
    agent._page = _RedirectingPage()

    with pytest.raises(ValueError):
        asyncio.run(agent.navigate("https://93.184.216.34/"))
    assert agent._page.url == "about:blank"


@pytest.mark.parametrize("given,used", [(0, 1), (-5, 1), (10_000, 10_000), (10**9, 60_000)])
def test_wait_for_timeout_is_clamped(given, used):
    seen = []

    class _Page:
        async def wait_for_selector(self, selector, timeout):
            seen.append(timeout)

        async def query_selector(self, selector):
            return None

    agent = object.__new__(AbrasioBrowserAgent)
    agent._page = _Page()
    asyncio.run(agent.wait_for("#x", timeout=given))
    assert seen == [used]


def test_text_output_is_capped_with_marker():
    assert _cap_output("short") == "short"
    out = _cap_output("a" * (MAX_OUTPUT_CHARS + 7))
    assert out.startswith("a" * MAX_OUTPUT_CHARS)
    assert out.endswith("[truncated: 7 more characters]")

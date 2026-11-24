"""Input guards on AbrasioBrowserAgent: navigation targets, timeouts and output caps."""

import asyncio
import types

import pytest

from abrasio_mcp.browser import (
    ALLOW_PRIVATE_HOSTS_ENV,
    MAX_OUTPUT_CHARS,
    AbrasioBrowserAgent,
    _cap_output,
    _guard_navigation,
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
    ("http://127.0.0.1:9/api", False, False, "fallback"),   # non-navigation: untouched
    ("https://93.184.216.34/", True, False, "fallback"),
])
def test_navigation_route_guard(monkeypatch, url, navigation, opt_in, expected):
    if opt_in:
        monkeypatch.setenv(ALLOW_PRIVATE_HOSTS_ENV, "true")
    else:
        monkeypatch.delenv(ALLOW_PRIVATE_HOSTS_ENV, raising=False)
    route = _FakeRoute()
    asyncio.run(_guard_navigation(route, _FakeRequest(url, navigation)))
    assert route.calls == [expected]


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

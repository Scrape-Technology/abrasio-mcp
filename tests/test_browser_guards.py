"""Input guards on AbrasioBrowserAgent: navigation targets, timeouts and output caps."""

import asyncio

import pytest

from abrasio_mcp.browser import ALLOW_PRIVATE_HOSTS_ENV, check_navigation_target


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
    _check("https://example.com/", cloud=True)  # AI_NUMERICHOST: no DNS lookup here

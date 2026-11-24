import pytest

from abrasio_mcp import server


def test_non_loopback_http_host_refuses_to_start(monkeypatch):
    monkeypatch.setenv("ABRASIO_TRANSPORT", "streamable-http")
    monkeypatch.setenv("ABRASIO_HOST", "0.0.0.0")
    started = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: started.append(kw))

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code not in (0, None)
    assert "no authentication" in str(exc.value.code)
    assert started == []


def test_loopback_http_sets_explicit_dns_rebinding_protection(monkeypatch):
    monkeypatch.setenv("ABRASIO_TRANSPORT", "streamable-http")
    monkeypatch.setenv("ABRASIO_HOST", "127.0.0.1")
    monkeypatch.setenv("ABRASIO_PORT", "8931")
    for attr in ("host", "port", "transport_security"):  # restored after the test
        monkeypatch.setattr(server.mcp.settings, attr, getattr(server.mcp.settings, attr))
    monkeypatch.setattr(server.mcp, "run", lambda **kw: None)

    server.main()

    ts = server.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is True
    assert set(ts.allowed_hosts) == {"127.0.0.1:8931", "localhost:8931", "[::1]:8931"}
    assert "http://localhost:8931" in ts.allowed_origins
    assert not any(h.endswith(":*") for h in ts.allowed_hosts + ts.allowed_origins)

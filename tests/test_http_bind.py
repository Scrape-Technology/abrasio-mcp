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

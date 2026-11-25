"""
Tests for the `abrasio_mcp.tools.playbook` MCP tool wrappers — specifically that
`playbook_mark_extract` forwards `selector`/`script` to `RecordingSession.mark_extract`
(bug found in review, 2026-07-15: the tool's signature only exposed `schema`, so every
extract step produced through the real agent-facing tool had no selector, and every
T1/T2 replay broke immediately with reason:"response_shape" — invisible in the existing
`test_recording.py` suite, which drives `RecordingSession` directly, bypassing this
MCP-tool layer entirely).
"""

import asyncio

from abrasio_mcp.recording import RecordingSession
from abrasio_mcp.tools import playbook as playbook_tools


class _FakeMCP:
    """Captures functions registered via @mcp.tool(), mirroring FastMCP's decorator
    shape closely enough to call the wrapped tool directly in a test."""

    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register() -> dict[str, object]:
    fake_mcp = _FakeMCP()
    playbook_tools.register(fake_mcp, get_agent=None)
    return fake_mcp.tools


def test_playbook_mark_extract_forwards_selector_to_the_session(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    monkeypatch.setattr(playbook_tools, "session", RecordingSession())

    tools = _register()
    asyncio.run(tools["playbook_mark_extract"]({"odds": "number"}, selector=".odds-value"))

    step = playbook_tools.session._steps[-1]
    assert step["op"] == "extract"
    assert step["selector"] == ".odds-value"


def test_playbook_mark_extract_forwards_script_to_the_session(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    monkeypatch.setattr(playbook_tools, "session", RecordingSession())

    tools = _register()
    asyncio.run(tools["playbook_mark_extract"]({"odds": "number"}, script="() => document.title"))

    step = playbook_tools.session._steps[-1]
    assert step["script"] == "() => document.title"


def test_playbook_mark_extract_without_selector_or_script_still_works(monkeypatch):
    """No selector/script is a valid call shape (e.g. T0-only playbooks that never use
    T1/T2 extract) — must not raise, just produce a selector-less step."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    monkeypatch.setattr(playbook_tools, "session", RecordingSession())

    tools = _register()
    asyncio.run(tools["playbook_mark_extract"]({"odds": "number"}))

    step = playbook_tools.session._steps[-1]
    assert step["op"] == "extract"
    assert "selector" not in step


# ── playbook_finalize uploads itself (no file, no secret in the result) ──────

import json
import tempfile

import httpx
import pytest

API_KEY = "st-test-key-not-real"


def _recorded_session(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    monkeypatch.setattr(playbook_tools, "session", RecordingSession())
    s = playbook_tools.session
    s.register_secret("vault://x/pw", "hunter2-raw")
    s.record("navigate", url="https://book.example/")
    s.add({"op": "fill", "selector": "#pw", "secret_ref": "vault://x/pw"})
    return s


def _mock_api(monkeypatch, status=200, body=None):
    """Route the tool's httpx client through a MockTransport; returns the seen requests
    and the kwargs the client was built with."""
    seen, client_kw = [], {}
    body = body if body is not None else {"success": True, "playbook": {
        "id": "pb-1", "group_id": "g-1", "name": "x", "version": 1, "health": "healthy",
        "steps": [], "has_secrets": True}}

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient

    def _client(**kw):
        client_kw.update(kw)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(playbook_tools.httpx, "AsyncClient", _client)
    monkeypatch.setattr(tempfile, "mkstemp", lambda *a, **k: pytest.fail("no file may be written"))
    return seen, client_kw


def test_playbook_finalize_uploads_and_returns_no_secret(monkeypatch):
    s = _recorded_session(monkeypatch)
    monkeypatch.setenv("SCRAPETECH_API_KEY", API_KEY)
    monkeypatch.delenv("SCRAPETECH_API_URL", raising=False)
    seen, client_kw = _mock_api(monkeypatch)

    result = asyncio.run(_register()["playbook_finalize"]("x", "book.example"))

    assert client_kw["follow_redirects"] is False and client_kw["timeout"]
    (req,) = seen
    assert str(req.url) == "https://api.scrapetechnology.com/api/playbooks"
    assert req.headers["X-API-KEY"] == API_KEY
    sent = json.loads(req.content)
    assert sent["secrets"] == {"vault://x/pw": "hunter2-raw"}
    assert sent["domain"] == "book.example" and len(sent["steps"]) == 2

    assert "hunter2-raw" not in result and API_KEY not in result
    out = json.loads(result)
    assert out["id"] == "pb-1" and out["group_id"] == "g-1" and out["steps"] == 2
    assert out["secret_refs"] == ["vault://x/pw"]
    # The recording was reset: a second recording starts empty, without old secrets.
    assert s._steps == [] and s._secrets == {}


def test_playbook_finalize_requires_the_api_key(monkeypatch):
    s = _recorded_session(monkeypatch)
    monkeypatch.delenv("SCRAPETECH_API_KEY", raising=False)
    seen, _ = _mock_api(monkeypatch)
    with pytest.raises(ValueError, match="SCRAPETECH_API_KEY"):
        asyncio.run(_register()["playbook_finalize"]("x", "book.example"))
    assert seen == [] and len(s._steps) == 2  # nothing sent, recording kept


@pytest.mark.parametrize("base", ["http://api.scrapetechnology.com", "ftp://x.example", "http://10.0.0.5:8000"])
def test_playbook_finalize_refuses_non_https_api(monkeypatch, base):
    _recorded_session(monkeypatch)
    monkeypatch.setenv("SCRAPETECH_API_KEY", API_KEY)
    monkeypatch.setenv("SCRAPETECH_API_URL", base)
    seen, _ = _mock_api(monkeypatch)
    with pytest.raises(ValueError, match="SCRAPETECH_API_URL"):
        asyncio.run(_register()["playbook_finalize"]("x", "book.example"))
    assert seen == []


def test_playbook_finalize_allows_local_http_api(monkeypatch):
    _recorded_session(monkeypatch)
    monkeypatch.setenv("SCRAPETECH_API_KEY", API_KEY)
    monkeypatch.setenv("SCRAPETECH_API_URL", "http://127.0.0.1:8000/")
    seen, _ = _mock_api(monkeypatch)
    asyncio.run(_register()["playbook_finalize"]("x", "book.example"))
    assert str(seen[0].url) == "http://127.0.0.1:8000/api/playbooks"


@pytest.mark.parametrize("status,body", [
    (422, {"detail": [{"input": {"secrets": {"vault://x/pw": "hunter2-raw"}}}]}),  # echoes the body
    (400, {"detail": "bad hunter2-raw"}),
    (307, {}),  # a redirect is not followed
])
def test_playbook_finalize_error_never_echoes_secrets(monkeypatch, status, body):
    s = _recorded_session(monkeypatch)
    monkeypatch.setenv("SCRAPETECH_API_KEY", API_KEY)
    seen, _ = _mock_api(monkeypatch, status=status, body=body)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_register()["playbook_finalize"]("x", "book.example"))
    assert f"HTTP {status}" in str(exc.value)
    assert "hunter2-raw" not in str(exc.value) and API_KEY not in str(exc.value)
    assert len(seen) == 1 and len(s._steps) == 2  # recording kept for a retry
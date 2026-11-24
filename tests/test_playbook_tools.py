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

    step = playbook_tools.session.finalize("x", "book.example")["steps"][-1]
    assert step["op"] == "extract"
    assert step["selector"] == ".odds-value"


def test_playbook_mark_extract_forwards_script_to_the_session(monkeypatch):
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    monkeypatch.setattr(playbook_tools, "session", RecordingSession())

    tools = _register()
    asyncio.run(tools["playbook_mark_extract"]({"odds": "number"}, script="() => document.title"))

    step = playbook_tools.session.finalize("x", "book.example")["steps"][-1]
    assert step["script"] == "() => document.title"


def test_playbook_mark_extract_without_selector_or_script_still_works(monkeypatch):
    """No selector/script is a valid call shape (e.g. T0-only playbooks that never use
    T1/T2 extract) — must not raise, just produce a selector-less step."""
    monkeypatch.setenv("ABRASIO_RECORD", "true")
    monkeypatch.setattr(playbook_tools, "session", RecordingSession())

    tools = _register()
    asyncio.run(tools["playbook_mark_extract"]({"odds": "number"}))

    step = playbook_tools.session.finalize("x", "book.example")["steps"][-1]
    assert step["op"] == "extract"
    assert "selector" not in step


def test_playbook_finalize_writes_secrets_to_a_private_file_not_the_result(monkeypatch, tmp_path):
    """Audit 2026-11-09: the tool used to return `secrets` in plaintext to the model."""
    import json
    import os
    import tempfile

    monkeypatch.setenv("ABRASIO_RECORD", "true")
    monkeypatch.setattr(playbook_tools, "session", RecordingSession())
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    playbook_tools.session.register_secret("vault://x/pw", "hunter2-raw")
    playbook_tools.session.record("navigate", url="https://book.example/")

    result = asyncio.run(_register()["playbook_finalize"]("x", "book.example"))

    assert "hunter2-raw" not in result
    out = json.loads(result)
    assert out["secret_refs"] == ["vault://x/pw"]
    assert "secrets" not in out["playbook"]
    with open(out["playbook_file"], encoding="utf-8") as f:
        body = json.load(f)  # exactly the POST /api/playbooks body
    assert body["secrets"] == {"vault://x/pw": "hunter2-raw"}
    assert body["steps"] == out["playbook"]["steps"]
    if os.name == "posix":
        assert os.stat(out["playbook_file"]).st_mode & 0o777 == 0o600

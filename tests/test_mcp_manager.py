"""Tests for McpManager transport handling — auth headers + streamable-http.

No network: the mcp client functions (sse_client / streamablehttp_client /
ClientSession) are monkeypatched with fakes that capture call args and yield a
canned tool list. Verifies headers are plumbed through and the right transport
is selected. Also covers the headers-column migration idempotency.
"""

import pytest

from src.mcp_manager import McpManager


# --- fakes --------------------------------------------------------------------

class _FakeACM:
    """Async context manager yielding a preset value."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = "desc"
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeToolsResult:
    def __init__(self, tools):
        self.tools = tools


class _FakeSession:
    def __init__(self, tools):
        self._tools = tools

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeToolsResult(self._tools)


def _patch_session(monkeypatch, tools=None):
    tools = tools or [_FakeTool("alpha"), _FakeTool("beta")]
    monkeypatch.setattr(
        "mcp.ClientSession", lambda r, w: _FakeACM(_FakeSession(tools)), raising=True
    )


# --- SSE ----------------------------------------------------------------------

async def test_sse_passes_headers(monkeypatch):
    captured = {}

    def fake_sse(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeACM(("read", "write"))

    monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse, raising=True)
    _patch_session(monkeypatch)

    mgr = McpManager()
    ok = await mgr.connect_server(
        "s1", "S1", "sse", url="https://x/mcp", headers={"Authorization": "Bearer z"}
    )
    assert ok is True
    assert captured["url"] == "https://x/mcp"
    assert captured["headers"] == {"Authorization": "Bearer z"}
    assert mgr.get_server_status("s1")["tool_count"] == 2


async def test_sse_without_headers_omits_kwarg(monkeypatch):
    seen = {}

    def fake_sse(url, headers="UNSET"):
        seen["headers"] = headers  # default sentinel proves kwarg not passed
        return _FakeACM(("read", "write"))

    monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse, raising=True)
    _patch_session(monkeypatch)

    mgr = McpManager()
    ok = await mgr.connect_server("s2", "S2", "sse", url="https://x")
    assert ok is True
    assert seen["headers"] == "UNSET"  # plain sse_client(url) path taken


# --- streamable-http ----------------------------------------------------------

async def test_http_transport_selected_and_headers(monkeypatch):
    captured = {}

    def fake_http(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        # streamable-http yields a 3-tuple
        return _FakeACM(("read", "write", lambda: "session-id"))

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamablehttp_client", fake_http, raising=True
    )
    _patch_session(monkeypatch, tools=[_FakeTool("only")])

    mgr = McpManager()
    ok = await mgr.connect_server(
        "h1", "H1", "http", url="https://api/mcp", headers={"X-Key": "abc"}
    )
    assert ok is True
    assert captured["url"] == "https://api/mcp"
    assert captured["headers"] == {"X-Key": "abc"}
    assert mgr.get_server_status("h1")["transport"] == "http"
    assert mgr.get_server_status("h1")["tool_count"] == 1


async def test_streamable_http_alias(monkeypatch):
    called = {}

    def fake_http(url, headers=None):
        called["hit"] = True
        return _FakeACM(("r", "w", None))

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamablehttp_client", fake_http, raising=True
    )
    _patch_session(monkeypatch)

    mgr = McpManager()
    ok = await mgr.connect_server("h2", "H2", "streamable-http", url="https://api")
    assert ok is True
    assert called.get("hit") is True


# --- transport validation -----------------------------------------------------

async def test_unknown_transport_returns_false():
    mgr = McpManager()
    ok = await mgr.connect_server("bad", "Bad", "carrier-pigeon", url="https://x")
    assert ok is False
    assert mgr.get_server_status("bad")["status"] in ("disconnected", "error")


# --- migration ----------------------------------------------------------------

def test_mcp_headers_migration_idempotent(tmp_path):
    """_migrate_add_mcp_headers_column adds the column once and is safe to re-run.

    Runs in a clean subprocess: sibling test modules replace `core.database` in
    sys.modules with a stub (and don't restore it), so an in-process import here
    would pick up the stub. A subprocess gives us the real module deterministically.
    """
    import os
    import subprocess
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = tmp_path / "mig.db"
    code = (
        "import sys\n"
        "from sqlalchemy import create_engine, text\n"
        "import core.database as db\n"
        "eng = create_engine('sqlite:///' + sys.argv[1])\n"
        "with eng.connect() as c:\n"
        "    c.execute(text('CREATE TABLE mcp_servers (id TEXT PRIMARY KEY, name TEXT)')); c.commit()\n"
        "db.engine = eng\n"
        "db._migrate_add_mcp_headers_column()\n"
        "db._migrate_add_mcp_headers_column()\n"  # idempotent — must not raise
        "with eng.connect() as c:\n"
        "    cols = [row[1] for row in c.execute(text('PRAGMA table_info(mcp_servers)'))]\n"
        "assert cols.count('headers') == 1, cols\n"
        "print('MIGRATION_OK')\n"
    )
    env = dict(os.environ, PYTHONPATH=repo_root)
    proc = subprocess.run(
        [sys.executable, "-c", code, str(db_path)],
        cwd=repo_root, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "MIGRATION_OK" in proc.stdout

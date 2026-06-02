"""Tests for the local STUB Vanta service + provider round-trip + fail-open.

Starts the stub on an ephemeral 127.0.0.1 port in a daemon thread and drives the
REAL VantaBrainProvider against it (local loopback only — never a real service).
"""
import sys
import json
import hashlib
import pathlib
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

httpx = pytest.importorskip("httpx")

from tools.vanta_stub.stub_service import make_server, StubStore
from src.memory_providers.vanta_provider import VantaBrainProvider, VantaProviderError
from src.memory_providers.base import FallbackProvider

FAKE_KEY = "stub-fake-key-123"


@pytest.fixture()
def stub():
    httpd, store, log = make_server("127.0.0.1", 0, FAKE_KEY)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"base_url": f"http://127.0.0.1:{port}", "store": store, "log": log, "httpd": httpd}
    httpd.shutdown()


def _provider(stub, key=FAKE_KEY, timeout_ms=800):
    return VantaBrainProvider(stub["base_url"], key, timeout_ms=timeout_ms)


# --------------------------------------------------------------------------- #
def test_stub_binds_loopback_only():
    with pytest.raises(ValueError):
        make_server("0.0.0.0", 0, FAKE_KEY)


def test_stub_health_works(stub):
    assert _provider(stub).healthy is True


def test_stub_rejects_wrong_key(stub):
    assert _provider(stub, key="WRONG").healthy is False


def test_upsert_and_count(stub):
    p = _provider(stub)
    p.add("m1", "alpha bravo", owner="growthgod")
    p.add("m2", "charlie delta", owner="growthgod")
    assert p.count() == 2
    assert stub["store"].count() == 2


def test_search_returns_memory_id_score_shape(stub):
    p = _provider(stub)
    p.add("m1", "the agent is named Sc3pt3R", owner="growthgod")
    p.add("m2", "unrelated weather note", owner="growthgod")
    res = p.search("agent named", k=5, owner="growthgod")
    assert isinstance(res, list) and res
    assert all(set(r) >= {"memory_id", "score"} and isinstance(r["score"], float) for r in res)
    assert res[0]["memory_id"] == "m1"  # best keyword overlap


def test_remove(stub):
    p = _provider(stub)
    p.add("m1", "alpha", owner="growthgod")
    p.remove("m1")
    assert p.count() == 0


def test_rebuild_replaces_index(stub):
    p = _provider(stub)
    p.add("old", "stale", owner="growthgod")
    p.rebuild([{"id": "n1", "text": "fresh one", "owner": "growthgod"},
               {"id": "n2", "text": "fresh two", "owner": "growthgod"}])
    assert p.count() == 2
    assert "old" not in stub["store"].items


def test_killed_stub_does_not_break_provider(stub):
    p = _provider(stub, timeout_ms=300)
    assert p.healthy is True
    stub["httpd"].shutdown(); stub["httpd"].server_close()  # kill it
    # provider raises (connection refused), never hangs
    with pytest.raises(VantaProviderError):
        p.search("q")


def test_fallback_to_local_when_stub_dead(stub):
    class FakeLocal:
        healthy = True
        def search(self, q, k=8, *, owner=None):
            return [{"memory_id": "LOCAL", "score": 1.0}]
        def add(self, *a, **k): ...
        def remove(self, *a, **k): ...
        def find_similar(self, *a, **k): return "LOCAL"
        def rebuild(self, *a, **k): ...
        def count(self): return 0

    fp = FallbackProvider(_provider(stub, timeout_ms=300), FakeLocal())
    stub["httpd"].shutdown(); stub["httpd"].server_close()
    assert fp.search("q") == [{"memory_id": "LOCAL", "score": 1.0}]  # fail open, no raise


# --------------------------------------------------------------------------- #
# invariants — provider never touches memory.json; Memory Tidy paused
# --------------------------------------------------------------------------- #
def test_provider_ops_do_not_touch_memory_json(stub):
    mem = ROOT / "data" / "memory.json"
    if not mem.exists():
        pytest.skip("no data/memory.json in this checkout")
    before = hashlib.sha256(mem.read_bytes()).hexdigest()
    p = _provider(stub)
    p.add("x", "indexed only, not stored", owner="growthgod")
    p.search("indexed", owner="growthgod")
    p.rebuild([{"id": "y", "text": "z", "owner": "growthgod"}])
    after = hashlib.sha256(mem.read_bytes()).hexdigest()
    assert before == after, "Vanta provider must never write memory.json"


def test_memory_tidy_remains_paused():
    import sqlite3
    db = ROOT / "data" / "app.db"
    if not db.exists():
        pytest.skip("no data/app.db in this checkout")
    c = sqlite3.connect(str(db))
    row = c.execute("SELECT status FROM scheduled_tasks WHERE action='consolidate_memory'").fetchone()
    c.close()
    if row is None:
        pytest.skip("consolidate_memory task not present")
    assert row[0] == "paused"

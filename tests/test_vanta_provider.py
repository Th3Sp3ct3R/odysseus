"""P2 coverage for VantaBrainProvider — stubbed transport ONLY (no real Vanta).

Uses httpx.MockTransport so no socket is ever opened. Verifies endpoint mapping,
search normalization, the full fail-open matrix, INDEX_ON_BULK, factory wiring,
and that the API key is never logged.
"""
import json
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

httpx = pytest.importorskip("httpx")

from src.memory_providers.vanta_provider import VantaBrainProvider, VantaProviderError
from src.memory_providers.base import FallbackProvider
from src.memory_providers.local_provider import LocalMemoryProvider
from src.memory_providers import factory

SECRET_KEY = "sk-test-SECRET-DO-NOT-LOG-123456"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def make_provider(handler, *, index_on_bulk=False, timeout_ms=800, api_key=SECRET_KEY):
    client = httpx.Client(base_url="http://vanta.test", transport=httpx.MockTransport(handler))
    return VantaBrainProvider("http://vanta.test", api_key, timeout_ms=timeout_ms,
                              index_on_bulk=index_on_bulk, client=client)


def recording_handler(seen, responder):
    def h(request):
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = None
        seen.append({"method": request.method, "path": request.url.path, "body": body,
                     "auth": request.headers.get("Authorization")})
        return responder(request)
    return h


def healthy_responder(request):
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok", "index_ready": True})
    return httpx.Response(200, json={"ok": True})


class FakeLocal:
    """Local fallback stand-in that returns a marker result."""
    name = "local"
    healthy = True

    def __init__(self):
        self.calls = []

    def add(self, memory_id, text, *, owner=None, bulk=False, **meta):
        self.calls.append(("add", memory_id))

    def remove(self, memory_id):
        self.calls.append(("remove", memory_id))

    def search(self, query, k=8, *, owner=None):
        return [{"memory_id": "LOCAL", "score": 1.0}]

    def find_similar(self, text, threshold=0.92):
        return "LOCAL"

    def rebuild(self, memories):
        self.calls.append(("rebuild", len(memories)))

    def count(self):
        return 999


# --------------------------------------------------------------------------- #
# endpoint mapping
# --------------------------------------------------------------------------- #
def test_healthy_maps_to_get_health():
    seen = []
    p = make_provider(recording_handler(seen, healthy_responder))
    assert p.healthy is True
    assert ("GET", "/health") in [(s["method"], s["path"]) for s in seen]


def test_healthy_false_when_index_not_ready():
    def r(request):
        return httpx.Response(200, json={"status": "ok", "index_ready": False})
    assert make_provider(r).healthy is False


def test_add_maps_to_upsert():
    seen = []

    def r(request):
        if request.url.path == "/memories/upsert":
            return httpx.Response(200, json={"upserted": 1, "failed": []})
        return healthy_responder(request)

    p = make_provider(recording_handler(seen, r))
    p.add("id-1", "hello world", owner="growthgod", source="obsidian:x.md", category="fact")
    up = [s for s in seen if s["path"] == "/memories/upsert"][0]
    assert up["method"] == "POST"
    assert up["body"]["memories"][0]["memory_id"] == "id-1"
    assert up["body"]["memories"][0]["owner"] == "growthgod"


def test_search_maps_to_search_and_normalizes():
    def r(request):
        if request.url.path == "/memories/search":
            return httpx.Response(200, json=[
                {"memory_id": "a", "score": 0.95},
                {"memory_id": "b", "score": "0.5"},   # string score coerced
                {"memory_id": 123, "score": 0.4},     # non-str id dropped
                {"score": 0.3},                        # missing id dropped
                {"memory_id": "c", "score": "nope"},  # bad score dropped
                "garbage",                              # non-dict dropped
            ])
        return healthy_responder(request)

    out = make_provider(r).search("q", k=5, owner="growthgod")
    assert out == [{"memory_id": "a", "score": 0.95}, {"memory_id": "b", "score": 0.5}]


def test_remove_maps_to_delete():
    seen = []

    def r(request):
        return httpx.Response(200, json={"deleted": True})

    make_provider(recording_handler(seen, r)).remove("abc-123")
    assert seen[0]["method"] == "DELETE"
    assert seen[0]["path"] == "/memories/abc-123"


def test_rebuild_maps_to_rebuild():
    seen = []

    def r(request):
        return httpx.Response(200, json={"indexed": 2, "removed_stale": 0})

    p = make_provider(recording_handler(seen, r))
    p.rebuild([{"id": "m1", "text": "a", "owner": "growthgod"}, {"id": "m2", "text": "b"}])
    rb = [s for s in seen if s["path"] == "/memories/rebuild"][0]
    assert rb["method"] == "POST"
    ids = [m["memory_id"] for m in rb["body"]["memories"]]
    assert ids == ["m1", "m2"]


def test_count_maps_to_count():
    def r(request):
        return httpx.Response(200, json={"count": 180})
    assert make_provider(r).count() == 180


# --------------------------------------------------------------------------- #
# fail-open matrix (provider raises; FallbackProvider routes to local)
# --------------------------------------------------------------------------- #
def test_malformed_search_raises_then_fails_open_via_fallback():
    def r(request):
        if request.url.path == "/memories/search":
            return httpx.Response(200, json={"not": "an array"})
        return healthy_responder(request)

    vanta = make_provider(r)
    with pytest.raises(VantaProviderError):
        vanta.search("q")
    # Wrapped: FallbackProvider returns the local result instead of raising.
    fp = FallbackProvider(make_provider(r), FakeLocal())
    assert fp.search("q") == [{"memory_id": "LOCAL", "score": 1.0}]


def test_timeout_fails_open():
    def r(request):
        if request.url.path == "/memories/search":
            raise httpx.TimeoutException("slow")
        return healthy_responder(request)

    with pytest.raises(VantaProviderError):
        make_provider(r).search("q")
    fp = FallbackProvider(make_provider(r), FakeLocal())
    assert fp.search("q")[0]["memory_id"] == "LOCAL"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_error_statuses_fail_open(status):
    def r(request):
        if request.url.path == "/memories/search":
            return httpx.Response(status, json={"error": "x"})
        return healthy_responder(request)

    with pytest.raises(VantaProviderError):
        make_provider(r).search("q")
    fp = FallbackProvider(make_provider(r), FakeLocal())
    assert fp.search("q")[0]["memory_id"] == "LOCAL"


def test_add_failure_does_not_block_via_fallback():
    def r(request):
        if request.url.path == "/memories/upsert":
            return httpx.Response(500, json={"error": "boom"})
        return healthy_responder(request)

    local = FakeLocal()
    fp = FallbackProvider(make_provider(r), local)
    fp.add("id-1", "text", owner="growthgod")  # must not raise
    assert ("add", "id-1") in local.calls       # fallback still indexed


# --------------------------------------------------------------------------- #
# INDEX_ON_BULK
# --------------------------------------------------------------------------- #
def test_bulk_add_skipped_when_index_on_bulk_false():
    seen = []
    p = make_provider(recording_handler(seen, healthy_responder), index_on_bulk=False)
    p.add("id-1", "text", owner="growthgod", bulk=True)
    assert not [s for s in seen if s["path"] == "/memories/upsert"]  # no upsert call


def test_bulk_add_indexed_when_index_on_bulk_true():
    seen = []

    def r(request):
        if request.url.path == "/memories/upsert":
            return httpx.Response(200, json={"upserted": 1, "failed": []})
        return healthy_responder(request)

    p = make_provider(recording_handler(seen, r), index_on_bulk=True)
    p.add("id-1", "text", owner="growthgod", bulk=True)
    assert [s for s in seen if s["path"] == "/memories/upsert"]


# --------------------------------------------------------------------------- #
# factory wiring
# --------------------------------------------------------------------------- #
def _patch_local(monkeypatch):
    class _Store:
        healthy = False
        def add(self, *a, **k): ...
        def search(self, *a, **k): return []
        def count(self): return 0
        def rebuild(self, *a, **k): ...
        def remove(self, *a, **k): ...
        def find_similar(self, *a, **k): return None
    monkeypatch.setattr("src.memory_vector.MemoryVectorStore",
                        lambda data_dir, embedding_model=None: _Store())


def test_factory_disabled_returns_local(monkeypatch, tmp_path):
    monkeypatch.delenv("VANTA_BRAIN_ENABLED", raising=False)
    _patch_local(monkeypatch)
    assert isinstance(factory.build_memory_index(str(tmp_path)), LocalMemoryProvider)


def test_factory_enabled_missing_env_falls_back_to_local(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("VANTA_BRAIN_ENABLED", "true")
    monkeypatch.delenv("VANTA_BRAIN_BASE_URL", raising=False)
    monkeypatch.delenv("VANTA_BRAIN_API_KEY", raising=False)
    _patch_local(monkeypatch)
    with caplog.at_level("WARNING"):
        prov = factory.build_memory_index(str(tmp_path))
    assert isinstance(prov, LocalMemoryProvider)
    assert any("missing" in r.message.lower() for r in caplog.records)


def test_factory_enabled_with_env_builds_vanta_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("VANTA_BRAIN_ENABLED", "true")
    monkeypatch.setenv("VANTA_BRAIN_PROVIDER", "vanta")
    monkeypatch.setenv("VANTA_BRAIN_BASE_URL", "http://vanta.test")
    monkeypatch.setenv("VANTA_BRAIN_API_KEY", SECRET_KEY)
    _patch_local(monkeypatch)
    prov = factory.build_memory_index(str(tmp_path))  # no network (lazy)
    assert isinstance(prov, FallbackProvider)
    assert isinstance(prov._primary, VantaBrainProvider)
    assert isinstance(prov._fallback, LocalMemoryProvider)


# --------------------------------------------------------------------------- #
# security: API key / Authorization never logged
# --------------------------------------------------------------------------- #
def test_api_key_never_logged(caplog):
    def r(request):
        if request.url.path == "/memories/upsert":
            return httpx.Response(200, json={"upserted": 1, "failed": []})
        if request.url.path == "/memories/search":
            return httpx.Response(401, json={"error": "nope"})  # forces a WARNING log
        return healthy_responder(request)

    p = make_provider(r)
    with caplog.at_level("DEBUG"):
        assert p.healthy is True
        p.add("id-1", "secret-bearing text", owner="growthgod")
        with pytest.raises(VantaProviderError):
            p.search("q")
    blob = "\n".join(f"{rec.getMessage()} {rec.args}" for rec in caplog.records)
    assert SECRET_KEY not in blob
    assert "Bearer " + SECRET_KEY not in blob

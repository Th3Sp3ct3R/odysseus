"""P1 coverage for the memory index provider seam (design-only behavior change:
none). Verifies the local provider is the default, is a transparent pass-through,
and that FallbackProvider degrades correctly. No ChromaDB, no network, no heavy
deps — the underlying store is faked.

See docs/design/vanta-brain-adapter.md.
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.memory_providers.local_provider import LocalMemoryProvider
from src.memory_providers.base import FallbackProvider, MemoryIndexProvider
from src.memory_providers import factory


class FakeStore:
    """Minimal MemoryVectorStore stand-in recording calls."""

    def __init__(self, healthy=True, search_result=None, raise_on=None):
        self._healthy = healthy
        self._search_result = search_result if search_result is not None else []
        self._raise_on = raise_on or set()
        self.calls = []

    @property
    def healthy(self):
        return self._healthy

    def _maybe_raise(self, name):
        if name in self._raise_on:
            raise RuntimeError(f"{name} boom")

    def add(self, memory_id, text):
        self.calls.append(("add", memory_id, text))
        self._maybe_raise("add")

    def remove(self, memory_id):
        self.calls.append(("remove", memory_id))

    def search(self, query, k=8):
        self.calls.append(("search", query, k))
        self._maybe_raise("search")
        return self._search_result

    def find_similar(self, text, threshold=0.92):
        self.calls.append(("find_similar", text, threshold))
        return None

    def rebuild(self, memories):
        self.calls.append(("rebuild", len(memories)))

    def count(self):
        self.calls.append(("count",))
        return 7

    # An incidental attribute the wrapper must forward transparently.
    sentinel = "passthrough-ok"


# --------------------------------------------------------------------------- #
# LocalMemoryProvider — transparent pass-through
# --------------------------------------------------------------------------- #
def test_local_provider_satisfies_protocol():
    p = LocalMemoryProvider(FakeStore())
    assert isinstance(p, MemoryIndexProvider)
    assert p.name == "local"


def test_local_provider_delegates_every_method():
    store = FakeStore(search_result=[{"memory_id": "a", "score": 0.5}])
    p = LocalMemoryProvider(store)
    assert p.healthy is True
    p.add("id1", "hello", owner="growthgod")   # owner accepted + ignored for parity
    p.remove("id1")
    assert p.search("q", 3) == [{"memory_id": "a", "score": 0.5}]
    assert p.find_similar("x", 0.9) is None
    p.rebuild([{"id": "a"}, {"id": "b"}])
    assert p.count() == 7
    assert store.calls == [
        ("add", "id1", "hello"),
        ("remove", "id1"),
        ("search", "q", 3),
        ("find_similar", "x", 0.9),
        ("rebuild", 2),
        ("count",),
    ]


def test_local_provider_forwards_unknown_attributes():
    p = LocalMemoryProvider(FakeStore())
    assert p.sentinel == "passthrough-ok"  # __getattr__ passthrough


def test_local_provider_healthy_reflects_store():
    assert LocalMemoryProvider(FakeStore(healthy=False)).healthy is False


# --------------------------------------------------------------------------- #
# Factory — local by default; enabled-without-Vanta still local
# --------------------------------------------------------------------------- #
def _patch_store(monkeypatch, store):
    monkeypatch.setattr("src.memory_vector.MemoryVectorStore",
                        lambda data_dir, embedding_model=None: store)


def test_factory_default_returns_local(monkeypatch, tmp_path):
    monkeypatch.delenv("VANTA_BRAIN_ENABLED", raising=False)
    store = FakeStore()
    _patch_store(monkeypatch, store)
    prov = factory.build_memory_index(str(tmp_path))
    assert isinstance(prov, LocalMemoryProvider)
    assert prov.name == "local"
    assert prov.healthy is True


def test_factory_enabled_without_vanta_falls_back_to_local(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("VANTA_BRAIN_ENABLED", "true")
    monkeypatch.setenv("VANTA_BRAIN_PROVIDER", "vanta")
    _patch_store(monkeypatch, FakeStore())
    with caplog.at_level("WARNING"):
        prov = factory.build_memory_index(str(tmp_path))
    assert isinstance(prov, LocalMemoryProvider)
    assert any("not implemented yet" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# FallbackProvider — degrade correctly (seam P2 will use)
# --------------------------------------------------------------------------- #
def test_fallback_uses_primary_when_healthy():
    primary = FakeStore(healthy=True, search_result=[{"memory_id": "P", "score": 1.0}])
    fallback = FakeStore(healthy=True, search_result=[{"memory_id": "F", "score": 1.0}])
    fp = FallbackProvider(LocalMemoryProvider(primary), LocalMemoryProvider(fallback))
    assert fp.search("q")[0]["memory_id"] == "P"


def test_fallback_uses_fallback_when_primary_unhealthy():
    primary = FakeStore(healthy=False)
    fallback = FakeStore(healthy=True, search_result=[{"memory_id": "F", "score": 1.0}])
    fp = FallbackProvider(LocalMemoryProvider(primary), LocalMemoryProvider(fallback))
    assert fp.search("q")[0]["memory_id"] == "F"
    assert ("search", "q", 8) not in primary.calls  # primary not queried


def test_fallback_search_fails_open_on_primary_exception():
    primary = FakeStore(healthy=True, raise_on={"search"})
    fallback = FakeStore(healthy=True, search_result=[{"memory_id": "F", "score": 1.0}])
    fp = FallbackProvider(LocalMemoryProvider(primary), LocalMemoryProvider(fallback))
    # Must not raise; returns fallback result.
    assert fp.search("q")[0]["memory_id"] == "F"


def test_fallback_write_tolerates_primary_failure():
    primary = FakeStore(healthy=True, raise_on={"add"})
    fallback = FakeStore(healthy=True)
    fp = FallbackProvider(LocalMemoryProvider(primary), LocalMemoryProvider(fallback))
    fp.add("id1", "text")  # must not raise despite primary.add boom
    assert ("add", "id1", "text") in fallback.calls  # fallback still written

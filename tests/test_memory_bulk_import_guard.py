"""Regression coverage for the bulk-import guard on /api/memory/add.

Context: a deterministic bulk import (the Obsidian->Odysseus memory sync) once
fired the `memory_added` event for every entry, which tripped the event-driven
"Memory Tidy" (consolidate_memory) task and LLM-collapsed the freshly imported
facts (180 entries -> 3). The fix: callers set `X-Odysseus-Bulk-Import` to
suppress the `memory_added` event. These tests pin that behavior so it can't
regress.

`consolidate_memory` is triggered purely by the `memory_added` event
(scheduled_tasks: trigger_type=event, trigger_event=memory_added). So
"no memory_added fired" is exactly equivalent to "Memory Tidy not triggered" —
the tests assert on the event, which is the trigger.

These mount only the memory router with a real (temp-dir) MemoryManager and a
spy on `src.event_bus.fire_event`; no full app boot, no DB, no network.
"""
import os
import sys
import pathlib
from unittest.mock import MagicMock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# These tests need the real FastAPI/TestClient + pydantic, so skip cleanly if
# the optional deps aren't installed in the runner (conftest MagicMock-stubs
# them when absent, which TestClient can't use).
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("pydantic")
try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    pytest.skip("fastapi.testclient unavailable", allow_module_level=True)

from fastapi import FastAPI, Request
from services.memory import MemoryManager
from routes.memory_routes import setup_memory_routes


def _make_client(tmp_path):
    """A minimal app with just the memory router and a fixed current_user."""
    app = FastAPI()

    @app.middleware("http")
    async def _set_user(request: Request, call_next):
        request.state.current_user = "tester"
        return await call_next(request)

    mm = MemoryManager(str(tmp_path))
    sm = MagicMock()
    app.include_router(setup_memory_routes(mm, sm, memory_vector=None))
    return TestClient(app), mm


def _add(client, text, *, bulk=False, category="fact", source="test"):
    headers = {"X-Odysseus-Bulk-Import": "1"} if bulk else {}
    return client.post("/api/memory/add",
                       json={"text": text, "category": category, "source": source},
                       headers=headers)


def test_bulk_header_suppresses_memory_added_event(tmp_path):
    client, mm = _make_client(tmp_path)
    with patch("src.event_bus.fire_event") as spy:
        r = _add(client, "Bulk imported fact about the fleet", bulk=True)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert spy.call_count == 0, "bulk import must NOT fire memory_added"
    assert len(mm.load(owner="tester")) == 1  # still persisted


def test_normal_single_add_fires_event(tmp_path):
    client, mm = _make_client(tmp_path)
    with patch("src.event_bus.fire_event") as spy:
        r = _add(client, "A normal interactive memory", bulk=False)
    assert r.status_code == 200 and r.json()["ok"] is True
    spy.assert_called_once()
    assert spy.call_args[0][0] == "memory_added"
    assert len(mm.load(owner="tester")) == 1


def test_100_bulk_adds_never_trigger_memory_tidy(tmp_path):
    client, mm = _make_client(tmp_path)
    with patch("src.event_bus.fire_event") as spy:
        for i in range(120):
            r = _add(client, f"Bulk fact number {i} — distinct content", bulk=True)
            assert r.status_code == 200
    # consolidate_memory is event-driven on memory_added; zero events == zero
    # chance of Memory Tidy firing during a 100+ entry import.
    assert spy.call_count == 0
    assert len(mm.load(owner="tester")) == 120


def test_recommit_is_idempotent_zero_duplicates(tmp_path):
    client, mm = _make_client(tmp_path)
    texts = [f"Idempotent fact {i}" for i in range(10)]
    for t in texts:
        assert _add(client, t, bulk=True).status_code == 200
    assert len(mm.load(owner="tester")) == 10
    # Re-commit identical set: server-side exact-match dedup must add nothing.
    for t in texts:
        r = _add(client, t, bulk=True)
        assert r.status_code == 200
        assert r.json().get("message") == "Memory already exists"
    assert len(mm.load(owner="tester")) == 10, "re-commit must not duplicate"

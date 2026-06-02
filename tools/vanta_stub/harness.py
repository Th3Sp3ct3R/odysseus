#!/usr/bin/env python3
"""P3 Step 1 dry-run harness — fake Vanta enablement against the local stub.

Proves the enablement flow end-to-end WITHOUT a real Vanta service and WITHOUT
mutating data/memory.json:

  Phase 1  direct provider round-trip vs the stub (health/upsert/search/count)
  Phase 2  boot Odysseus with VANTA_BRAIN_* pointed at the stub; confirm the
           factory selects Vanta via FallbackProvider and the stub is contacted
  Phase 3  kill the stub; confirm fail-open to local (provider + live app stays up)

Guardrails: stub is loopback-only with a FAKE key; no real service; the app's
startup only READS memory.json (rebuild push to the stub), never writes it; this
harness performs no API memory writes.

Run:  .venv/bin/python tools/vanta_stub/harness.py
Exits 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import os
import sys
import json
import time
import hashlib
import sqlite3
import subprocess
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from tools.vanta_stub.stub_service import make_server  # noqa: E402
from src.memory_providers.vanta_provider import VantaBrainProvider  # noqa: E402
from src.memory_providers.base import FallbackProvider  # noqa: E402
from src.memory_providers.local_provider import LocalMemoryProvider  # noqa: E402

FAKE_KEY = "vanta-stub-fake-key-DO-NOT-USE-REAL"
APP_PORT = 7011  # distinct from the usual 7001 to avoid clashes
MEMORY_JSON = ROOT / "data" / "memory.json"
APP_DB = ROOT / "data" / "app.db"

results: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _file_fingerprint(p: Path):
    if not p.exists():
        return (0, "")
    data = p.read_bytes()
    return (len(json.loads(data)) if p.suffix == ".json" else len(data),
            hashlib.sha256(data).hexdigest())


def _http(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


class _FakeLocal:
    name = "local"
    healthy = True

    def search(self, query, k=8, *, owner=None):
        return [{"memory_id": "LOCAL-KEYWORD", "score": 1.0}]

    def add(self, *a, **k): ...
    def remove(self, *a, **k): ...
    def find_similar(self, *a, **k): return "LOCAL-KEYWORD"
    def rebuild(self, *a, **k): ...
    def count(self): return 0


def main() -> int:
    print("=" * 68)
    print("  Vanta STUB harness — P3 Step 1 dry run (no real Vanta)")
    print("=" * 68)

    mem_before = _file_fingerprint(MEMORY_JSON)
    print(f"  memory.json before: {mem_before[0]} entries")

    # --- start stub (loopback, fake key) ---
    httpd, store, req_log = make_server("127.0.0.1", 0, FAKE_KEY)
    port = httpd.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"  stub up at {base_url} (loopback only, fake key)")

    # ---------- Phase 1: direct provider round-trip ----------
    print("\n-- Phase 1: provider <-> stub round-trip --")
    p = VantaBrainProvider(base_url, FAKE_KEY, timeout_ms=800)
    check("health -> GET /health", p.healthy is True)
    p.add("m1", "the fleet agent is named Sc3pt3R", owner="growthgod")
    p.add("m2", "weather is sunny today", owner="growthgod")
    check("upsert -> POST /memories/upsert", store.count() == 2, f"count={store.count()}")
    res = p.search("agent name", k=5, owner="growthgod")
    shape_ok = isinstance(res, list) and all(
        set(x) >= {"memory_id", "score"} and isinstance(x["score"], float) for x in res)
    check("search returns [{memory_id, score}]", bool(res) and shape_ok, f"{res}")
    check("count -> GET /memories/count", p.count() == 2)
    check("auth enforced (wrong key -> unhealthy)",
          VantaBrainProvider(base_url, "WRONG", timeout_ms=800).healthy is False)

    # ---------- Phase 2: boot Odysseus pointed at the stub ----------
    print("\n-- Phase 2: boot Odysseus with VANTA_BRAIN_* -> stub --")
    env = dict(os.environ)
    env.update({
        "VANTA_BRAIN_ENABLED": "true",
        "VANTA_BRAIN_PROVIDER": "vanta",
        "VANTA_BRAIN_BASE_URL": base_url,
        "VANTA_BRAIN_API_KEY": FAKE_KEY,
        "VANTA_BRAIN_TIMEOUT_MS": "800",
    })
    log_path = "/tmp/vanta_harness_odysseus.log"
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(APP_PORT)],
            cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(60):
            if _http(f"http://127.0.0.1:{APP_PORT}/api/auth/status") in (200, 401):
                up = True
                break
            if proc.poll() is not None:
                break
            time.sleep(2)
        check("Odysseus booted (loopback)", up)
        log_text = Path(log_path).read_text(errors="replace")
        check("factory selected Vanta via FallbackProvider",
              "Vanta Brain memory index enabled" in log_text)
        check("stub received GET /health from app",
              any(r["method"] == "GET" and r["path"] == "/health" for r in req_log))
        check("stub received startup rebuild/count (180 pushed read-only)",
              any(r["path"] in ("/memories/rebuild", "/memories/count") for r in req_log))
        mem_mid = _file_fingerprint(MEMORY_JSON)
        check("memory.json unchanged after boot", mem_mid == mem_before,
              f"{mem_mid[0]} entries")

        # ---------- Phase 3: kill stub -> fail open ----------
        print("\n-- Phase 3: kill stub -> fail open to local --")
        httpd.shutdown()
        time.sleep(0.5)
        fp = FallbackProvider(VantaBrainProvider(base_url, FAKE_KEY, timeout_ms=300), _FakeLocal())
        sr = fp.search("anything")
        check("provider fails open to local keyword",
              sr == [{"memory_id": "LOCAL-KEYWORD", "score": 1.0}], f"{sr}")
        check("live Odysseus still responds after stub death",
              _http(f"http://127.0.0.1:{APP_PORT}/api/auth/status") in (200, 401))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    # ---------- invariants ----------
    print("\n-- Invariants --")
    mem_after = _file_fingerprint(MEMORY_JSON)
    check("memory.json stayed at 180 (unchanged)",
          mem_after == mem_before and mem_after[0] == 180, f"{mem_after[0]} entries")
    tidy = None
    if APP_DB.exists():
        try:
            c = sqlite3.connect(str(APP_DB))
            row = c.execute("SELECT status FROM scheduled_tasks WHERE action='consolidate_memory'").fetchone()
            tidy = row[0] if row else None
            c.close()
        except Exception:
            tidy = "?"
    check("Memory Tidy stayed paused", tidy == "paused", f"status={tidy}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 68)
    print(f"  RESULT: {passed}/{total} checks passed")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

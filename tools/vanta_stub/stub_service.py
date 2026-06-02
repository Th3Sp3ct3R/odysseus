#!/usr/bin/env python3
"""Local STUB Vanta Brain service for P3 Step 1 (dry run).

A dependency-free (stdlib http.server) mock that implements the Vanta contract
endpoints over an in-memory index. It is for LOCAL TESTING ONLY:
  - binds to 127.0.0.1 (never public),
  - requires a fake Bearer key supplied at startup,
  - holds nothing on disk, contacts no real service.

Endpoints (see docs/design/vanta-brain-provider-contract.md):
  GET    /health
  POST   /memories/upsert
  POST   /memories/search
  DELETE /memories/{memory_id}
  POST   /memories/rebuild
  GET    /memories/count

Usage (CLI):
  python -m tools.vanta_stub.stub_service --host 127.0.0.1 --port 8200 --key fake-key

Programmatic (tests/harness):
  httpd, store, log = make_server(api_key="fake-key")  # port 0 -> ephemeral
  port = httpd.server_address[1]
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("vanta_stub")


class StubStore:
    """In-memory index keyed by memory_id."""

    def __init__(self) -> None:
        self.items: Dict[str, dict] = {}

    def upsert(self, memories: List[dict]) -> Tuple[int, list]:
        upserted, failed = 0, []
        for m in memories or []:
            mid = m.get("memory_id")
            if not mid:
                failed.append({"memory_id": mid, "error": "missing memory_id"})
                continue
            self.items[mid] = dict(m)
            upserted += 1
        return upserted, failed

    def search(self, query: str, k: int, owner: Optional[str]) -> List[dict]:
        q = set((query or "").lower().split())
        scored = []
        for mid, m in self.items.items():
            if owner and m.get("owner") and m.get("owner") != owner:
                continue
            toks = set((m.get("text") or "").lower().split())
            if not q or not toks:
                continue
            inter = len(q & toks)
            if inter <= 0:
                continue
            scored.append((round(inter / len(q), 4), mid))
        scored.sort(reverse=True)
        return [{"memory_id": mid, "score": float(s)} for s, mid in scored[: max(0, k)]]

    def remove(self, memory_id: str) -> bool:
        return self.items.pop(memory_id, None) is not None

    def rebuild(self, memories: List[dict], owner: Optional[str]) -> Tuple[int, int]:
        before = len(self.items)
        if owner:
            self.items = {mid: m for mid, m in self.items.items() if m.get("owner") != owner}
        else:
            self.items = {}
        removed = before - len(self.items)
        indexed, _ = self.upsert(memories)
        return indexed, removed

    def count(self, owner: Optional[str] = None) -> int:
        if owner:
            return sum(1 for m in self.items.values()
                       if (m.get("owner") in (owner, None)))
        return len(self.items)


def make_handler(store: StubStore, api_key: str, request_log: list):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # silence default stderr logging; record structured entries instead
        def log_message(self, *args):  # noqa: A003
            pass

        def _authed(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {api_key}"

        def _send(self, code: int, payload) -> None:
            # No keep-alive: close each connection so a shut-down stub is truly
            # unreachable (a pooled keep-alive socket would otherwise be served
            # by a lingering handler thread after shutdown()).
            self.close_connection = True
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return None
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                return None

        def _record(self, method: str, path: str):
            request_log.append({"method": method, "path": path})

        def _route(self, method: str):
            path = self.path.split("?", 1)[0]
            self._record(method, path)
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})

            if method == "GET" and path == "/health":
                return self._send(200, {"status": "ok", "index_ready": True})
            if method == "GET" and path == "/memories/count":
                return self._send(200, {"count": store.count()})
            if method == "POST" and path == "/memories/upsert":
                body = self._read_json() or {}
                up, failed = store.upsert(body.get("memories", []))
                return self._send(200, {"upserted": up, "failed": failed})
            if method == "POST" and path == "/memories/search":
                body = self._read_json() or {}
                res = store.search(body.get("query", ""), int(body.get("k", 8)),
                                   body.get("owner"))
                return self._send(200, res)
            if method == "POST" and path == "/memories/rebuild":
                body = self._read_json() or {}
                indexed, removed = store.rebuild(body.get("memories", []), body.get("owner"))
                return self._send(200, {"indexed": indexed, "removed_stale": removed})
            if method == "DELETE" and path.startswith("/memories/"):
                mid = path.rsplit("/", 1)[-1]
                store.remove(mid)
                return self._send(200, {"deleted": True})
            return self._send(404, {"error": "not found"})

        def do_GET(self):     # noqa: N802
            self._route("GET")

        def do_POST(self):    # noqa: N802
            self._route("POST")

        def do_DELETE(self):  # noqa: N802
            self._route("DELETE")

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 0, api_key: str = "fake-key"):
    """Build (httpd, store, request_log). host is forced to loopback."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("stub service may only bind loopback (127.0.0.1)")
    store = StubStore()
    request_log: list = []
    httpd = ThreadingHTTPServer((host, port), make_handler(store, api_key, request_log))
    return httpd, store, request_log


def main() -> int:
    ap = argparse.ArgumentParser(description="Local STUB Vanta Brain service (loopback-only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--key", default="vanta-stub-fake-key")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    httpd, _store, _log = make_server(args.host, args.port, args.key)
    logger.info("Vanta STUB listening on http://%s:%d (loopback only)", args.host,
                httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

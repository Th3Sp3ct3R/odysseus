"""VantaBrainProvider — HTTP memory index provider (P2).

Implements the MemoryIndexProvider surface against the contract in
docs/design/vanta-brain-provider-contract.md. It is an INDEX over memory.json
ids — it never reads/writes memory.json and holds no MemoryManager handle.

Failure model: every method RAISES `VantaProviderError` on timeout / non-2xx /
malformed response, and marks itself unhealthy. It is meant to be wrapped by
`FallbackProvider`, which routes to the local provider on any failure (fail open
to keyword retrieval). The provider itself never blocks a memory.json write —
the route persists to memory.json before calling the index, and FallbackProvider
swallows index failures.

Security: API key is read from the caller (env-sourced), sent only as a Bearer
header to BASE_URL, never logged. Redirects are disabled. No credential is
persisted.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a project dependency
    httpx = None  # type: ignore

logger = logging.getLogger(__name__)


class VantaProviderError(Exception):
    """Any Vanta request failure. Caught by FallbackProvider → local fallback."""


def _parse_retry_after(value: Optional[str], default: float = 1.0) -> float:
    if not value:
        return default
    try:
        return max(0.0, float(int(value.strip())))
    except (ValueError, AttributeError):
        return default


class VantaBrainProvider:
    name = "vanta"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_ms: int = 800,
        index_on_bulk: bool = False,
        client: Any = None,
        health_ttl_s: float = 10.0,
    ):
        if httpx is None:
            raise VantaProviderError("httpx is not available")
        if not base_url or not api_key:
            # Defense in depth — the factory already guards this.
            raise VantaProviderError("base_url and api_key are required")
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = max(0.001, (timeout_ms or 800) / 1000.0)
        self._index_on_bulk = bool(index_on_bulk)
        self._health_ttl = health_ttl_s
        self._health_ts: Optional[float] = None
        self._healthy = False
        self._backoff_until = 0.0
        # follow_redirects=False: never send the key anywhere but BASE_URL origin.
        self._client = client or httpx.Client(
            base_url=self._base, timeout=self._timeout, follow_redirects=False
        )

    # ------------------------------------------------------------------ #
    # internal
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        # Built per-request; never logged.
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _mark_unhealthy(self) -> None:
        self._healthy = False
        self._health_ts = None  # force a re-probe on next `healthy` access

    def _request(self, method: str, path: str, *, json: Any = None):
        """Perform a request; raise VantaProviderError on any failure.

        Logs method/path/status/latency only — never headers or the API key.
        """
        t0 = time.monotonic()
        try:
            resp = self._client.request(method, path, json=json, headers=self._headers())
        except Exception as e:  # timeout, connection refused, DNS, etc.
            logger.warning("Vanta %s %s failed: %s", method, path, type(e).__name__)
            self._mark_unhealthy()
            raise VantaProviderError(f"{method} {path}: {type(e).__name__}") from None

        status = resp.status_code
        if status == 429:
            backoff = _parse_retry_after(resp.headers.get("Retry-After"))
            self._backoff_until = time.monotonic() + backoff
            self._healthy = False
            logger.warning("Vanta %s %s -> 429 (backoff %.1fs)", method, path, backoff)
            raise VantaProviderError("rate limited (429)")
        if status in (401, 403):
            # Auth failure — log status only, never the key/header.
            logger.warning("Vanta %s %s -> %d (auth rejected)", method, path, status)
            self._mark_unhealthy()
            raise VantaProviderError(f"auth {status}")
        if status >= 400:
            logger.warning("Vanta %s %s -> %d", method, path, status)
            self._mark_unhealthy()
            raise VantaProviderError(f"http {status}")

        logger.debug(
            "Vanta %s %s -> %d (%.0fms)", method, path, status, (time.monotonic() - t0) * 1000
        )
        return resp

    def _normalize_search(self, resp) -> List[Dict]:
        try:
            data = resp.json()
        except Exception:
            self._mark_unhealthy()
            raise VantaProviderError("malformed search response (not JSON)")
        if not isinstance(data, list):
            self._mark_unhealthy()
            raise VantaProviderError("malformed search response (not an array)")
        out: List[Dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            mid = item.get("memory_id")
            if not isinstance(mid, str):
                continue
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            out.append({"memory_id": mid, "score": score})
        return out

    # ------------------------------------------------------------------ #
    # MemoryIndexProvider surface
    # ------------------------------------------------------------------ #
    def _probe_health(self) -> bool:
        try:
            resp = self._client.request("GET", "/health", headers=self._headers())
        except Exception:
            return False
        if resp.status_code != 200:
            return False
        try:
            body = resp.json()
        except Exception:
            return False
        return bool(body.get("status") == "ok" and body.get("index_ready") is True)

    @property
    def healthy(self) -> bool:
        now = time.monotonic()
        if now < self._backoff_until:
            return False
        if self._health_ts is not None and (now - self._health_ts) < self._health_ttl:
            return self._healthy
        self._healthy = self._probe_health()
        self._health_ts = now
        return self._healthy

    def add(self, memory_id: str, text: str, *, owner: Optional[str] = None,
            bulk: bool = False, **meta) -> None:
        # INDEX_ON_BULK=false: skip per-entry upsert during a bulk import; the
        # caller is expected to rebuild() once after. memory.json is already
        # written by the route, so skipping here loses no data.
        if bulk and not self._index_on_bulk:
            logger.debug("Vanta add skipped (bulk import, INDEX_ON_BULK=false): %s", memory_id)
            return
        entry: Dict[str, Any] = {"memory_id": memory_id, "text": text}
        if owner:
            entry["owner"] = owner
        for key in ("source", "category", "tags"):
            if meta.get(key):
                entry[key] = meta[key]
        self._request("POST", "/memories/upsert", json={"memories": [entry]})

    def remove(self, memory_id: str) -> None:
        self._request("DELETE", f"/memories/{memory_id}")

    def search(self, query: str, k: int = 8, *, owner: Optional[str] = None) -> List[Dict]:
        body: Dict[str, Any] = {"query": query, "k": k}
        if owner:
            body["owner"] = owner
        resp = self._request("POST", "/memories/search", json=body)
        return self._normalize_search(resp)

    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]:
        resp = self._request("POST", "/memories/search",
                             json={"mode": "similar", "text": text, "k": 1})
        results = self._normalize_search(resp)
        if results and results[0]["score"] >= threshold:
            return results[0]["memory_id"]
        return None

    def rebuild(self, memories: List[Dict], *, owner: Optional[str] = None) -> None:
        payload: List[Dict] = []
        for m in memories:
            mid = m.get("id") or m.get("memory_id")
            if not mid:
                continue
            row: Dict[str, Any] = {"memory_id": mid, "text": m.get("text", "")}
            for key in ("owner", "source", "category"):
                if m.get(key):
                    row[key] = m[key]
            payload.append(row)
        body: Dict[str, Any] = {"replace": True, "memories": payload}
        if owner:
            body["owner"] = owner
        self._request("POST", "/memories/rebuild", json=body)

    def count(self) -> int:
        resp = self._request("GET", "/memories/count")
        try:
            return int(resp.json().get("count", 0))
        except Exception:
            self._mark_unhealthy()
            raise VantaProviderError("malformed count response")

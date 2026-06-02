"""Memory index provider seam.

A *provider* is an optional retrieval/index backend layered on top of the
canonical memory store (`data/memory.json`, owned by `MemoryManager`). It is
NEVER the source of truth — it only accelerates/augments retrieval, exactly like
the existing `MemoryVectorStore`. Providers must fail open: when unhealthy or
erroring, the system falls back to keyword retrieval with no data loss.

The protocol intentionally mirrors the surface the live call sites already use
(`routes/memory_routes.py`, `src/chat_processor.py`, `src/app_initializer.py`):
`healthy`, `add(id, text)`, `remove(id)`, `search(query, k)`, `find_similar`,
`rebuild`, `count`.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class MemoryIndexProvider(Protocol):
    """Structural contract for a memory index backend."""

    @property
    def healthy(self) -> bool: ...

    def add(self, memory_id: str, text: str, *, owner: Optional[str] = None) -> None: ...

    def remove(self, memory_id: str) -> None: ...

    def search(self, query: str, k: int = 8, *, owner: Optional[str] = None) -> List[Dict]: ...

    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]: ...

    def rebuild(self, memories: List[Dict]) -> None: ...

    def count(self) -> int: ...


class FallbackProvider:
    """Delegates to `primary` while it is healthy, else to `fallback`.

    Read path (`search`/`find_similar`/`count`): use primary when healthy; on a
    per-call exception, fail open to the fallback. Write path (`add`/`remove`/
    `rebuild`): best-effort to primary (never raises), then apply to fallback so
    the fallback index stays warm and authoritative.

    Not wired into the live path in P1 (the factory returns the local provider
    directly). Landed + tested now as the seam P2 (Vanta primary) will use.
    """

    name = "fallback"

    def __init__(self, primary: MemoryIndexProvider, fallback: MemoryIndexProvider):
        self._primary = primary
        self._fallback = fallback

    @property
    def healthy(self) -> bool:
        # Healthy if either layer can serve; fallback is the local store.
        return bool(getattr(self._primary, "healthy", False)) or bool(
            getattr(self._fallback, "healthy", False)
        )

    def _primary_up(self) -> bool:
        return bool(getattr(self._primary, "healthy", False))

    def add(self, memory_id: str, text: str, *, owner: Optional[str] = None) -> None:
        if self._primary_up():
            try:
                self._primary.add(memory_id, text, owner=owner)
            except Exception:
                logger.debug("primary provider add failed; continuing", exc_info=True)
        self._fallback.add(memory_id, text, owner=owner)

    def remove(self, memory_id: str) -> None:
        if self._primary_up():
            try:
                self._primary.remove(memory_id)
            except Exception:
                logger.debug("primary provider remove failed; continuing", exc_info=True)
        self._fallback.remove(memory_id)

    def search(self, query: str, k: int = 8, *, owner: Optional[str] = None) -> List[Dict]:
        if self._primary_up():
            try:
                return self._primary.search(query, k, owner=owner)
            except Exception:
                logger.warning("primary provider search failed; falling back", exc_info=True)
        return self._fallback.search(query, k, owner=owner)

    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]:
        if self._primary_up():
            try:
                return self._primary.find_similar(text, threshold)
            except Exception:
                logger.debug("primary provider find_similar failed; falling back", exc_info=True)
        return self._fallback.find_similar(text, threshold)

    def rebuild(self, memories: List[Dict]) -> None:
        if self._primary_up():
            try:
                self._primary.rebuild(memories)
            except Exception:
                logger.debug("primary provider rebuild failed; continuing", exc_info=True)
        self._fallback.rebuild(memories)

    def count(self) -> int:
        if self._primary_up():
            try:
                return self._primary.count()
            except Exception:
                logger.debug("primary provider count failed; falling back", exc_info=True)
        return self._fallback.count()

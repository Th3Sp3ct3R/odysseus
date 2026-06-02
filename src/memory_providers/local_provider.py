"""Local memory index provider.

A transparent wrapper around the existing `MemoryVectorStore` (ChromaDB-backed,
degrades to unhealthy when ChromaDB is absent). This is the DEFAULT and the
FALLBACK provider. It adds zero behavior: every protocol method delegates to the
underlying store, and `__getattr__` forwards anything else so the wrapper is
indistinguishable from the store to existing call sites.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class LocalMemoryProvider:
    """Pass-through provider over a MemoryVectorStore-like object."""

    name = "local"

    def __init__(self, store: Any):
        self._store = store

    @property
    def healthy(self) -> bool:
        return bool(getattr(self._store, "healthy", False))

    def add(self, memory_id: str, text: str, *, owner: Optional[str] = None) -> None:
        # The underlying store has no owner concept (owner scoping happens via
        # MemoryManager id-filtering upstream); accept and ignore for parity.
        return self._store.add(memory_id, text)

    def remove(self, memory_id: str) -> None:
        return self._store.remove(memory_id)

    def search(self, query: str, k: int = 8, *, owner: Optional[str] = None) -> List[Dict]:
        return self._store.search(query, k)

    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]:
        return self._store.find_similar(text, threshold)

    def rebuild(self, memories: List[Dict]) -> None:
        return self._store.rebuild(memories)

    def count(self) -> int:
        return self._store.count()

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined above. Guard against recursion
        # before _store is set, then forward everything to the wrapped store so
        # any incidental attribute access remains transparent.
        if name == "_store":
            raise AttributeError(name)
        return getattr(self._store, name)

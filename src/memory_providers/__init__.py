"""Optional memory index providers (adapter seam).

The canonical store remains `data/memory.json` via `MemoryManager`. Providers
here only augment retrieval and must fail open to keyword search. See
docs/design/vanta-brain-adapter.md.
"""
from src.memory_providers.base import MemoryIndexProvider, FallbackProvider
from src.memory_providers.local_provider import LocalMemoryProvider
from src.memory_providers.factory import build_memory_index

__all__ = [
    "MemoryIndexProvider",
    "FallbackProvider",
    "LocalMemoryProvider",
    "build_memory_index",
]

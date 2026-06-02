"""Memory index provider factory — the single selection point.

Reads the VANTA_BRAIN_* env flags and returns a provider. In P1 only the local
provider exists, so the result is always local (the seam is forward-compatible:
P2 will return FallbackProvider(VantaBrainProvider(...), local) when enabled).

Default behavior (VANTA_BRAIN_ENABLED unset/false) is byte-for-byte identical to
constructing a MemoryVectorStore directly.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from src.memory_providers.local_provider import LocalMemoryProvider

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in _TRUE


def _build_local(data_dir: str, embedding_model: Any = None) -> LocalMemoryProvider:
    from src.memory_vector import MemoryVectorStore
    return LocalMemoryProvider(MemoryVectorStore(data_dir, embedding_model=embedding_model))


def build_memory_index(data_dir: str, embedding_model: Any = None):
    """Return the configured memory index provider.

    P1: always local. If VANTA_BRAIN_ENABLED=true the Vanta provider is not yet
    implemented, so we log once and use local (fail-safe by design).
    """
    local = _build_local(data_dir, embedding_model)

    if not _env_bool("VANTA_BRAIN_ENABLED", False):
        return local

    provider = (os.environ.get("VANTA_BRAIN_PROVIDER") or "vanta").strip().lower()
    logger.warning(
        "VANTA_BRAIN_ENABLED=true (provider=%s) but the Vanta provider is not "
        "implemented yet (P2); using the local memory index.", provider,
    )
    return local

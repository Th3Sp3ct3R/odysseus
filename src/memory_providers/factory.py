"""Memory index provider factory — the single selection point.

Reads the VANTA_BRAIN_* env flags and returns a provider:

- VANTA_BRAIN_ENABLED unset/false  -> LocalMemoryProvider (default; byte-for-byte
  identical to constructing a MemoryVectorStore directly).
- enabled + provider=vanta + BASE_URL + API_KEY -> FallbackProvider(Vanta, local).
- enabled but provider!=vanta, or BASE_URL/API_KEY missing, or Vanta init fails
  -> log a WARNING and fall back to local.

The factory never logs the API key (it only checks presence) and never makes a
network call itself (the first Vanta call happens lazily on `.healthy`/use).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from src.memory_providers.base import FallbackProvider
from src.memory_providers.local_provider import LocalMemoryProvider

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in _TRUE


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None or not str(val).strip():
        return default
    try:
        return int(str(val).strip())
    except ValueError:
        return default


def _build_local(data_dir: str, embedding_model: Any = None) -> LocalMemoryProvider:
    from src.memory_vector import MemoryVectorStore
    return LocalMemoryProvider(MemoryVectorStore(data_dir, embedding_model=embedding_model))


def build_memory_index(data_dir: str, embedding_model: Any = None):
    """Return the configured memory index provider (see module docstring)."""
    local = _build_local(data_dir, embedding_model)

    if not _env_bool("VANTA_BRAIN_ENABLED", False):
        return local

    provider = (os.environ.get("VANTA_BRAIN_PROVIDER") or "vanta").strip().lower()
    if provider != "vanta":
        logger.warning("Unknown VANTA_BRAIN_PROVIDER=%s; using local memory index.", provider)
        return local

    base_url = (os.environ.get("VANTA_BRAIN_BASE_URL") or "").strip()
    api_key = os.environ.get("VANTA_BRAIN_API_KEY") or ""  # presence-checked only; never logged
    if not base_url or not api_key:
        logger.warning(
            "VANTA_BRAIN_ENABLED=true but VANTA_BRAIN_BASE_URL/API_KEY missing; "
            "using local memory index."
        )
        return local

    try:
        from src.memory_providers.vanta_provider import VantaBrainProvider
        vanta = VantaBrainProvider(
            base_url,
            api_key,
            timeout_ms=_env_int("VANTA_BRAIN_TIMEOUT_MS", 800),
            index_on_bulk=_env_bool("VANTA_BRAIN_INDEX_ON_BULK", False),
        )
    except Exception as e:
        logger.warning("Vanta provider init failed (%s); using local memory index.", type(e).__name__)
        return local

    logger.info("Vanta Brain memory index enabled (fallback=local).")
    return FallbackProvider(vanta, local)

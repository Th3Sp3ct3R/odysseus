#!/usr/bin/env python3
"""
reindex_memory_vectors.py — backfill/rebuild the Odysseus memory vector index.

Reads every entry from data/memory.json and (re)indexes it into the Chroma
collection `odysseus_memories` via MemoryVectorStore.rebuild(). Run this after
starting the Chroma server, and any time you want to resync the vector index
with memory.json.

Fail-open: if Chroma is not healthy, it indexes nothing and returns non-zero —
keyword search keeps working regardless (never breaks the bus).

Usage:
    .venv/bin/python scripts/reindex_memory_vectors.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import DATA_DIR
from src.memory import MemoryManager
from src.memory_vector import MemoryVectorStore


def main() -> int:
    entries = MemoryManager(DATA_DIR).load_all()
    mv = MemoryVectorStore(DATA_DIR)
    if not mv.healthy:
        print("⚠️  Chroma not healthy — vector index NOT built. "
              "Keyword search still works. (Start the Chroma server, then retry.)")
        return 1
    mv.rebuild(entries)
    print(f"✅ Indexed {mv.count()} entries into Chroma collection "
          f"'{mv.COLLECTION_NAME}' (from {len(entries)} memories).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
memory_readonly_server.py

READ-ONLY view of Odysseus memory, for Hermes (the executor) to query live
mid-session — the "live lane" alongside the session-start vault snapshot.

Deliberately read-only (search + list, no add/edit/delete): Hermes writes back
to Odysseus through the task-result → ingest path, never directly. Keeping this
lane read-only means Hermes and Odysseus's own process never race on memory.json.

Launched by Hermes as an MCP subprocess with cwd = the Odysseus repo root and
the Odysseus venv python (so `src.*` imports + chromadb resolve). Reuses
MemoryManager + MemoryVectorStore.
"""

import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("odysseus_memory")

_memory_manager = None
_memory_vector = None
_initialized = False


def _ensure_init():
    global _memory_manager, _memory_vector, _initialized
    if _initialized:
        return
    _initialized = True
    from src.constants import DATA_DIR
    from src.memory import MemoryManager
    _memory_manager = MemoryManager(DATA_DIR)
    try:
        from src.memory_vector import MemoryVectorStore
        mv = MemoryVectorStore(DATA_DIR)
        _memory_vector = mv if getattr(mv, "healthy", False) else None
    except Exception:
        _memory_vector = None


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_memory",
            description=(
                "Search Odysseus's (the orchestrator's) memory live — semantic + keyword. "
                "Use this to recall what Odysseus knows/decided beyond the session-start snapshot."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_memory",
            description="List Odysseus memory entries, optionally filtered by category.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Optional category filter (fact/project/...)"},
                    "limit": {"type": "integer", "description": "Max results (default 100)"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    _ensure_init()
    if not _memory_manager:
        return [TextContent(type="text", text="Error: Odysseus memory not available")]

    if name == "search_memory":
        query = (arguments.get("query") or "").strip()
        if not query:
            return [TextContent(type="text", text="Error: search_memory needs a 'query'.")]
        limit = int(arguments.get("limit") or 20)
        memories = _memory_manager.load()
        by_id = {m.get("id"): m for m in memories}
        mode = "keyword"
        results = []

        # Semantic first (Chroma vector store), keyword fallback. If Chroma is
        # down/empty/errors, we fall through to the Jaccard keyword path — the
        # lane never hard-fails on a missing vector backend.
        if _memory_vector is not None and getattr(_memory_vector, "healthy", False):
            try:
                if _memory_vector.count() > 0:
                    hits = _memory_vector.search(query, k=limit)
                    results = [dict(by_id[h["memory_id"]], _score=h["score"])
                               for h in hits if h.get("memory_id") in by_id]
                    if results:
                        mode = "semantic"
            except Exception:
                results = []

        if not results:
            mode = "keyword"
            if hasattr(_memory_manager, "get_relevant_memories"):
                results = _memory_manager.get_relevant_memories(query, memories, threshold=0.05, max_items=limit)
            else:
                ql = query.lower()
                results = [m for m in memories if ql in (m.get("text") or "").lower()][:limit]

        if not results:
            return [TextContent(type="text", text=f"No Odysseus memories matching '{query}'.")]
        lines = [f"Found {len(results)} Odysseus memories matching '{query}' (mode: {mode}):\n"]
        for m in results:
            sc = m.get("_score")
            score = f" ({sc:.2f})" if isinstance(sc, (int, float)) else ""
            lines.append(f"- [{m.get('category','fact')}] `{m.get('id','?')[:8]}`{score} — {m.get('text','')}")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "list_memory":
        cat = (arguments.get("category") or "").strip()
        limit = int(arguments.get("limit") or 100)
        memories = _memory_manager.load()
        if cat:
            memories = [m for m in memories if (m.get("category") or "").lower() == cat.lower()]
        if not memories:
            return [TextContent(type="text", text="No Odysseus memories found.")]
        lines = [f"{len(memories)} Odysseus memories" + (f" in '{cat}'" if cat else "") + ":\n"]
        for m in memories[:limit]:
            t = m.get("text", "")
            t = t if len(t) <= 150 else t[:150] + "..."
            lines.append(f"- [{m.get('category','fact')}] `{m.get('id','?')[:8]}` — {t}")
        if len(memories) > limit:
            lines.append(f"... and {len(memories) - limit} more")
        return [TextContent(type="text", text="\n".join(lines))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

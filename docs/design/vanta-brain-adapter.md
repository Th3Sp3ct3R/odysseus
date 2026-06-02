# Design: Vanta Brain as an optional adapter layer

**Status:** Design-only (first pass). No production behavior changes proposed in this
document. Implementation requires explicit approval.

**Goal:** Add Vanta Brain as an *optional* memory **index/retrieval provider** layered on
top of Odysseus's existing memory system — not a rewrite, not a new source of truth.
Local keyword memory stays the default and the fallback.

---

## 1. Current memory architecture (as-is)

Source of truth and the two seams that matter:

| Concern | Where | Notes |
|---|---|---|
| **Canonical store** | `src/memory.py` `MemoryManager` → `data/memory.json` | The 180 restored facts live here. `load/load_all/save/add_entry`. |
| **Dedup / idempotency** | `MemoryManager.find_duplicates` (exact lowercase match) | Server-side guard; client adds a normalized-hash ledger in `tools/obsidian_sync`. |
| **Keyword retrieval** | `MemoryManager.get_relevant_memories` + `src/chat_processor.py` BM25 + category boosts | **Always runs.** This is the default/fallback retrieval. |
| **Vector index (optional)** | `src/memory_vector.py` `MemoryVectorStore` (ChromaDB) | Degrades to "unhealthy" when ChromaDB absent; retrieval still works via keyword. |
| **Write API** | `routes/memory_routes.py` `api_add_memory` | dedup → `memory.json` save → `if memory_vector.healthy: memory_vector.add`. |
| **Bulk-import guard** | `api_add_memory` skips `memory_added` event when `X-Odysseus-Bulk-Import` header present | Keeps Memory Tidy from collapsing bulk imports. |
| **Memory Tidy boundary** | `src/event_bus.py` → `scheduled_tasks` (event `memory_added`, every 5) → `builtin_actions.action_consolidate_memory` (LLM) | Currently **paused**. Independent of retrieval. |
| **Construction / injection** | `src/app_initializer.py` builds `memory_manager` + `memory_vector`, injected into routes + `chat_processor` | The single place a provider is selected. |
| **obsidian_sync import** | `tools/obsidian_sync/sync_memory.py` → `POST /api/memory/add` with bulk header | Deterministic, no-LLM, dedup via local hash ledger. |

**Key observations that make the adapter safe:**
1. Retrieval is **already a dual path** (vector-or-keyword). Adding a third backend is a
   variation of an existing pattern, not a new architecture.
2. `MemoryVectorStore` already has a provider-shaped surface:
   `healthy`, `add(id, text)`, `remove(id)`, `search(query, k) -> [{memory_id, score}]`,
   `find_similar(text, threshold)`, `rebuild(memories)`, `count()`.
3. The vector index is **never the source of truth** — `chat_processor` only scores ids
   that exist in `memory.json` (`mem_by_id`). An index that goes away changes ranking, never
   data.

> Note — module duplication exists: `services/memory/` (with `MemoryService`) is a parallel,
> largely-unwired facade; the **live** path uses `src/memory.py` + `src/memory_vector.py`.
> This design targets the live `src/` path. Unifying the two is out of scope.

---

## 2. Proposed provider interface

A thin `Protocol` matching the **existing** `MemoryVectorStore` surface, so Vanta and Local
are interchangeable and call sites don't change.

```python
# src/memory_providers/base.py  (proposed)
from typing import Protocol, List, Dict, Optional, runtime_checkable

@runtime_checkable
class MemoryIndexProvider(Protocol):
    @property
    def healthy(self) -> bool: ...
    def add(self, memory_id: str, text: str, *, owner: Optional[str] = None) -> None: ...
    def remove(self, memory_id: str) -> None: ...
    def search(self, query: str, k: int = 8, *, owner: Optional[str] = None) -> List[Dict]: ...
    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]: ...
    def rebuild(self, memories: List[Dict]) -> None: ...
    def count(self) -> int: ...
```

`search()` MUST return `[{"memory_id": str, "score": float}, ...]` (the shape
`chat_processor` already consumes). `owner` is added as an optional kwarg for multi-tenant
scoping; the local provider may ignore it (it already intersects with owner-filtered ids).

Two implementations:
- **`LocalMemoryProvider`** — thin wrapper over today's `MemoryVectorStore` (or `MemoryVectorStore`
  itself, which already satisfies the protocol). This is the default and the fallback.
- **`VantaBrainProvider`** — HTTP client to the Vanta Brain service. Index-only over
  `memory.json` ids. All network calls wrapped in tight timeouts and `try/except` that
  **fail open** (return `[]` / no-op), exactly mirroring `MemoryVectorStore`'s unhealthy path.

A `FallbackProvider(primary, fallback)` wrapper delegates to `primary` while `primary.healthy`,
otherwise to `fallback`; on a per-call exception in `search()`, it returns the fallback result
(or `[]`). This is the only new control-flow concept.

---

## 3. Where `VantaBrainProvider` plugs in

**One injection point:** `src/app_initializer.py`. Today:

```python
memory_vector = MemoryVectorStore(DATA_DIR, embedding_model=embedding_model)
```

Proposed (behind the default-off flag):

```python
from src.memory_providers.factory import build_memory_index
memory_vector = build_memory_index(DATA_DIR, embedding_model=embedding_model)
# returns LocalMemoryProvider by default; FallbackProvider(Vanta, Local) when enabled
```

The injected object keeps the name/role of `memory_vector`, so **`routes/memory_routes.py`
and `src/chat_processor.py` need zero changes** — they keep calling `.healthy`, `.add`,
`.search`. Minimal blast radius.

---

## 4. Invariants — how each guarantee is preserved

- **Local keyword memory stays default.** `VANTA_BRAIN_ENABLED=false` → factory returns the
  local provider; runtime is byte-for-byte today's behavior. Even when enabled, BM25 keyword
  scoring in `chat_processor` *always* runs and is blended with provider scores.
- **Fallback when Vanta fails.** Init failure or `healthy == False` → `FallbackProvider` uses
  Local. Per-query exception/timeout → caught, treated as "no vector scores," keyword ranking
  stands. Same failure mode as ChromaDB being absent today (already exercised — the 180 were
  restored under exactly this degraded mode).
- **Bulk-import guard stays protected.** The guard lives in `api_add_memory` and is independent
  of the index provider. `provider.add()` is called *after* the `memory.json` write and is
  best-effort; provider choice never re-introduces the `memory_added` event. (Test pins this.)
- **Dedup / idempotency unchanged.** Dedup is `MemoryManager.find_duplicates` on `memory.json`.
  The provider is never consulted for dedup. `find_similar` (if used by Vanta) is advisory only
  and must not gate writes.
- **180 memories untouched.** Vanta is index-only; it never writes `memory.json`. Worst case a
  stale index → fixed by `rebuild(memory_manager.load())` on reconnect.
- **Memory Tidy stays paused.** Out of scope; provider does not touch `scheduled_tasks` and must
  never emit `memory_added`.
- **No secrets stored.** Vanta base URL/key come from env only; never written to repo, config,
  or logs.

---

## 5. Proposed env flags

| Flag | Default | Meaning |
|---|---|---|
| `VANTA_BRAIN_ENABLED` | `false` | Master switch. Off → pure local behavior. |
| `VANTA_BRAIN_PROVIDER` | `vanta` | Primary provider id when enabled. |
| `VANTA_BRAIN_FALLBACK_PROVIDER` | `local` | Fallback when primary is unhealthy. |
| `VANTA_BRAIN_BASE_URL` | — | Vanta service URL (loopback/Tailscale; not public). |
| `VANTA_BRAIN_API_KEY` | — | Secret, env-only. Never logged or persisted. |
| `VANTA_BRAIN_TIMEOUT_MS` | `800` | Hard timeout for hot-path search; fail open past it. |
| `VANTA_BRAIN_INDEX_ON_BULK` | `false` | If false, skip per-entry `add` during bulk import and `rebuild` once at the end. |

`.env.example` gets these documented; `.env` stays gitignored.

---

## 6. Files that would change (implementation phase — NOT now)

**New:**
- `src/memory_providers/__init__.py`
- `src/memory_providers/base.py` — `MemoryIndexProvider` protocol + `FallbackProvider`
- `src/memory_providers/local_provider.py` — wraps `MemoryVectorStore`
- `src/memory_providers/vanta_provider.py` — `VantaBrainProvider` (HTTP, fail-open)
- `src/memory_providers/factory.py` — reads env flags, returns provider
- `tests/test_memory_providers.py` — see §7
- `docs/design/vanta-brain-adapter.md` — this file

**Modified (small, additive):**
- `src/app_initializer.py` — swap direct `MemoryVectorStore(...)` for `build_memory_index(...)`
- `.env.example` — document the flags

**Explicitly NOT changed** (the design's main payoff):
- `routes/memory_routes.py`, `src/chat_processor.py`, `src/memory.py`, `src/memory_vector.py`,
  the bulk-import guard, dedup, Memory Tidy, `tools/obsidian_sync/*`.

---

## 7. Required tests

1. **Default is local:** `VANTA_BRAIN_ENABLED=false` → factory returns local provider; no network.
2. **Enabled + healthy:** factory returns Vanta-backed provider; `add`/`search` delegate to it.
3. **Init fallback:** enabled but primary unhealthy at construction → `FallbackProvider` uses local.
4. **Query fail-open:** primary `search()` raises/times out → result is keyword-only; no exception
   bubbles to chat.
5. **Write resilience:** `provider.add()` raising does NOT block the `memory.json` write
   (dedup/idempotency assertions unchanged).
6. **Bulk guard intact:** with provider enabled, `X-Odysseus-Bulk-Import` still suppresses
   `memory_added` (extend `tests/test_memory_bulk_import_guard.py`).
7. **No-secret logging:** provider never logs `VANTA_BRAIN_API_KEY`.
8. **Search contract:** Vanta results normalized to `{memory_id, score}` and filtered to ids
   present in `memory.json`.

All tests use stubbed HTTP (no live Vanta, no heavy deps) and run under the existing pytest setup.

---

## 8. Risks / edge cases

- **Interface drift:** `chat_processor` expects `{memory_id, score}`. Vanta must be normalized or
  reads silently degrade. → Contract test (§7.8).
- **Index vs store confusion:** Vanta must stay index-only. If it ever returns ids not in
  `memory.json`, they're dropped by `mem_by_id` filtering (safe), but that means Vanta-only
  knowledge is invisible — by design for this phase.
- **Hot-path latency:** a network call in chat ranking. Mitighome: tight `VANTA_BRAIN_TIMEOUT_MS`,
  fail open, and consider async/cache later.
- **Bulk import cost:** 180 imports × 1 Vanta write each. Mitigation: `VANTA_BRAIN_INDEX_ON_BULK=false`
  → skip per-entry, single `rebuild` at end.
- **Stale index after Vanta downtime:** writes succeed to `memory.json`, index misses them.
  Mitigation: `rebuild` on reconnect / healthy-transition.
- **Owner scoping:** Vanta must scope by `owner` or results get cross-tenant. Local path already
  intersects with owner-filtered ids; Vanta should pass `owner` and/or rely on the same intersect.
- **Secret handling:** env-only; assert no logging; redact in any debug output.
- **Memory Tidy interaction:** provider must never emit `memory_added`; keep Tidy paused.

---

## 9. Recommendation

Proceed in this order *after approval*, each phase gated:
1. **P1 (no behavior change):** add `base.py` protocol + `LocalMemoryProvider` + factory wired to
   return local by default; swap the one line in `app_initializer`. Prove identical behavior + the
   tests in §7.1–7.6. This lands the seam with zero functional change.
2. **P2:** add `VantaBrainProvider` (HTTP, fail-open) + `FallbackProvider`, still default-off.
3. **P3:** enable behind env in a controlled test, validate fallback + latency, then decide.

Stop here per instruction — no code beyond this design until approved.

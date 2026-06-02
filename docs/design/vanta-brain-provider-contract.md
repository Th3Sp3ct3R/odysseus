# Vanta Brain Provider — API Contract (P2 pre-implementation)

**Status:** Contract-only. No client code in this branch. This defines the exact
HTTP contract `VantaBrainProvider` will implement so P2 can be built and tested
against a stub without ambiguity.

**Relationship to P1:** `VantaBrainProvider` implements the existing
`MemoryIndexProvider` protocol (`src/memory_providers/base.py`). It is selected by
the factory only when `VANTA_BRAIN_ENABLED=true`, wrapped as
`FallbackProvider(VantaBrainProvider(...), LocalMemoryProvider(...))`. The Vanta
service is an **index over `memory.json` ids** — never a source of truth.

Protocol → endpoint mapping:

| `MemoryIndexProvider` method | Vanta endpoint |
|---|---|
| `healthy` | `GET /health` |
| `add(memory_id, text, owner=)` | `POST /memories/upsert` |
| `search(query, k, owner=)` | `POST /memories/search` |
| `remove(memory_id)` | `DELETE /memories/{memory_id}` |
| `rebuild(memories)` | `POST /memories/rebuild` |
| `count()` | `GET /memories/count` |
| `find_similar(text, threshold)` | (optional) `POST /memories/search` with `mode=similar` |

---

## 1. Required environment

| Var | Default | Required when | Meaning |
|---|---|---|---|
| `VANTA_BRAIN_ENABLED` | `false` | always read | Master switch. `false` ⇒ provider never constructed; pure local. |
| `VANTA_BRAIN_PROVIDER` | `vanta` | enabled | Primary provider id. |
| `VANTA_BRAIN_FALLBACK_PROVIDER` | `local` | enabled | Fallback id. Only `local` supported. |
| `VANTA_BRAIN_BASE_URL` | — | enabled (**required**) | e.g. `http://127.0.0.1:8200`. Loopback/Tailscale only; never public. No trailing slash. |
| `VANTA_BRAIN_API_KEY` | — | enabled (**required**) | Bearer secret. Env-only; never logged/persisted. |
| `VANTA_BRAIN_TIMEOUT_MS` | `800` | enabled | Per-request hard timeout (connect+read). Search must fail open past it. |
| `VANTA_BRAIN_INDEX_ON_BULK` | `false` | enabled | `false` ⇒ skip per-entry upsert during bulk import; single `rebuild` after. |

Startup validation (enabled): if `BASE_URL` or `API_KEY` is missing, log one
WARNING and **fall back to local** (do not crash, do not block boot).

---

## 2. Endpoints

All requests:
- `Authorization: Bearer <VANTA_BRAIN_API_KEY>`
- `Content-Type: application/json` (for bodies)
- `Accept: application/json`
- Hard timeout = `VANTA_BRAIN_TIMEOUT_MS`.
- Base = `VANTA_BRAIN_BASE_URL`.

### 2.1 `GET /health`
Liveness/readiness for `provider.healthy`. Cheap, no auth side effects.

**200**:
```json
{ "status": "ok", "index_ready": true }
```
`healthy == (HTTP 200 and status=="ok" and index_ready==true)`. Anything else
(non-200, timeout, malformed) ⇒ `healthy == false`.

### 2.2 `POST /memories/upsert`
Idempotent index upsert keyed by `memory_id`. Maps to `add()` (single) and is the
batch unit for `rebuild()`.

**Request:**
```json
{
  "memories": [
    {
      "memory_id": "a9b96eda-8fbb-46bc-a9e1-9d591cb17203",
      "text": "Agent: Sc3pt3R",
      "owner": "growthgod",
      "source": "obsidian:OpenClaw-Export.md",
      "category": "fact",
      "tags": ["fleet"]
    }
  ]
}
```
Single `add()` sends a 1-element `memories` array. `owner`/`source`/`category`
echo the `memory.json` entry; `tags` optional.

**200:**
```json
{ "upserted": 1, "failed": [] }
```
Partial failure (see §5): `failed` lists `{ "memory_id": "...", "error": "..." }`.
Upsert MUST be idempotent — re-sending the same `memory_id` updates in place, never
duplicates.

### 2.3 `POST /memories/search`
Maps to `search()`. Returns ranked `memory_id`s scoped to `owner`.

**Request:**
```json
{ "query": "what is the agent name", "k": 8, "owner": "growthgod" }
```
Optional: `"mode": "similar"` + `"text": "..."` for `find_similar`
(returns the single best match above a server-side threshold).

**200:** see §3.

### 2.4 `DELETE /memories/{memory_id}`
Maps to `remove()`. Idempotent: deleting an unknown id returns **200/204** (not 404-as-error).

**200:**
```json
{ "deleted": true }
```

### 2.5 `POST /memories/rebuild`
Maps to `rebuild()`. Full reindex from the authoritative set the caller supplies
(always derived from `memory.json`). Server replaces its index for the given
`owner` scope.

**Request:**
```json
{
  "owner": "growthgod",
  "replace": true,
  "memories": [ { "memory_id": "...", "text": "...", "owner": "...", "source": "...", "category": "..." } ]
}
```
**200:**
```json
{ "indexed": 180, "removed_stale": 3 }
```

### 2.6 `GET /memories/count`
Maps to `count()`. Optional `?owner=growthgod`.

**200:**
```json
{ "count": 180 }
```

---

## 3. Search response shape (authoritative)

`POST /memories/search` **200** body MUST be exactly:
```json
[
  { "memory_id": "string", "score": 0.95 }
]
```
- Top-level JSON array, ordered by descending `score`.
- `memory_id`: string, must correspond to an id in `memory.json`.
- `score`: float in `[0.0, 1.0]`.
- Extra keys are ignored by the client. Missing `memory_id` or non-numeric
  `score` ⇒ that element is dropped; a non-array body ⇒ treated as malformed (§5).

**Client post-processing (unchanged from today's vector path):** results are
intersected with the owner-filtered ids from `memory.json` (`mem_by_id` in
`chat_processor`). Vanta ids not present locally are dropped — Vanta cannot inject
memories that aren't in the canonical store.

---

## 4. Owner / scoping fields

| Field | Where | Required | Notes |
|---|---|---|---|
| `memory_id` | upsert/search-result/delete/rebuild | yes | Canonical id from `memory.json` (`entry["id"]`). The join key. |
| `owner` | upsert/search/rebuild/count | yes when known | Tenant scope = Odysseus username (e.g. `growthgod`). Search MUST scope by it. |
| `source` | upsert/rebuild | optional | Provenance string (e.g. `obsidian:<relpath>`). For audit/filtering only. |
| `category` | upsert/rebuild | optional | One of `identity|preference|fact|contact|project|goal`. May inform ranking. |
| `tags` | upsert/rebuild | optional | Array of strings if the server supports tag filters; ignored otherwise. |

Owner is authoritative for isolation: a search with `owner=A` must never return
`B`'s memory_ids. The client also intersects with local owner-filtered ids as a
second guard (§3).

---

## 5. Failure behavior (client-side handling)

The client wraps every call in a hard timeout + `try/except` and maps outcomes to
the fail-open contract. **No Vanta failure may raise into a chat request or block
a `memory.json` write.**

| Condition | `healthy` | `search` | `add`/`upsert` | `remove`/`rebuild`/`count` |
|---|---|---|---|---|
| **Timeout** (`> TIMEOUT_MS`) | false | `[]` → keyword fallback | log debug, no-op (memory.json already written) | no-op / fallback `count` |
| **401 / 403** (bad/expired key) | false | `[]` → fallback; log WARNING once (no key) | log WARNING once, no-op | no-op |
| **429** (rate limited) | unchanged | `[]` → fallback; honor `Retry-After` for backoff | defer/no-op; mark for next `rebuild` | no-op |
| **500 / 5xx** | false | `[]` → fallback | log WARNING, no-op | no-op |
| **Malformed response** (non-JSON / wrong shape) | false | `[]` → fallback | treat as failure, no-op | no-op |
| **Partial upsert failure** (`failed[]` non-empty) | unchanged | n/a | log count; queue failed ids for next `rebuild`; memory.json unaffected | n/a |
| **Connection refused / DNS** | false | `[]` → fallback | no-op | no-op |

Notes:
- 401/403 logs the **status and a static message only** — never the key or
  `Authorization` header.
- 429 backoff is best-effort; the hot path still fails open immediately to keyword.
- Repeated failures flip `healthy=false`, so `FallbackProvider` routes reads to
  local until the next successful `GET /health`.

---

## 6. Fallback rules (binding)

These mirror what `FallbackProvider` (P1) already implements; the contract pins
the semantics:

1. **Vanta disabled** (`VANTA_BRAIN_ENABLED=false`) → factory returns
   `LocalMemoryProvider`. Vanta client never constructed; no network.
2. **Vanta unhealthy at startup** (missing config, `/health` not ok, unreachable)
   → factory returns `FallbackProvider(vanta, local)`; since `vanta.healthy` is
   false, all calls route to local. Boot never blocks.
3. **Vanta search timeout/error** → `search()` returns local result; chat ranking
   uses keyword BM25 (always running). Never raises.
4. **Vanta upsert failure** → the `POST /api/memory/add` flow has ALREADY written
   `memory.json` (and deduped) before the provider is called; provider failure is
   logged and swallowed. The write succeeds regardless. Failed ids are reconciled
   on the next `rebuild`/healthy transition.
5. **Vanta must never delete or rewrite `memory.json`.** The provider has no handle
   to `MemoryManager`; it only talks HTTP. `rebuild` reads FROM `memory.json` and
   pushes TO Vanta — one direction only. Deletes target the Vanta index, never the
   local store.

Invariant: with Vanta fully down, observable behavior equals today's
ChromaDB-absent degraded mode — which is exactly how the 180 memories currently
run and were restored.

---

## 7. Security

- **API key is env-only** (`VANTA_BRAIN_API_KEY`). Never written to `config.yaml`,
  `data/`, `settings.json`, logs, or error messages. Not echoed in
  `/api/...` responses.
- **Never log secrets.** Redact `Authorization` in any debug/trace. Log endpoints,
  status codes, latencies, and counts only.
- **Do not persist Vanta credentials.** No caching of the key to disk/db; read from
  env at construction.
- **Localhost/dev-safe defaults.** `VANTA_BRAIN_ENABLED=false` by default; when
  enabled, `BASE_URL` is expected to be loopback or a Tailscale address. The client
  does not follow redirects to other hosts and does not send the key to any host
  other than `BASE_URL`'s origin.
- **No new outbound exposure.** The provider only makes outbound calls to
  `BASE_URL`; it opens no listeners and changes no bind addresses.
- **PII boundary.** Memory `text` is sent to Vanta only when enabled; document this
  in operator docs so users opt in knowingly.

---

## 8. P2 implementation checklist (after approval)

- `src/memory_providers/vanta_provider.py` — `VantaBrainProvider` implementing the
  protocol per this contract; `httpx` (already a dep) with per-call timeout; all
  methods fail-open.
- `src/memory_providers/factory.py` — when enabled + config present, return
  `FallbackProvider(VantaBrainProvider(...), local)`; else local (P1 behavior).
- `tests/test_vanta_provider.py` — against a stubbed transport (no live service,
  no new deps): health gating, search shape + owner scope, upsert idempotency,
  every row of the §5 failure matrix, and the §6 fallback rules.
- Bulk: honor `VANTA_BRAIN_INDEX_ON_BULK=false` (skip per-entry, `rebuild` once).
- No changes to routes, dedup, the bulk-import guard, or Memory Tidy.

Stop here — contract only. No client implementation until approved.

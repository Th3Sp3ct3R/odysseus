# P3 Plan: Controlled Vanta Brain Enablement

**Status:** Documentation only. No code, no service, no enablement. This plan
gates the eventual turn-on of `VantaBrainProvider` so it can be validated
safely and reverted instantly.

Related: `docs/design/vanta-brain-adapter.md` (design, P1),
`docs/design/vanta-brain-provider-contract.md` (HTTP contract, P2).

---

## 1. Current state after P2

- **Provider seam (P1) merged** — `src/memory_providers/` with `MemoryIndexProvider`,
  `LocalMemoryProvider` (transparent wrapper over `MemoryVectorStore`),
  `FallbackProvider`, and `build_memory_index()` factory at the single injection
  point in `src/app_initializer.py`.
- **VantaBrainProvider (P2) merged** — `src/memory_providers/vanta_provider.py`
  implements the contract over `httpx` (already a dependency). Mapping:
  `healthy→GET /health`, `add→POST /memories/upsert`,
  `search→POST /memories/search`, `remove→DELETE /memories/{id}`,
  `rebuild→POST /memories/rebuild`, `count→GET /memories/count`.
- **Default OFF** — `VANTA_BRAIN_ENABLED=false`. With it off (or with config
  missing/invalid, or Vanta unhealthy), the factory returns the local provider.
  No Vanta connection is made.
- **Verified** — full suite 298 passing; app boots default-off with zero Vanta
  connection; `data/memory.json` holds the 180 restored memories; the
  "Memory Tidy" (`consolidate_memory`) task is **paused**.
- All work lives in the fork `Th3Sp3ct3R/odysseus`; no upstream remote exists.

P3 is the FIRST time Vanta would actually be contacted, and only in a contained
test — never as a committed default.

---

## 2. P3 goal

Turn Vanta on in a contained, reversible way for a single owner, prove that:
1. healthy selection + fallback work against a real endpoint,
2. the index can be backfilled from the existing 180 memories,
3. hot-path search latency is acceptable and fails open under load/error,
4. bulk import stays cheap and safe,
then decide keep-on vs. revert — with `memory.json` and the local keyword path
always intact.

---

## 3. Prerequisites (operator-provided)

- `VANTA_BRAIN_BASE_URL` — e.g. `http://127.0.0.1:8200` or a Tailscale address.
- `VANTA_BRAIN_API_KEY` — supplied via environment only (never committed, never
  logged, never persisted).
- **owner = `growthgod` first** — enable/validate for this single account before
  any broader use.
- **The Vanta service must be private** — loopback or Tailscale only, never
  exposed to the public internet. Behind TLS if it leaves the host.
- Optional: `VANTA_BRAIN_TIMEOUT_MS` (default 800), `VANTA_BRAIN_INDEX_ON_BULK`
  (default false).

These are set in the runtime environment for the test only — not in
`.env.example` or any committed config.

---

## 4. Step-by-step gated plan

Each step has an explicit pass/fail gate. Stop and revert (Section 6) on any fail.

### Step 1 — Stub-service dry run
Stand up a minimal local mock implementing the 6 contract endpoints (planned in a
later, separate change — NOT in this doc). Set the env, boot the app, confirm:
- `GET /health` probed once at first use; factory selects `FallbackProvider(Vanta, local)`.
- A search and an add reach the mock with the expected request shapes.
- Kill the mock mid-session → next call **fails open to local keyword** with no
  error surfaced to chat and no write failure.
- **Gate:** healthy selection works; kill → instant fallback; `memory.json` unchanged.

### Step 2 — Index backfill from existing 180 memories
With Vanta healthy, trigger one `rebuild()` from `memory_manager.load()` (180
entries). This is one-directional: READ from `memory.json`, PUSH to Vanta.
- Confirm `count()` on Vanta returns 180 (scoped to `owner=growthgod`).
- **Gate:** Vanta count == 180; `memory.json` is read-only in this step (still 180).

### Step 3 — Shadow latency / search check
Exercise chat retrieval with Vanta healthy.
- Measure search hot-path latency against the 800 ms timeout.
- Confirm chat ranking still blends local keyword BM25 (Vanta scores augment, not
  replace) and that results are intersected with owner-filtered `memory.json` ids.
- Force a 429 and a 5xx; confirm immediate fail-open to keyword.
- **Gate:** p95 search latency within an agreed budget; keyword always present;
  error responses fall open.

### Step 4 — Bulk-import behavior with `INDEX_ON_BULK=false`
Run the Obsidian sync (`tools/obsidian_sync`, default deterministic, no-LLM) and
confirm:
- Per-entry Vanta upserts are **skipped** during the bulk import.
- Every entry is still written to `memory.json` first (writes never fail because
  Vanta is involved).
- A single `rebuild()` after the import reconciles the Vanta index.
- Memory Tidy remains paused throughout (the `X-Odysseus-Bulk-Import` guard still
  suppresses `memory_added`).
- **Gate:** no per-entry upserts during bulk; `memory.json` writes all succeed;
  one rebuild reconciles; Tidy untouched.

### Step 5 — Failure drills
With Vanta enabled, inject faults and assert graceful degradation:
- Kill Vanta mid-session → reads fall open to keyword; no chat error.
- Expire/rotate the key (401) → provider marks unhealthy, logs status only (never
  the key), routes to local.
- Force a timeout → fail open; provider recovers on the next successful `/health`.
- **Gate:** no chat-facing errors, no write failures, automatic recovery.

### Step 6 — Decision + rollback
- **Keep on:** document a runbook (`docs/runbooks/vanta-enablement.md`) covering
  config, health expectations, and reconciliation. Default committed config still
  stays OFF; enablement remains an operator env action.
- **Revert:** unset `VANTA_BRAIN_ENABLED` (Section 6). Instant; no data migration
  because Vanta is index-only.

---

## 5. Guardrails (hold for the entire phase)

- **`memory.json` remains the source of truth.** Vanta never reads or writes it;
  the provider holds no `MemoryManager` handle.
- **Vanta is index-only.** It accelerates retrieval; it can be wiped/rebuilt with
  zero data loss.
- **Memory Tidy stays paused.** No re-enabling LLM consolidation in P3.
- **Keys are env-only.** Never committed, never logged (Authorization redacted),
  never persisted.
- **Default config remains OFF.** `VANTA_BRAIN_ENABLED=false` in all committed
  files; enablement is a runtime env action for the test only.
- **No Docker, no ChromaDB, no heavy dependencies.** `httpx` (already present) is
  the only client. The stub service (Step 1) will be stdlib-only when built.
- **Upstream untouched.** All work stays in `Th3Sp3ct3R/odysseus`; no upstream
  remote, no upstream PR.

---

## 6. Rollback

Disabling Vanta is a single action with immediate effect:

1. `unset VANTA_BRAIN_ENABLED` (or set it to `false`) and restart the app.
2. The factory returns `LocalMemoryProvider`; **local keyword memory resumes
   immediately**. No reindex, no migration, no `memory.json` change.

Because Vanta is index-only, the Vanta index may be left as-is or discarded; the
authoritative 180 memories are unaffected either way.

---

## 7. Out of scope for this document

- Implementing the stub Vanta service (planned separately).
- Enabling Vanta or contacting any real service.
- Importing vault data or modifying `memory.json`.
- Any change to committed default configuration.

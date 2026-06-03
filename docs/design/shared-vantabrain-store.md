# Design (Option C): VantaBrain as a shared memory both Odysseus and Claude Code read

**Goal:** A single **VantaBrain** service that is *the* brain — digest a document
or learn a fact in **Odysseus**, and **Claude Code / Hermes** can recall it too
(and vice versa). One source both tools read/write.

**Status:** Design only. No code, no service, no enablement. Builds on the existing
contract (`vanta-brain-provider-contract.md`), provider (P2, off), and stub (P3
Step 1). Pairs with the Option B runbook (`odysseus-chat-on-free-models.md`).

---

## 1. The decision that shapes everything: index vs. source-of-truth

The current VantaBrain provider was designed as an **index** over Odysseus's
`memory.json` (facts only, `memory_id` + score; `memory.json` is canonical). For a
*shared* brain that also holds **documents/content**, that's not enough. Two paths:

| Model | VantaBrain holds | Canonical source | Implication |
|---|---|---|---|
| **A. Index-only (current)** | `memory_id` + vectors | `memory.json` (Odysseus) | Claude Code can't get real content — only ids that mean nothing to it. **Not sufficient for C.** |
| **B. Shared content store (required for C)** | full text/facts/doc chunks + metadata + vectors | **VantaBrain itself** | VantaBrain becomes a real store both tools read; Odysseus `memory.json` becomes a *client/cache*, not the sole truth. |

**The eventual shared brain wants Model B**, but **for the current phase canonicity
is NOT flipped** — see §6 (Decisions locked for now). Until telemetry, backfill, MCP
access, conflict handling, and rollback are all proven, **`memory.json` (and each
tool's local memory) stays canonical**, and VantaBrain is treated as a **shared
mirror / index layer** that augments retrieval without owning the truth. Model B is
the *target*, reached only by a deliberate, separately-approved flip — not a default
of this build.

---

## 2. Architecture

```
        ┌─────────────────────────────────────────────┐
        │            VantaBrain service                │
        │  persistent store + embeddings + content     │
        │  /health /memories/* /documents/* /search    │
        └───────────────▲───────────────▲──────────────┘
                        │ HTTP          │ MCP (tools)
            VantaBrainProvider     vanta-memory MCP server
                        │                │
                 ┌──────┴──────┐   ┌─────┴───────────────┐
                 │  Odysseus   │   │  Claude Code/Hermes │
                 │ (P2 client) │   │ (reads via MCP +/or │
                 │  + doc push │   │  ~/.claude sync)    │
                 └─────────────┘   └─────────────────────┘
```

Three workstreams, with very different "done-ness":

| Piece | Exists today | Net-new for C |
|---|---|---|
| **Contract / API shape** | ✅ `vanta-brain-provider-contract.md` | extend with `/documents/*` + content fields |
| **Real persistent service** | ⚙️ stub only (in-memory) | persistence, embeddings, content store, backups |
| **Odysseus → Vanta (facts)** | ✅ provider (P2, off) | enable (P3) + backfill the 180 |
| **Odysseus → Vanta (documents)** | ❌ | push RAG/digested docs, not just facts |
| **Claude Code → Vanta (read/write)** | ❌ **0%** | the **`vanta-memory` MCP server** — the linchpin |
| **Identity / dedup / conflict** | partial (owner) | cross-source dedup, write conflicts, provenance |

### The linchpin: `vanta-memory` MCP server
Claude Code's memory/skills are **file-based**; it cannot natively call an HTTP
brain. The clean bridge is an **MCP server** exposing VantaBrain as tools
(`vanta_search`, `vanta_remember`, `vanta_get_document`). Then **both** Odysseus and
Claude Code talk to the same brain over a standard protocol. Alternative/lighter:
a one-way **sync** that writes VantaBrain results into
`~/.claude/projects/<cwd>/memory/*.md` so Claude's *native* memory mirrors the brain
(no live calls, simpler, eventually-consistent).

---

## 3. Phased, gated plan (each phase reversible, default-off)

- **C1 — Real service.** Turn the stub into a persistent service (store + local
  embeddings + content). Loopback/Tailscale only, fake→real key in env. Keep the
  same contract so the existing provider/tests still pass. *Gate:* health, persist
  across restart, contract conformance.
- **C2 — Odysseus facts.** Enable the P2 provider against the real service
  (the existing P3 plan: stub dry run → backfill the 180 → latency/failure drills).
  *Gate:* 180 indexed, fail-open intact, `memory.json` unchanged.
- **C3 — Documents/content.** Extend Odysseus to push digested docs (RAG chunks)
  to VantaBrain `/documents/*`, and VantaBrain to store + embed them. *Gate:* a doc
  digested in Odysseus is retrievable by content from the service.
- **C4 — `vanta-memory` MCP server.** Build the MCP exposing search/recall/get.
  Stdio + loopback HTTP; key env-only. *Gate:* MCP tools return correct,
  owner-scoped results from the service.
- **C5 — Claude Code reads the brain.** Connect Claude Code to the MCP (and/or the
  `~/.claude` memory sync). *Gate:* a fact stored via Odysseus is recalled by Claude
  Code; and vice-versa.
- **C6 — Consistency & identity.** Cross-source dedup, write-conflict policy,
  provenance, owner isolation, and rebuild/reconcile of the local caches. *Gate:*
  concurrent writes converge; no cross-owner leakage.

Decision/keep-or-revert review after C5, before broad use.

---

## 4. Guardrails (whole project)
- **Default-off, fail-open.** Nothing enabled by default; every tool degrades to its
  local memory if VantaBrain is down (Odysseus → keyword; Claude Code → `~/.claude`).
- **Private only.** Service binds loopback/Tailscale, never public; behind TLS if it
  leaves the host. The brain holds personal content — treat as sensitive.
- **Keys env-only**, never committed/logged. No credentials persisted in repo.
- **`memory.json` safety.** Until C explicitly flips canonicity, `memory.json` is not
  destructively rewritten; the provider stays index-safe.
- **Memory Tidy stays paused** unless separately revisited with stricter rules.
- **No upstream.** All work stays in the fork; Claude Code integration is **additive**
  (an MCP/sync), it does not replace `~/.claude` file memory.
- **No heavy deps without review;** prefer stdlib + already-present `httpx`/fastembed.

## 5. Cost
Self-hosted service + local embeddings ≈ **$0**. C's cost is **build + ops** (a
long-running service to keep alive and back up), not dollars.

## 6. Decisions locked for now (provisional — current build phase)

These are **provisional locked decisions** for the current phase. They settle the
direction so work can proceed safely; they can be revisited deliberately later, but
until then they are the operating rules.

1. **Source of truth.** Odysseus `memory.json` remains **canonical** for now.
   VantaBrain is **not** canonical yet.
2. **VantaBrain role.** Treat VantaBrain as a **shared mirror / index / store layer
   first**. Do **not** flip canonicity until telemetry, backfill, MCP access,
   conflict handling, and rollback are all proven.
3. **Claude Code / Hermes bridge.** Start with a **read-only MCP** first. **No
   writes** from Claude Code / Hermes into VantaBrain or Odysseus until read-only
   retrieval is proven.
4. **Content scope.** Start with **facts, skills, and searchable knowledge** first.
   Full documents / content chunks come **later**, after the skill registry and MCP
   bridge are stable.
5. **Host.** Keep **loopback / local-first** for now. Tailscale / private network can
   come later. **Never public.**
6. **Vanta sync.** **No real Vanta backfill yet.** Telemetry must be added **before**
   any real sync.
7. **Hermes skills.** Do **not** ingest or mutate Hermes / Claude skills yet. First
   step is a **read-only audit and design plan**.

## 7. Remaining open questions (deferred until §6 items are proven)

The big forks (canonicity, bridge direction, content scope, host) are settled above
for this phase. What stays genuinely open — to revisit only after read-only
retrieval + telemetry are proven:

- **Telemetry shape:** what to measure before any sync (latency, hit rate, fallback
  rate, error classes) and where it's recorded.
- **MCP tool surface:** exact read-only tools (`vanta_search`, `vanta_get`, …),
  owner scoping, and result shape.
- **Conflict / dedup policy:** only relevant *if/when* canonicity is ever flipped
  (decision §6.2) — not now.
- **Embedding choice:** stay on local fastembed vs. an alternative — only when
  content/doc scope (§6.4) is taken up.

---

## 8. Relationship to Option B
B is independent and immediate (chat on free models, separate brains). C is the
later project that *unifies* the brains. Recommended order: **ship B, then build C
incrementally (C1→C6).** B also de-risks C by making Odysseus your daily driver first.

# Hermes / Claude Skills — Read-Only Audit & Ingestion Design Plan

**Status:** Audit + design only. Per the locked decision in
`shared-vantabrain-store.md` §6.7 — **do not ingest or mutate Hermes/Claude skills
yet.** This document is the required first step: a read-only audit and a design for
how a future *read-only* skill registry could feed VantaBrain. No code, no ingestion,
no mutation, no enablement.

---

## 1. Read-only audit findings (observed, not modified)

**Locations & scale**
| Location | Dirs | `SKILL.md` files | Notes |
|---|---|---|---|
| `~/.claude/skills/` | 1,944 | **1,904** | primary corpus, **392 MB** total |
| `~/.hermes/skills/` | 58 | 23 | smaller Hermes-local set |
| `~/.claude/plugins/` | 10 | (marketplace cache) | plugins distribute skills via marketplaces/cache, not flat `SKILL.md` |

**Format** — each skill is a directory `<name>/SKILL.md`, markdown with YAML
frontmatter, e.g.:
```yaml
---
name: javascript-typescript-typescript-scaffold
description: "You are a TypeScript project architecture expert ..."
risk: unknown
source: community
date_added: "2026-02-27"
---
# <body: when to use + instructions>
```

**Metadata available** (the useful, ingestible fields — frontmatter only):
- `name`, `description`, `risk`, `source`, `date_added`, plus the body and the path.

**`source` distribution** (sample of 400): mostly `community` and `gitgod`, some
`personal`/`self`, a few explicit GitHub URLs. → provenance is mostly known.

**`risk` distribution** (sample of 400): `unknown` (most), `safe`, `none`, and a
small number flagged `offensive` / `low`. → **a risk signal exists and must drive
filtering** (security/offensive skills should never be auto-exposed).

**Overlap** between `~/.claude/skills` and `~/.hermes/skills`: only **2** names
(`discord`, `firecrawl`) → de-duplication is a minor concern, but name collisions
across roots must be handled.

**Takeaways for design**
- The corpus is large (≈1,900 skills, 392 MB) but the *indexable signal* is small —
  frontmatter + a short body summary, not the full 392 MB.
- Every skill carries a `risk` and `source` field → we can filter and attribute.
- Two roots (`~/.claude`, `~/.hermes`) + plugins → the registry needs a clear
  precedence/merge rule.

---

## 2. What a future read-only ingestion would (and would not) do

**Would (read-only):**
- **Read** each `SKILL.md` frontmatter + a derived summary.
- Emit a **registry record** per skill: `{name, description, risk, source,
  date_added, root, path, summary}` — into a VantaBrain *read-only* index, so any
  tool can ask "is there a skill for X?" and get a pointer back to the file.

**Would NOT (hard rules):**
- ❌ Never modify, move, rename, or delete any `SKILL.md` or skill dir.
- ❌ Never have Hermes/Claude *write* skills via VantaBrain (per §6.3 — read-only
  bridge first; no writes until read-only retrieval is proven).
- ❌ Never auto-execute or auto-install a skill from the registry.
- ❌ Never expose `risk: offensive`/`unknown` skills without an explicit allow step.

The skill **files remain the source of truth**; the registry is a derived, rebuildable
*pointer index* — consistent with `memory.json` staying canonical (§6.1–6.2).

---

## 3. Proposed registry schema (design only)

```json
{
  "skill_id": "claude:javascript-typescript-typescript-scaffold",
  "name": "javascript-typescript-typescript-scaffold",
  "description": "…",
  "summary": "<= 1-2 lines derived from the body's 'Use this skill when'",
  "risk": "unknown",
  "source": "community",
  "root": "~/.claude/skills",
  "path": "~/.claude/skills/<name>/SKILL.md",
  "date_added": "2026-02-27",
  "owner": "growthgod"
}
```
- `skill_id` namespaced by root (`claude:` / `hermes:`) so the 2 overlaps don't clash.
- Indexed for **search/recall only**; the body stays on disk (don't bloat the index
  with 392 MB).
- `owner`-scoped like all VantaBrain records.

---

## 4. Phased plan (each gated; ingestion is NOT this phase)

- **S0 — Audit (this doc).** ✅ done: locations, format, metadata, risk/source,
  overlap, scale.
- **S1 — Registry schema + read-only extractor design.** Finalize §3 schema; design
  a pure-read extractor (parses frontmatter, derives summary). **No writes anywhere.**
- **S2 — Read-only index build (deferred).** Build the registry into VantaBrain
  (which itself is still a shared *mirror*, not canonical). Skills files untouched.
  Gate: registry count ≈ source count; spot-checks match; offensive/unknown excluded
  by default.
- **S3 — Read-only MCP exposure (deferred).** Expose `skills_search` / `skills_get`
  read-only tools so Claude Code/Hermes can *discover* skills via the shared brain —
  still no writes, no auto-install.
- **S4 — (later, separate approval).** Any write-back, auto-suggest, or content
  ingestion beyond frontmatter.

This sequencing matches §6.3 (read-only MCP first) and §6.4 (facts/skills/searchable
knowledge first, full content later).

---

## 5. Risks / edge cases
- **Scale:** ~1,900 skills — index **metadata + summary only**, never the 392 MB of
  bodies, or the index bloats and embeddings get noisy.
- **Security:** `risk: offensive`/`unknown` skills exist. Default-exclude; require an
  explicit allowlist to surface them. The registry must never trigger execution.
- **Provenance:** keep `source` so community/gitgod skills are attributable.
- **Collisions:** namespace `skill_id` by root; surface duplicates rather than
  silently merging.
- **Drift:** skills change/are added; the registry is rebuildable, so re-index rather
  than mutate in place.
- **Privacy:** the registry holds skill *metadata* only; it inherits VantaBrain's
  loopback/local-first, never-public, keys-env-only guardrails (§6.5).

---

## 6. Guardrails (this phase)
- **Read-only.** Nothing under `~/.claude/skills`, `~/.hermes/skills`, or
  `~/.claude/plugins` is modified by this work.
- **No ingestion yet.** S2+ are deferred; this document is S0 only.
- **No Hermes/Claude writes.** Bridge stays read-only.
- **Local-first, never public; keys env-only; default-off.** Inherits the shared-store
  guardrails.
- **No upstream; fork only.**

## 7. Open questions (for when S1 starts)
- Summary derivation: take the body's "Use this skill when" block, or the
  description field, or both?
- Include `~/.hermes/skills` (23) and plugin-sourced skills in v1, or `~/.claude`
  only first?
- Default risk policy: exclude `unknown` too, or only `offensive`?

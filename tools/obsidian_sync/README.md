# Obsidian → Odysseus memory sync

One-way, deterministic sync of **curated durable facts** from an Obsidian vault
into Odysseus Core Memory. Never calls an LLM. Dry-run by default.

## What it does / does not do
- **Does:** scan an allow-list of curated dirs, extract durable facts from `.md`
  (bullets, `**key**: value`) and `.jsonl` (knowledge-graph entities), dedup by
  normalized-text hash (across runs), and POST new ones to `/api/memory/add`
  (which dedups + syncs the ChromaDB vector index, no LLM).
- **Does not:** import your whole vault, import daily/ephemeral notes, call any
  LLM, write to Core Memory from `rag_source_dirs`, or expose anything publicly.

Full-vault semantic search is a **separate** concern: `rag_source_dirs` only
produces `out/rag_manifest.json` for a later Documents/RAG ingestion.

## Setup
```bash
cd tools/obsidian_sync
cp config.example.yaml config.yaml      # edit paths + username/owner
export ODYSSEUS_PASSWORD='your-odysseus-password'   # preferred over storing in yaml
python3 -m pip install --quiet requests pyyaml      # already present on most hosts
```

## Run
```bash
python3 sync_memory.py            # DRY RUN: prints report, writes out/memory_import_*.json
python3 sync_memory.py --commit   # actually import new entries
python3 sync_memory.py --max 50   # cap entries this run
```

## Config keys (config.yaml or env)
| key | env | meaning |
|---|---|---|
| `memory_source_dirs` | `MEMORY_SOURCE_DIRS` | curated dirs → Core Memory |
| `rag_source_dirs` | `RAG_SOURCE_DIRS` | manifest only, NOT memory |
| `max_memory_entries_per_run` | `MAX_MEMORY_ENTRIES_PER_RUN` | per-run cap |
| `dry_run` | `DRY_RUN` | default `true` |
| `include_dated` | `INCLUDE_DATED` | include `YYYY-MM-DD.md` (default false) |
| `odysseus_url` / `username` / `owner` | `ODYSSEUS_URL` / `ODYSSEUS_USERNAME` / `ODYSSEUS_OWNER` | target + identity |
| (password) | `ODYSSEUS_PASSWORD` | login secret — env preferred |

## Extraction rules (deterministic)
- Files: `.md`, `.jsonl`. Skips dotfiles and (by default) date-named notes.
- Skips content under ephemeral headings (Activity Log, Appointments, TODO, …).
- Markdown frontmatter `memory: false` skips a file; `category:` overrides detection.
- Quality gate: normalized length 8–400, ≥2 word tokens, drops URL-only / `x: x` noise.
- Categories: `identity | preference | fact | contact | project | goal`.

## Notes
- The generated `out/memory_import_*.json` is shaped `{text,category,source,owner}` —
  the exact shape Odysseus's in-app importer round-trips with **no LLM**, so you can
  also drag-drop it into the UI memory importer instead of `--commit`.
- Re-running is safe: the `.sync_state.json` hash ledger prevents re-imports.
- Restore/replay a prior run with `--from-artifact out/memory_import_<ts>.json`
  (skips the vault scan and imports exactly those entries).

## Operational notes — Memory Tidy / consolidation (IMPORTANT)
- **Keep the built-in "Memory Tidy" (`consolidate_memory`) task PAUSED for now.**
  It is event-triggered on `memory_added` (every 5 events) and runs an LLM
  consolidation that has been observed to over-collapse a bulk import
  (180 curated facts → 3). It is paused in `scheduled_tasks` (`status='paused'`).
- **Bulk imports must send the `X-Odysseus-Bulk-Import` header.** The server
  (`routes/memory_routes.py`) skips the `memory_added` event when it is present,
  so a large import can never trip Memory Tidy — even if the task is later
  re-enabled. This sync script sends the header automatically on every `--commit`.
- **Do NOT re-enable LLM consolidation until stricter rules exist** (e.g. a hard
  cap on how many entries a single tidy run may remove/merge, a dry-run/preview
  step, and per-category protection). Re-enabling without those risks silent
  data loss on the next bulk add. Regression coverage lives in
  `tests/test_memory_bulk_import_guard.py`.

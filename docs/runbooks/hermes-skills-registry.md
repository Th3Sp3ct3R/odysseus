# Runbook — Hermes / Claude Skills Read-Only Registry (S1/S2)

A **read-only** metadata index over the on-disk skill libraries. It answers
"what skills exist, where, and which are safe to expose?" without ever
modifying, deleting, or executing a skill, and without ingesting full skill
bodies into `memory.json`.

- Code: `services/skill_registry/`
- Tests: `tests/test_skill_registry.py`
- Cache (gitignored): `data/skill_registry.json`

## What it scans

| Root source | Path | Typical frontmatter |
|-------------|------|---------------------|
| `claude` | `~/.claude/skills` | `name`, `description`, `risk`, `source`, `date_added` |
| `hermes` | `~/.hermes/skills` | `name`, `description`, `category`, `tags` (usually **no** `risk`) |

The scanner walks each root for `SKILL.md` files and records **metadata only**
per skill:

`name`, `description` (bounded summary), `risk`, `source`, `date_added`,
`root` (claude/hermes), `path`, `category` (inferred from path), `modified`
(mtime), `content_hash` (sha256 of the file), `exposed` (computed), and
`frontmatter_enabled` (raw `enabled`/`exposed` flag if the skill declares one).

There is **no `body` or `content` field** — by design and asserted by tests.

## Summary rules (no LLM)

1. Use the frontmatter `description` when present.
2. Otherwise derive a deterministic fallback from the body: the first markdown
   heading + the first meaningful paragraph (code fences, bullets, and HTML
   comments are skipped).
3. Always truncated to ≤ `MAX_SUMMARY_CHARS` (280) at a word boundary.

No model is ever called; the same input always yields the same summary.

## Risk / exposure policy (S1, conservative)

```
if risk in {offensive, critical}:   exposed = False   # S1 dangerous tier — hard-blocked
elif risk in {safe, none}:          exposed = True    # auto-exposed
else:                               exposed = (skill is allowlisted)
```

- `safe` / `none` → **exposed**.
- `unknown`, `low`, **or a missing risk field** (normalized to `unknown`) →
  **disabled metadata only**, unless explicitly allowlisted.
- `offensive` / `critical` → **S1 dangerous tier**: disabled and **cannot be
  exposed via the allowlist**. `offensive` is the spec-mandated hard-block;
  `critical` is included as a conservative extension of the same dangerous
  class. Exposing these would require an explicitly-approved future
  *dangerous-mode* policy (out of scope for S1).
- Hermes skills carry no `risk`, so they default to `unknown` → disabled until
  reviewed and allowlisted.

## Allowlist

The allowlist is the only way to expose a non-safe (but non-dangerous) skill.
It is an explicit, operator-curated override.

- **Default file (optional):** `data/skill_registry_allowlist.json` — gitignored,
  never committed. The scanner works fine with **no** allowlist file present.
- **Resolution order at `build`:** explicit `--allowlist <file>` wins; otherwise
  the default file is auto-loaded if it exists; otherwise the allowlist is empty.
- **Entry forms** (mix freely):

  ```json
  [
    "typescript-scaffold",                 // by skill_id / name
    { "skill_id": "bland" },               // explicit skill_id
    { "name": "jq-helper" },               // alias for skill_id
    { "path": "/Users/me/.hermes/skills/bland/SKILL.md" }  // by exact path
  ]
  ```

  An object form `{ "allowlist": [ ... ] }` is also accepted. `~` in path
  entries is expanded.

- **Hard rule:** allowlisting an `offensive`/`critical` skill has **no effect** —
  it stays disabled in S1.

```bash
# Expose two reviewed Hermes skills
echo '["bland", {"skill_id":"jq-helper"}]' > data/skill_registry_allowlist.json
python -m services.skill_registry build          # auto-loads the file
python -m services.skill_registry list --source hermes --exposed
```

## Usage

```bash
# Build the local cache from both roots (read-only scan)
python -m services.skill_registry build

# Build with an explicit allowlist of skill names
python -m services.skill_registry build --allowlist allow.json
# allow.json: ["typescript-scaffold", "jq-helper"]   (or {"allowlist": [...]})

# List exposed skills from one source
python -m services.skill_registry list --source hermes --exposed

# Search text + filter by risk / source / category / exposure
python -m services.skill_registry search --query voice --risk safe
python -m services.skill_registry search --disabled --source hermes
```

Programmatic:

```python
from services.skill_registry import build_registry, search_skills, save_registry

reg = build_registry()                         # scans DEFAULT_ROOTS, read-only
save_registry(reg)                             # writes data/skill_registry.json
hits = search_skills(reg, query="voice", exposed=True)
```

## Safety invariants (enforced by tests)

- Scanner never writes to skill files — bytes and mtimes are unchanged after a
  scan (`test_scanner_does_not_modify_skill_files`).
- No full skill body is stored anywhere in the cache
  (`test_no_full_body_stored`, `test_skillmeta_fields_are_metadata_only`).
- Unknown / offensive / missing-risk skills are disabled by default
  (`test_unknown_and_offensive_disabled_by_default`).
- The cache path `data/skill_registry.json` is gitignored
  (`test_registry_cache_path_is_gitignored`).
- The registry does **not** touch `memory.json`, Memory Tidy, or Vanta. It is a
  standalone read-only index and never triggers memory consolidation.

## Operational notes

- The cache is a disposable local artifact — delete it and rebuild any time.
- `build_registry()` is deterministic (no timestamp); `save_registry()` stamps
  `generated_at` at write time only.
- Re-run `build` after adding/updating skills to refresh the index. Newly added
  Hermes skills appear as `disabled` until they declare `risk: safe`/`none` or
  are added to the allowlist.

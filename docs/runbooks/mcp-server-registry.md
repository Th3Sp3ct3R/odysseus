# Runbook: Config-backed MCP Server Registry

Odysseus can load known MCP servers from a **config file** instead of relying on
manual UI entry. This registry is **read/status only** — it shows what's configured
and whether each server looks runnable. It does **not** start servers and never
exposes secrets.

## Where things live
- **Manual servers (existing):** stored in the DB (`mcp_servers` table), managed by
  the admin UI → `POST/GET/PATCH/DELETE /api/mcp/servers` (`routes/mcp_routes.py`,
  `src/mcp_manager.py`). These are **started** on app startup (`connect_all_enabled`).
- **Config-backed registry (new):** `config/mcp_servers.local.json` (preferred,
  gitignored) → falls back to `config/mcp_servers.example.json`. Read by
  `src/mcp_registry.py`, exposed via `routes/mcp_registry_routes.py`. **Display/status
  only — not started.**

## Config format
```json
{
  "mcpServers": {
    "<name>": {
      "transport": "stdio",
      "command": "hermes",
      "args": ["mcp", "serve"],
      "env": { "HERMES_HOME": "/Users/growthgod/.hermes" },
      "enabled": true,
      "description": "..."
    }
  }
}
```
- **Never put secret VALUES in `env`.** Use env var names here and set real values in
  the host environment. The registry returns only env **key names**, never values.
- **Filesystem scope must be narrow.** Use project dirs
  (`/Users/growthgod/Desktop/VAN/odysseus`, `/Users/growthgod/.hermes/skills/creative`),
  never the home root `/Users/growthgod` (the registry flags that as too broad).

## Endpoints (admin-only)
- `GET /api/mcp/registry` → `{ source, source_kind, servers:[{name, enabled, transport,
  command_present, args_count, env_keys, description, status, source, warnings}], warnings, started:false }`
  - `status` ∈ `configured | missing_command | disabled | invalid`
- `POST /api/mcp/registry/reload` → re-reads the config from disk and returns the same
  view. **Does not start servers.**

## Enable / disable a server
Edit the config file and set `"enabled": true|false`, then click **Reload MCP Config**
(or `POST /api/mcp/registry/reload`). Enabling here only marks it as *intended*; it is
not auto-started by this registry. (Auto-start remains the DB-backed path's job.)

## Why manual add may have been failing
- `POST /api/mcp/servers` is **admin-only** (`require_admin`). If your session isn't
  authenticated as admin, the add returns 401/403 and nothing is saved.
- It saves a DB row **and** immediately attempts a stdio connection; if the command
  isn't on PATH the row may save but show as disconnected/errored (looks like "not added").
- The config-backed registry sidesteps this for *known* servers: define them in
  `mcp_servers.local.json`, see accurate status, and fix `missing_command`/scope issues
  before they ever start.

## Frontend
Admin → MCP Servers shows a **Config-backed MCP servers** panel above the manual list:
per-server status, source (`config`), enabled/disabled, broad-scope ⚠ warnings, a
macOS-desktop-enabled ⚠ warning, and a **Reload MCP Config** button. No live
start/stop buttons are added (the registry doesn't manage lifecycle).

## Hermes labels
- Current Hermes MCP = **"Hermes messaging MCP — conversations, messages, events, approvals."**
- Future placeholder shown in the UI: **"Hermes media execution MCP — not wired yet."**

## Security
- No secrets in tracked files; `config/mcp_servers.local.json` is gitignored.
- env **values** are never read into the view or returned by the API.
- The registry performs no network/process starts.

## Remaining step — Hermes media execution MCP (later)
The media skills (`~/.hermes/skills/creative/*`) are not yet exposed as an MCP server.
To wire them: build a Hermes "media execution" MCP server that exposes
`route_media_task` / `fal_*` / `ppq_*` / `execute_reel_manifest` as MCP tools, add it
to `mcp_servers.local.json` (`enabled: false` until approved), then start it through
the DB-backed lifecycle. Until then the UI shows the "not wired yet" placeholder.

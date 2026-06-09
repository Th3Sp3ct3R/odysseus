# Safe Email MCP Wrapper Design

**Status:** Design Only — Not Implemented  
**Owner:** Growth God / Vanta Labs  
**Date:** 2026-06-03  
**Related:** `odysseus/mcp_servers/email_server.py`

## Goals

Create a hardened, auditable MCP wrapper around the existing Odysseus email server that can be safely exposed to Hermes (and other agents) while minimizing risk of accidental or malicious email actions.

## Core Principles

- **Default Deny** — Everything is read-only or draft-only unless explicitly enabled.
- **Explicit Confirmation** — Any write action requires human approval.
- **Least Privilege** — Restrict accounts, folders, and actions via allowlist.
- **Full Auditability** — Every tool call is logged with context.
- **No Public Exposure** — This wrapper must never be reachable over the network without strong auth.

## Proposed Architecture

```
Hermes (or other MCP client)
        ↓ (stdio / local only)
safe-email-mcp-wrapper (Python MCP server)
        ↓ (controlled calls)
odysseus email_server.py  (original, unmodified)
        ↓
IMAP / SMTP
```

The wrapper acts as a **policy enforcement layer** in front of the real email server.

## Required Modes

| Mode                  | Default | Description |
|-----------------------|---------|-----------|
| `read-only`           | ON      | Only listing, searching, and reading emails |
| `draft-only`          | ON      | Outbound actions create drafts only (never send) |
| `live-send`           | OFF     | Allows actual SMTP delivery (requires confirmation) |
| `attachment-download` | OFF     | Disabled by default |
| `delete-enabled`      | OFF     | No delete or permanent delete |
| `bulk-enabled`        | OFF     | No bulk actions |

## Tool Surface (Proposed)

### Always Available (Read-only)

- `list_email_accounts` (filtered by allowlist)
- `list_emails`
- `search_emails`
- `read_email`
- `list_folders` (new)

### Draft-only (when enabled)

- `create_draft` (new)
- `reply_to_draft` (new)

### Write Actions (require explicit confirmation + mode enabled)

- `send_email` → only when `live-send=true`
- `reply_to_email` → only when `live-send=true`
- `archive_email`
- `mark_email_read`
- `delete_email` → permanently disabled in this wrapper

### Disabled / Removed

- `bulk_email`
- `download_attachment` (unless explicitly enabled per-account)

## Configuration

Example `safe-email-mcp.yaml`:

```yaml
email:
  default_mode: read-only
  allowed_accounts:
    - name: "personal"
      folders: ["INBOX", "Archive"]
    - name: "work"
      folders: ["INBOX"]

  modes:
    draft_only: true
    live_send: false
    allow_attachments: false
    allow_delete: false
    allow_bulk: false

  audit:
    log_path: ~/.hermes/logs/safe-email-mcp.log
    redact_bodies: true
    redact_credentials: true

  confirmation:
    require_for: ["send", "reply", "archive", "delete"]
    timeout_seconds: 300
```

## Security Controls

1. **Read-only by default** — All tools default to read operations.
2. **Draft-only outbound** — `send_email` and `reply_to_email` are replaced with draft creation unless `live-send` is explicitly enabled.
3. **No delete** — `delete_email` and `bulk_email` actions are completely removed or return "disabled".
4. **Attachment sandbox** — Downloads only allowed to a designated safe directory with size limits.
5. **Credential redaction** — Never log IMAP/SMTP passwords or tokens.
6. **Body redaction** — Email bodies are truncated or hashed in audit logs unless `redact_bodies: false`.
7. **Account/folder allowlist** — Hardcoded or config-driven list of permitted accounts and folders.
8. **Audit logging** — Every tool call records: timestamp, account, tool name, parameters (redacted), result, actor.
9. **Transport restriction** — Only stdio transport. No HTTP/SSE exposure by default.
10. **Confirmation gate** — Write actions trigger a confirmation flow (via Hermes clarify tool or terminal prompt).

## Implementation Phases (Future)

- **Phase 0**: Design + threat model (current)
- **Phase 1**: Read-only wrapper with audit logging
- **Phase 2**: Add draft-only mode + confirmation system
- **Phase 3**: Controlled live-send with per-account approval
- **Phase 4**: Integration tests + red-team review

## Open Questions

- Should the wrapper live inside the Odysseus repo or as a standalone Hermes skill?
- How should confirmation be handled when used via gateway (Telegram/Discord)?
- Should we support per-tool permissions instead of global modes?

## References

- Original unsafe server: `mcp_servers/email_server.py`
- Hermes MCP commands: `hermes mcp add`, `hermes mcp configure`
- Related skills: `hermes-agent`

---

**Do not implement until this design is reviewed and approved.**
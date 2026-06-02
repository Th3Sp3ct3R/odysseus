# Runbook: Vanta STUB harness (P3 Step 1 dry run)

A safe, local-only way to exercise the entire Vanta enablement flow **without a
real Vanta Brain service**. Proves provider selection, health, the stub
round-trip, and fail-open — while `data/memory.json` and Memory Tidy stay
untouched.

> This is a TEST harness. It does not connect to any real service, imports no
> vault data, writes nothing to `data/memory.json`, and leaves committed
> defaults at `VANTA_BRAIN_ENABLED=false`.

## Components
- `tools/vanta_stub/stub_service.py` — dependency-free (stdlib `http.server`)
  mock implementing the contract endpoints over an in-memory index. Binds
  **127.0.0.1 only** and requires a **fake** Bearer key.
- `tools/vanta_stub/harness.py` — orchestrates the dry run (Phases 1–3).
- `tests/test_vanta_stub.py` — pytest coverage of stub + provider + fallback.

## Endpoints (stub)
`GET /health` · `POST /memories/upsert` · `POST /memories/search` (returns
`[{memory_id, score}]`) · `DELETE /memories/{id}` · `POST /memories/rebuild` ·
`GET /memories/count`. All require `Authorization: Bearer <fake key>`.

## Run the automated dry run
```bash
cd /path/to/odysseus
.venv/bin/python tools/vanta_stub/harness.py
```
It will, in order:
1. start the stub on an ephemeral loopback port with a fake key;
2. **Phase 1** — drive the real `VantaBrainProvider` against it
   (health / upsert / search shape / count / auth);
3. **Phase 2** — boot Odysseus with `VANTA_BRAIN_*` pointed at the stub and
   confirm the factory selects `FallbackProvider(Vanta, local)`, that the stub
   received `GET /health` and the startup rebuild/count, and that
   `memory.json` is unchanged (read-only push of the 180);
4. **Phase 3** — kill the stub and confirm fail-open to local keyword and that
   the live app still responds;
5. assert `memory.json` stayed at 180 and Memory Tidy stayed `paused`.

Exit code `0` = all checks passed.

## Run the stub standalone (optional, manual poking)
```bash
.venv/bin/python tools/vanta_stub/stub_service.py --host 127.0.0.1 --port 8200 --key fake-key
# then, in another shell:
curl -s -H 'Authorization: Bearer fake-key' http://127.0.0.1:8200/health
curl -s -H 'Authorization: Bearer fake-key' http://127.0.0.1:8200/memories/count
```

## Run the tests
```bash
.venv/bin/python -m pytest tests/test_vanta_stub.py -v
```

## Safety / guardrails
- Stub binds **loopback only** (constructing it with a non-loopback host raises).
- Uses a **fake** API key passed at startup; no real secret involved, nothing
  persisted.
- The harness performs **no API memory writes**; the only `memory.json` access is
  the app's startup `rebuild` which **reads** it to push into the stub index.
- Committed default remains `VANTA_BRAIN_ENABLED=false`; the harness sets the env
  only for its own subprocess.

## Rollback
Nothing to roll back — the harness is ephemeral. To disable Vanta in any real
run, unset `VANTA_BRAIN_ENABLED` (or set `false`); the factory returns the local
provider and keyword memory resumes immediately.

# CLAUDE.md

Guidance for Claude Code (and humans) working in the Odysseus repo. Read this
before adding or moving code.

## What this is

Odysseus is a **FastAPI** web application (Python, async). `app.py` at the repo
root is a slim orchestrator: it builds the `FastAPI` app, wires middleware, and
calls a `setup_*_routes()` function from each module in `routes/`. The frontend
is server-rendered/static assets under `static/`.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate   # .venv/ already exists here
pip install -r requirements.txt
python setup.py                                      # creates data dirs, prints initial admin password
python -m uvicorn app:app --host 0.0.0.0 --port 7000
```

Open `http://localhost:7000` and log in with the generated admin password.
Config is read from `.env` (see `.env.example`) plus the in-app **Settings**
panel — defaults work out of the box.

## Test it

```bash
pytest                       # full suite (testpaths=tests, asyncio_mode=auto)
pytest tests/test_research_v2.py -q   # single file
```

Config lives in `pyproject.toml` (`[tool.pytest.ini_options]`). Tests use
`pytest-asyncio` in auto mode — no need to mark coroutines. Shared fixtures are
in `tests/conftest.py`. Add a `tests/test_<module>.py` for new modules.

## Code layout — where things go

Four Python roots, each with a distinct job. Match the existing one; don't
invent new top-level dirs.

| Dir         | Purpose                                                                 | Import as            |
|-------------|-------------------------------------------------------------------------|----------------------|
| `core/`     | Framework plumbing: `auth`, `database`, `middleware`, `models`, `session_manager`, `atomic_io`, `exceptions`, `constants` | `from core.X import` |
| `src/`      | **Application logic / handlers / business code** (~80 modules)          | `from src.X import`  |
| `services/` | Self-contained subsystems: `search`, `memory`, `research`, `tts`, `stt`, `skill_registry`, `docs`, `faces`, `youtube`, `shell`, `hwfit` | `from services.X import` |
| `routes/`   | HTTP route wiring (~48 files). Each exposes `setup_*_routes(app, ...)` called from `app.py` | `from routes.X import` |

**Adding a feature, the standard path:**
1. Put the logic in `src/` (a handler/manager module) — or a `services/`
   subpackage if it's a self-contained subsystem with its own internals.
2. Expose it over HTTP by adding/extending a `routes/<name>_routes.py` with a
   `setup_<name>_routes()` function.
3. Import and call that `setup_*` in `app.py` alongside the others.
4. Add a `tests/test_<name>.py`.

Conventions seen across the codebase:
- Every module: `logger = logging.getLogger(__name__)`.
- Pydantic models for request/response shapes (`src/request_models.py`).
- SQLAlchemy via `core/database.py` (`SessionLocal`). Runtime data lives in `data/`.
- Optional heavy deps degrade gracefully (e.g. RAG falls back to keyword search
  if `chromadb-client`/`fastembed` are missing). Follow that pattern for new
  optional dependencies.

## Observability / telemetry

There is **no formal telemetry** — no OpenTelemetry, Prometheus, StatsD, or
Datadog, and no metrics/tracing exporter. What exists instead:

- **stdlib `logging`** throughout; log files under `logs/`.
- **`src/assistant_log.py`** — `log_to_assistant()` surfaces events into the
  assistant's unified chat activity feed. Use this for user-visible events.
- **`src/event_bus.py`** — lightweight in-process event bus for automation
  triggers (session created, message sent, etc.).
- **`services/search/analytics.py`** — search usage analytics + caching.

If you add real metrics/tracing, document it here.

## Gotchas

- **Migration in progress, `src/` → `services/`.** Parallel implementations
  exist and have diverged — both are live:
  - `src/search/` (used by `src/app_initializer`, `chat_processor`,
    `research_handler`, `tool_execution`, `deep_research`) vs `services/search/`
    (used by `routes/search_routes`, `services/research/research_handler`).
    Only `analytics.py`, `cache.py`, `query.py`, `ranking.py` match; `__init__`,
    `content`, `core`, `providers` differ; `services/search` adds `service.py`.
  - `src/research_handler.py` vs `services/research/research_handler.py` —
    different contents.
  Don't "dedupe" by deleting a file — packages import their own submodules
  (e.g. `src/search/__init__` imports `.analytics`). Reconciling requires
  picking the canonical package and repointing importers, with tests.
- Don't commit `*.bak` files.
- Node deps (`package.json`) exist for tooling only (`@anthropic-ai/sdk`,
  `puppeteer-core`, Antithesis `bombadil`); the app itself is Python.

## Docs

Root: `README.md` (full overview + quickstart), `ROADMAP.md`, `CONTRIBUTING.md`,
`SECURITY.md`. Design docs live in `docs/` (incl. `docs/design/`).

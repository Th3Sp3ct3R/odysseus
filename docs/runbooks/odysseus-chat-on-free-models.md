# Runbook (Option B): Odysseus as daily chat on free models, Claude Code for code

**Goal:** Use **Odysseus** (self-hosted) as your daily AI chat / memory / docs /
research front-end on **OpenRouter free models**, and keep **Claude Code / Hermes**
for software engineering. Two tools, clean split, near-zero inference cost.

**Status:** Runbook only — operator steps. No code, no committed default changes,
no Vanta enablement. Pairs with `docs/design/shared-vantabrain-store.md` (Option C,
the later "one shared brain" project).

---

## What you get vs. give up
- **Cost:** Odysseus side ≈ **$0** (free models + local embeddings + local search).
- **Capability:** great for chat/notes/research; free models are **rate-limited and
  weaker** at hard reasoning. Coding stays on Claude Code (unchanged quality).
- **Memory:** Odysseus keeps its own `memory.json` (keyword retrieval). This is a
  **separate brain** from Claude Code's `~/.claude` memory — by design in Option B.

---

## Prerequisites
- Odysseus running local-safe (see the manual-run command below) on
  `127.0.0.1:7001`, auth on, `VANTA_BRAIN_ENABLED` unset/false.
- An **OpenRouter account + API key**. Free models still require a key, but their
  inference is $0. The key is **env/Settings only — never committed, never logged**.

---

## Steps

### 1. Start Odysseus (local-safe)
```bash
cd ~/Desktop/VAN/odysseus
env -u VANTA_BRAIN_ENABLED -u VANTA_BRAIN_BASE_URL -u VANTA_BRAIN_API_KEY \
  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7001
```
Confirm: `http://127.0.0.1:7001/api/auth/status` → 200; LAN IP refused.

### 2. Add OpenRouter as a provider (in-app, not committed)
In **Settings → providers / model endpoints**, add an endpoint:
- Base URL: `https://openrouter.ai/api/v1`
- API key: your OpenRouter key (stored in app settings/keychain, not in git)
- It is OpenAI-compatible, so Odysseus's model discovery will list its models.

> Prefer adding the key in the app rather than `.env`. If you must pre-seed it,
> put it in `.env` (gitignored) — never in `.env.example` or any committed file.

### 3. Pick free model(s) as default
- In the chat model picker, select an OpenRouter **`:free`** model as your default.
- Set a couple of fallbacks (free models rate-limit; a fallback chain avoids dead
  ends). Odysseus supports `default_model_fallbacks` in settings.
- **Verify current free models + daily limits on OpenRouter before relying on them**
  — the free roster and caps change frequently.

### 4. Keep retrieval local + free
- Embeddings: Odysseus already uses **local fastembed (ONNX)** — free, no API.
- Web search: **SearXNG** (bundled) is local + free. Do **not** wire paid search
  (Brave/Serper/Tavily) unless you accept those per-call costs.
- Memory stays keyword (ChromaDB/semantic is optional and out of scope here — no
  Docker per current constraints).

### 5. Keep Claude Code for engineering — decide billing
- Real code work stays in Claude Code / Hermes.
- **Billing choice:**
  - Heavy coding → keep **Claude Max ($200/mo)**.
  - Light/occasional coding → consider **API pay-as-you-go** (pay per token only
    when you code), which can land **under $200/mo**.
- Either way, moving daily *chat* off Claude onto Odysseus is the lever that lets
  you reconsider the subscription.

---

## Cost watch-list (keep it ~$0)
| Item | Free? |
|---|---|
| OpenRouter `:free` models | yes (rate-limited) |
| Local fastembed embeddings | yes |
| SearXNG search | yes |
| Self-hosting on your Mac | yes |
| **Paid OpenRouter models** | ❌ per-token — avoid unless intended |
| **Paid search APIs** | ❌ per-call — leave off |
| **Claude Code** | separate (your billing choice) |

There is **no inherent monthly floor** — fully-free + self-hosted is ~$0.

---

## Guardrails
- Bind **127.0.0.1 only**; `AUTH_ENABLED=true`; `LOCALHOST_BYPASS=false`.
- API keys **env/Settings only**; never committed, never logged.
- `VANTA_BRAIN_ENABLED` stays **false** (Option B does not use Vanta).
- No Docker, no ChromaDB, no heavy deps required.

## Rollback
- Switch the default model back to your previous provider, or remove the OpenRouter
  endpoint in Settings. No data migration — chat history and memory are unaffected.

---

## Expectations (be honest with yourself)
- Free models: good enough for chat, summarizing, notes, light research. They will
  feel **slower, rate-limited, and weaker** on complex reasoning than Claude.
- This runbook **does not** make Odysseus and Claude Code share memory. That's
  Option C — a separate, larger project.

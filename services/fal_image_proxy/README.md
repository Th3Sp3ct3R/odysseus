# fal image proxy

A tiny local FastAPI service that lets Odysseus use **fal.ai** image models through
the **OpenAI Images** interface Odysseus already speaks
(`POST /v1/images/generations`). It translates OpenAI ⇄ fal and returns the
OpenAI response shape.

```
Odysseus ──OpenAI /v1/images/generations──▶ fal_image_proxy ──fal API──▶ fal.ai
```

## Endpoints
- `GET  /health` → `{"status":"ok","mode":"dry-run|live"}`
- `POST /v1/images/generations` → OpenAI request in, `{"created":…,"data":[{"url":…}]}` out

## Config (environment only)
| Var | Default | Meaning |
|---|---|---|
| `FAL_KEY` | _(empty)_ | Your fal API key. Read only from env, sent only in the outbound `Authorization: Key …` header, **never logged**. |
| `FAL_DEFAULT_MODEL` | `fal-ai/flux/dev` | Used when a request omits `model`. |
| `FAL_PROXY_TIMEOUT` | `120` | Outbound fal timeout (seconds). |
| `FAL_PROXY_DRY_RUN` | `true` | **Default true** → no fal calls; returns a mock URL. Set `false` for live. |

Copy `.env.example` → `.env` and fill in `FAL_KEY` **yourself**. `.env` is gitignored.

## Run (dry-run by default)
```bash
scripts/run-fal-image-proxy.sh           # http://127.0.0.1:7010, dry-run
```
Health check:
```bash
curl http://127.0.0.1:7010/health
```

## Models
Centralized in `MODEL_MAP` (`app.py`). Supported out of the box:
- `fal-ai/flux/dev`
- `fal-ai/recraft/v3`

Any other `fal-ai/...` id is passed through, so new fal models work without code
changes. Non-fal ids return a clear `400` error.

## Size handling
OpenAI size → fal `image_size` enum (works for flux + recraft):
| OpenAI | fal |
|---|---|
| `1024x1024` | `square_hd` |
| `1024x1792` | `portrait_16_9` |
| `1792x1024` | `landscape_16_9` |
| anything else | `square_hd` (fallback) |

## `n` (multiple images)
`n` maps to fal's `num_images=n` (flux + recraft support it). Each returned fal
image becomes one OpenAI `data[]` entry. No internal looping.

## `quality`
Accepted for OpenAI compatibility but **not forwarded** (fal flux/recraft don't
take it).

## Wiring Odysseus to the proxy
1. Start the proxy (above).
2. In Odysseus **Settings → endpoints**, add an OpenAI-compatible endpoint:
   - **Base URL:** `http://127.0.0.1:7010/v1`
   - **Type:** image
   - **API key:** any non-empty placeholder (the *proxy* holds the real fal key, not Odysseus)
3. Set `image_model` to one of:
   - `fal-ai/flux/dev`
   - `fal-ai/recraft/v3`
4. Generate an image. While `FAL_PROXY_DRY_RUN=true` you'll get a mock URL — flip
   to `false` (with `FAL_KEY` set) for real images.

## Security
- The fal key is never logged and never returned in responses.
- Dry-run makes zero network calls.
- Live mode with a missing `FAL_KEY` fails with a clear error and no network call.

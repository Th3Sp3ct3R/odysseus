# fal video proxy

A small local FastAPI service that exposes an **OpenAI-style video generation**
endpoint and translates to **fal.ai** video models (Veo, Kling, Luma, …). Sibling
of `fal_image_proxy`, for video.

```
caller ──POST /v1/videos/generations──▶ fal_video_proxy ──fal API──▶ fal.ai (mp4 url)
```

Supports **text-to-video** (prompt only) and **image-to-video** (prompt + `image_url`).

## Endpoints
- `GET  /health` → `{"status":"ok","mode":"dry-run|live"}`
- `POST /v1/videos/generations` → `{"created":…,"data":[{"url":"…mp4"}]}`

Request fields: `prompt` (req), `model`, `n`, `size` or `aspect_ratio`, `duration`
(seconds), `image_url` (→ image-to-video).

## Config (environment only)
| Var | Default | Meaning |
|---|---|---|
| `FAL_KEY` | _(empty)_ | fal API key. Env only; sent only in the outbound header; **never logged**. Shared with the image proxy. |
| `FAL_VIDEO_DEFAULT_MODEL` | `fal-ai/veo3` | Used when a request omits `model`. |
| `FAL_VIDEO_PROXY_TIMEOUT` | `600` | Outbound fal timeout (s) — video is slow. |
| `FAL_VIDEO_PROXY_DRY_RUN` | `true` | **Default true** → no fal calls; returns a mock mp4 URL. Set `false` for live. |

Copy `.env.example` → `.env` and add `FAL_KEY` **yourself**. `.env` is gitignored.

## Run (dry-run by default)
```bash
scripts/run-fal-video-proxy.sh          # http://127.0.0.1:7011, dry-run
curl http://127.0.0.1:7011/health
```

## Models
Centralized in `MODEL_MAP` (`app.py`). Known ids resolve to the right t2v/i2v
variant automatically:
- `fal-ai/veo3`
- `fal-ai/kling-video/v3/pro`

Any other `fal-ai/...` id or full path (e.g. `fal-ai/kling-video/v3/pro/image-to-video`)
passes through. Non-fal ids return a clear `400`.

## Aspect / size
`size` or `aspect_ratio` → fal `aspect_ratio`:
| input | fal |
|---|---|
| `1920x1080`, `1280x720`, `16:9` | `16:9` |
| `1080x1920`, `720x1280`, `9:16` | `9:16` |
| `1024x1024`, `1:1` | `1:1` |
| anything else | `16:9` (fallback) |

## `n` (multiple clips)
Most fal video models return **one clip per call**, so `n` is handled by **looping
n calls** in live mode (each appended to `data[]`). Dry-run returns `n` mock URLs.
Note: each clip is a **separate paid generation**.

## Wiring (when ready)
Add an OpenAI-compatible **video** endpoint in whatever consumes it, base URL
`http://127.0.0.1:7011/v1`, and pick a model id above. (Odysseus core has no native
video path yet; this proxy is the building block for the reel pipeline's video stage.)

## Security
- fal key never logged / never returned.
- Dry-run makes zero network calls.
- Live mode with missing `FAL_KEY` fails clearly with no network call.
- Video is **paid + slow** (30s–several min/clip) — keep dry-run until ready.

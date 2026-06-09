"""
fal_video_proxy — OpenAI-style video-generation adapter in front of fal.ai.

Sibling of fal_image_proxy, for VIDEO. Exposes an OpenAI-ish video endpoint and
translates to fal's video models (Veo, Kling, Luma, etc.):

    caller ──POST /v1/videos/generations──▶ this proxy ──fal API──▶ fal.ai  (mp4 url)

Supports both text-to-video (prompt only) and image-to-video (prompt + image_url).

Security & safety (identical posture to the image proxy):
  • FAL_KEY is read ONLY from env, sent only in the outbound Authorization header,
    and is NEVER logged.
  • DRY-RUN by default (FAL_VIDEO_PROXY_DRY_RUN=true): NO fal calls; returns a mock
    mp4 URL and logs the translated fal payload so you can verify the mapping.
  • If dry-run is off and FAL_KEY is missing, requests fail safely (HTTP 500, clear
    message) with no network call.

Run:  scripts/run-fal-video-proxy.sh   (defaults to 127.0.0.1:7011, dry-run)
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("fal_video_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="fal video proxy", version="1.0.0")

# --------------------------------------------------------------------------- #
# Centralized model + aspect mapping. Each known model declares its t2v / i2v
# fal paths. Any other `fal-ai/...` id is passed through unchanged.
# --------------------------------------------------------------------------- #

MODEL_MAP: dict[str, dict[str, str]] = {
    "fal-ai/veo3": {
        "t2v": "fal-ai/veo3",
        "i2v": "fal-ai/veo3/image-to-video",
    },
    "fal-ai/kling-video/v3/pro": {
        "t2v": "fal-ai/kling-video/v3/pro/text-to-video",
        "i2v": "fal-ai/kling-video/v3/pro/image-to-video",
    },
}

# OpenAI size string OR aspect_ratio -> fal `aspect_ratio`.
ASPECT_MAP: dict[str, str] = {
    "1920x1080": "16:9", "1280x720": "16:9", "16:9": "16:9",
    "1080x1920": "9:16", "720x1280": "9:16", "9:16": "9:16",
    "1024x1024": "1:1", "1:1": "1:1",
}
DEFAULT_ASPECT = "16:9"
FAL_BASE = "https://fal.run"


class VideoRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    n: int = 1
    size: Optional[str] = None          # OpenAI-style "1920x1080"
    aspect_ratio: Optional[str] = None  # or pass "16:9" directly
    duration: Optional[int] = None      # seconds; fal model decides if unset
    image_url: Optional[str] = None     # if present → image-to-video

    model_config = {"extra": "ignore"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def get_config() -> dict[str, Any]:
    """Read config fresh per request so tests can set env via monkeypatch."""
    return {
        "fal_key": os.getenv("FAL_KEY", "") or "",
        "default_model": os.getenv("FAL_VIDEO_DEFAULT_MODEL", "fal-ai/veo3"),
        "timeout": float(os.getenv("FAL_VIDEO_PROXY_TIMEOUT", "600") or "600"),
        "dry_run": _env_bool("FAL_VIDEO_PROXY_DRY_RUN", True),
    }


def resolve_model(model: Optional[str], default_model: str, has_image: bool) -> str:
    """Return the fal model path for a request, or raise ValueError.

    Known short ids resolve to their t2v/i2v variant based on `has_image`.
    Full `fal-ai/...` paths (e.g. already ending in /text-to-video) pass through.
    """
    m = (model or default_model or "").strip()
    if m in MODEL_MAP:
        return MODEL_MAP[m]["i2v" if has_image else "t2v"]
    if m.startswith("fal-ai/"):
        return m  # pass-through for any other fal model/path
    raise ValueError(
        f"Unsupported model '{m}'. Supported: {sorted(MODEL_MAP)} "
        f"(or any 'fal-ai/...' id/path)."
    )


def to_fal_payload(req: VideoRequest, default_model: str) -> tuple[str, dict[str, Any]]:
    """Pure translation: OpenAI-style video request -> (fal_url, fal_payload).

    Does NOT touch the API key. Safe to unit-test directly. `n` is handled by the
    caller looping (most fal video models return ONE clip per call).
    """
    has_image = bool(req.image_url)
    fal_model = resolve_model(req.model, default_model, has_image)
    aspect = req.aspect_ratio or req.size or ""
    fal_aspect = ASPECT_MAP.get(aspect, DEFAULT_ASPECT)
    payload: dict[str, Any] = {"prompt": req.prompt, "aspect_ratio": fal_aspect}
    if has_image:
        payload["image_url"] = req.image_url
    if req.duration:
        payload["duration"] = str(req.duration)
    return f"{FAL_BASE}/{fal_model}", payload


def _openai_response(urls: list[str]) -> dict[str, Any]:
    return {"created": int(time.time()), "data": [{"url": u} for u in urls]}


def _extract_urls(body: dict[str, Any]) -> list[str]:
    """fal video responses use {'video': {'url'}} or {'videos': [{'url'}]}."""
    if isinstance(body.get("video"), dict) and body["video"].get("url"):
        return [body["video"]["url"]]
    out = []
    for v in (body.get("videos") or body.get("data") or []):
        if isinstance(v, dict) and v.get("url"):
            out.append(v["url"])
    return out


@app.get("/health")
def health() -> dict[str, str]:
    cfg = get_config()
    return {"status": "ok", "mode": "dry-run" if cfg["dry_run"] else "live"}


@app.post("/v1/videos/generations")
async def videos_generations(req: VideoRequest) -> JSONResponse:
    cfg = get_config()

    try:
        fal_url, fal_payload = to_fal_payload(req, cfg["default_model"])
    except ValueError as e:
        logger.warning("rejected request: %s", e)
        return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    n = max(1, int(req.n or 1))
    mode = "image-to-video" if req.image_url else "text-to-video"
    # Log translated payload (never the key).
    logger.info("translate model=%s mode=%s -> %s payload=%s n=%d dry_run=%s",
                req.model, mode, fal_url, fal_payload, n, cfg["dry_run"])

    if cfg["dry_run"]:
        urls = [f"https://dry-run.local/fal-video/{uuid.uuid4().hex}.mp4" for _ in range(n)]
        return JSONResponse(content=_openai_response(urls))

    if not cfg["fal_key"]:
        logger.error("live mode requested but FAL_KEY is not set")
        return JSONResponse(status_code=500,
                            content={"error": {"message": "FAL_KEY not set; cannot make live fal call."}})

    headers = {"Authorization": f"Key {cfg['fal_key']}", "Content-Type": "application/json"}
    urls: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            for _ in range(n):  # most fal video models return one clip per call
                r = await client.post(fal_url, json=fal_payload, headers=headers)
                if r.status_code != 200:
                    return JSONResponse(status_code=502,
                                        content={"error": {"message": f"fal error {r.status_code}: {r.text[:400]}"}})
                urls.extend(_extract_urls(r.json()))
    except Exception as e:  # noqa: BLE001 - clean error, never the key
        logger.error("fal call failed: %s", type(e).__name__)
        return JSONResponse(status_code=502, content={"error": {"message": f"fal request failed: {type(e).__name__}"}})

    if not urls:
        return JSONResponse(status_code=502, content={"error": {"message": "fal returned no video URLs"}})
    return JSONResponse(content=_openai_response(urls))

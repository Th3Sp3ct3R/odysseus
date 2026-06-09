"""
fal_image_proxy — OpenAI-Images-compatible adapter in front of fal.ai.

Odysseus generates images by POSTing the OpenAI shape to `<base>/v1/images/generations`.
fal.ai uses a different API, so this tiny FastAPI service translates between them:

    Odysseus ──OpenAI /v1/images/generations──▶ this proxy ──fal API──▶ fal.ai

Security & safety:
  • The fal API key is read ONLY from the FAL_KEY env var, sent only in the
    outbound Authorization header, and is NEVER logged.
  • DRY-RUN by default (FAL_PROXY_DRY_RUN=true): NO fal calls; returns a mock URL
    and logs the translated fal payload so you can verify the mapping offline.
  • If FAL_PROXY_DRY_RUN=false and FAL_KEY is missing, requests fail safely
    (HTTP 500, clear message) without any network call.

Run:  scripts/run-fal-image-proxy.sh   (defaults to 127.0.0.1:7010, dry-run)
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

logger = logging.getLogger("fal_image_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="fal image proxy", version="1.0.0")

# --------------------------------------------------------------------------- #
# Centralized model + size mapping (extend MODEL_MAP to add fal models).
# --------------------------------------------------------------------------- #

# Known fal image models. Any id starting with "fal-ai/" is also accepted
# (pass-through), so new fal models work without code changes.
MODEL_MAP: dict[str, str] = {
    "fal-ai/flux/dev": "fal-ai/flux/dev",
    "fal-ai/recraft/v3": "fal-ai/recraft/v3",
}

# OpenAI size string -> fal `image_size` enum (works for flux + recraft).
SIZE_MAP: dict[str, str] = {
    "1024x1024": "square_hd",
    "1024x1792": "portrait_16_9",
    "1792x1024": "landscape_16_9",
}
DEFAULT_FAL_SIZE = "square_hd"
FAL_BASE = "https://fal.run"


class ImageRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    n: int = 1
    size: str = "1024x1024"
    quality: Optional[str] = None  # accepted for OpenAI-compat; fal ignores it

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
        "default_model": os.getenv("FAL_DEFAULT_MODEL", "fal-ai/flux/dev"),
        "timeout": float(os.getenv("FAL_PROXY_TIMEOUT", "120") or "120"),
        "dry_run": _env_bool("FAL_PROXY_DRY_RUN", True),
    }


def resolve_model(model: Optional[str], default_model: str) -> str:
    """Return the fal model id for a requested model, or raise ValueError."""
    m = (model or default_model or "").strip()
    if m in MODEL_MAP:
        return MODEL_MAP[m]
    if m.startswith("fal-ai/"):
        return m  # pass-through for any other fal model
    raise ValueError(
        f"Unsupported model '{m}'. Supported: {sorted(MODEL_MAP)} "
        f"(or any 'fal-ai/...' id)."
    )


def to_fal_payload(req: ImageRequest, default_model: str) -> tuple[str, dict[str, Any]]:
    """Pure translation: OpenAI image request -> (fal_url, fal_payload).

    Does NOT touch the API key. Safe to unit-test directly.
    n>1 is handled by asking fal for `num_images=n` (flux + recraft support it);
    each returned image becomes one OpenAI `data[]` entry.
    """
    fal_model = resolve_model(req.model, default_model)
    n = max(1, int(req.n or 1))
    fal_size = SIZE_MAP.get(req.size, DEFAULT_FAL_SIZE)
    payload = {
        "prompt": req.prompt,
        "image_size": fal_size,
        "num_images": n,
    }
    return f"{FAL_BASE}/{fal_model}", payload


def _openai_response(urls: list[str]) -> dict[str, Any]:
    return {"created": int(time.time()), "data": [{"url": u} for u in urls]}


@app.get("/health")
def health() -> dict[str, str]:
    cfg = get_config()
    return {"status": "ok", "mode": "dry-run" if cfg["dry_run"] else "live"}


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest) -> JSONResponse:
    cfg = get_config()

    # Translate (no key involved).
    try:
        fal_url, fal_payload = to_fal_payload(req, cfg["default_model"])
    except ValueError as e:
        logger.warning("rejected request: %s", e)
        return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    # Log the translated payload for verification — NEVER the key.
    logger.info("translate model=%s -> %s payload=%s dry_run=%s",
                req.model, fal_url, fal_payload, cfg["dry_run"])

    if cfg["dry_run"]:
        urls = [f"https://dry-run.local/fal/{uuid.uuid4().hex}.png"
                for _ in range(fal_payload["num_images"])]
        return JSONResponse(content=_openai_response(urls))

    # Live mode requires a key — fail safely if absent (no network call).
    if not cfg["fal_key"]:
        logger.error("live mode requested but FAL_KEY is not set")
        return JSONResponse(status_code=500,
                            content={"error": {"message": "FAL_KEY not set; cannot make live fal call."}})

    headers = {"Authorization": f"Key {cfg['fal_key']}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            r = await client.post(fal_url, json=fal_payload, headers=headers)
        if r.status_code != 200:
            # r.text may include fal error detail; it never contains our key.
            return JSONResponse(status_code=502,
                                content={"error": {"message": f"fal error {r.status_code}: {r.text[:400]}"}})
        body = r.json()
    except Exception as e:  # noqa: BLE001 - surface a clean error, never the key
        logger.error("fal call failed: %s", type(e).__name__)
        return JSONResponse(status_code=502, content={"error": {"message": f"fal request failed: {type(e).__name__}"}})

    images = body.get("images") or body.get("data") or []
    urls = [img.get("url") for img in images if isinstance(img, dict) and img.get("url")]
    if not urls:
        return JSONResponse(status_code=502, content={"error": {"message": "fal returned no image URLs"}})
    return JSONResponse(content=_openai_response(urls))

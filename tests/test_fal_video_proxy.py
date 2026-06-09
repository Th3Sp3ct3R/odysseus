"""Mock/offline tests for the fal video proxy. No live fal calls are made."""

import logging

import pytest
from fastapi.testclient import TestClient

from services.fal_video_proxy.app import app, to_fal_payload, resolve_model, VideoRequest

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_text_to_video_translation():
    url, payload = to_fal_payload(
        VideoRequest(prompt="a soaring drone shot", model="fal-ai/veo3", size="1080x1920", duration=8),
        default_model="fal-ai/veo3",
    )
    assert url == "https://fal.run/fal-ai/veo3"        # t2v variant (no image)
    assert payload == {"prompt": "a soaring drone shot", "aspect_ratio": "9:16", "duration": "8"}


def test_image_to_video_uses_i2v_variant():
    url, payload = to_fal_payload(
        VideoRequest(prompt="animate this", model="fal-ai/veo3", image_url="https://x/y.png"),
        default_model="fal-ai/veo3",
    )
    assert url == "https://fal.run/fal-ai/veo3/image-to-video"
    assert payload["image_url"] == "https://x/y.png"
    assert payload["aspect_ratio"] == "16:9"  # default when no size/aspect


def test_dry_run_returns_openai_shape(monkeypatch):
    monkeypatch.setenv("FAL_VIDEO_PROXY_DRY_RUN", "true")
    monkeypatch.delenv("FAL_KEY", raising=False)
    r = client.post("/v1/videos/generations",
                    json={"model": "fal-ai/kling-video/v3/pro", "prompt": "temple at dawn", "n": 2, "aspect_ratio": "16:9"})
    assert r.status_code == 200
    body = r.json()
    assert "created" in body and isinstance(body["data"], list)
    assert len(body["data"]) == 2
    assert all(d["url"].startswith("https://dry-run.local/") and d["url"].endswith(".mp4") for d in body["data"])


def test_missing_key_fails_safely_when_live(monkeypatch):
    monkeypatch.setenv("FAL_VIDEO_PROXY_DRY_RUN", "false")
    monkeypatch.delenv("FAL_KEY", raising=False)
    r = client.post("/v1/videos/generations", json={"model": "fal-ai/veo3", "prompt": "x"})
    assert r.status_code == 500
    assert "FAL_KEY" in r.json()["error"]["message"]


def test_secret_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("FAL_VIDEO_PROXY_DRY_RUN", "true")
    monkeypatch.setenv("FAL_KEY", "sk-fal-VIDEOSECRET-DO-NOT-LOG")
    with caplog.at_level(logging.INFO):
        r = client.post("/v1/videos/generations", json={"model": "fal-ai/veo3", "prompt": "x"})
    assert r.status_code == 200
    assert "VIDEOSECRET" not in caplog.text


def test_unknown_model_clear_error():
    with pytest.raises(ValueError):
        resolve_model("sora-2", default_model="fal-ai/veo3", has_image=False)
    r = client.post("/v1/videos/generations", json={"model": "totally/unknown", "prompt": "x"})
    assert r.status_code == 400
    assert "Unsupported model" in r.json()["error"]["message"]


def test_full_fal_path_passthrough():
    url, _ = to_fal_payload(
        VideoRequest(prompt="x", model="fal-ai/kling-video/v3/pro/image-to-video", image_url="https://x/y.png"),
        default_model="fal-ai/veo3",
    )
    assert url == "https://fal.run/fal-ai/kling-video/v3/pro/image-to-video"

"""Mock/offline tests for the fal image proxy. No live fal calls are made."""

import logging

import pytest
from fastapi.testclient import TestClient

from services.fal_image_proxy.app import app, to_fal_payload, resolve_model, ImageRequest

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_openai_to_fal_translation():
    # size mapping, n -> num_images, model resolution, fal url
    url, payload = to_fal_payload(
        ImageRequest(prompt="a temple", model="fal-ai/flux/dev", n=2, size="1792x1024"),
        default_model="fal-ai/flux/dev",
    )
    assert url == "https://fal.run/fal-ai/flux/dev"
    assert payload == {"prompt": "a temple", "image_size": "landscape_16_9", "num_images": 2}

    # unknown size falls back to square_hd; default model used when omitted
    url2, payload2 = to_fal_payload(
        ImageRequest(prompt="x", size="999x999"), default_model="fal-ai/recraft/v3"
    )
    assert url2 == "https://fal.run/fal-ai/recraft/v3"
    assert payload2["image_size"] == "square_hd"
    assert payload2["num_images"] == 1


def test_dry_run_returns_openai_shape(monkeypatch):
    monkeypatch.setenv("FAL_PROXY_DRY_RUN", "true")
    monkeypatch.delenv("FAL_KEY", raising=False)
    r = client.post("/v1/images/generations",
                    json={"model": "fal-ai/flux/dev", "prompt": "gold scribe", "n": 2, "size": "1024x1024"})
    assert r.status_code == 200
    body = r.json()
    assert "created" in body and isinstance(body["data"], list)
    assert len(body["data"]) == 2
    assert all("url" in d and d["url"].startswith("https://dry-run.local/") for d in body["data"])


def test_missing_key_fails_safely_when_live(monkeypatch):
    monkeypatch.setenv("FAL_PROXY_DRY_RUN", "false")
    monkeypatch.delenv("FAL_KEY", raising=False)
    r = client.post("/v1/images/generations", json={"model": "fal-ai/flux/dev", "prompt": "x"})
    assert r.status_code == 500
    assert "FAL_KEY" in r.json()["error"]["message"]


def test_secret_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("FAL_PROXY_DRY_RUN", "true")
    monkeypatch.setenv("FAL_KEY", "sk-fal-SUPERSECRET-DO-NOT-LOG")
    with caplog.at_level(logging.INFO):
        r = client.post("/v1/images/generations", json={"model": "fal-ai/flux/dev", "prompt": "x"})
    assert r.status_code == 200
    assert "SUPERSECRET" not in caplog.text
    assert "sk-fal-SUPERSECRET-DO-NOT-LOG" not in caplog.text


def test_unknown_model_clear_error():
    # pure resolver
    with pytest.raises(ValueError):
        resolve_model("dall-e-3", default_model="fal-ai/flux/dev")
    # via the endpoint
    r = client.post("/v1/images/generations", json={"model": "totally/unknown", "prompt": "x"})
    assert r.status_code == 400
    assert "Unsupported model" in r.json()["error"]["message"]


def test_fal_passthrough_model():
    # any fal-ai/* id is accepted (extensible without code change)
    url, _ = to_fal_payload(ImageRequest(prompt="x", model="fal-ai/flux-2/dev"),
                            default_model="fal-ai/flux/dev")
    assert url == "https://fal.run/fal-ai/flux-2/dev"

"""Unit tests for the read-only GeeLark client (services/posting/geelark.py).

No network: a fake httpx.AsyncClient captures the request and returns canned
envelopes. Verifies auth header construction (both methods), endpoint whitelist,
response unwrapping, and error classification.
"""

import hashlib

import pytest

from services.posting.geelark import (
    GeeLarkClient,
    GeeLarkConfig,
    GeeLarkError,
    GeeLarkAuthError,
    GeeLarkRateLimitError,
)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Captures the last POST and returns a queued envelope."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[dict] = []

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(self._payload)

    async def aclose(self):  # pragma: no cover - not used (client injected)
        pass


def _ok(data: dict) -> dict:
    return {"traceId": "t-1", "code": 0, "msg": "success", "data": data}


def _err(code: int, msg: str = "boom") -> dict:
    return {"traceId": "t-1", "code": code, "msg": msg, "data": None}


# ---- config / auth selection -------------------------------------------------

def test_auth_method_selection():
    assert GeeLarkConfig(token="abc").auth_method == "token"
    assert GeeLarkConfig(app_id="A", api_key="K").auth_method == "sign"
    assert GeeLarkConfig().auth_method == "none"


def test_validate_requires_credentials():
    with pytest.raises(GeeLarkAuthError):
        GeeLarkClient(GeeLarkConfig())  # validate() runs in __init__


def test_from_env_prefers_token(monkeypatch):
    monkeypatch.setenv("GEELARK_TOKEN", "tok")
    monkeypatch.setenv("GEELARK_APP_ID", "app")
    monkeypatch.setenv("GEELARK_API_KEY", "key")
    cfg = GeeLarkConfig.from_env(allow_skill_fallback=False)
    assert cfg.auth_method == "token"
    assert cfg.token == "tok"


# ---- header construction -----------------------------------------------------

async def test_token_auth_headers():
    fake = _FakeAsyncClient(_ok({"balance": 5}))
    async with GeeLarkClient(GeeLarkConfig(token="tok"), client=fake) as gl:
        await gl.wallet()
    headers = fake.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert "traceId" in headers
    assert "sign" not in headers


async def test_sign_auth_headers_are_correct():
    fake = _FakeAsyncClient(_ok({"balance": 5}))
    async with GeeLarkClient(GeeLarkConfig(app_id="APP", api_key="KEY"), client=fake) as gl:
        await gl.wallet()
    h = fake.calls[0]["headers"]
    assert h["appId"] == "APP"
    assert h["nonce"] == h["traceId"][:6]
    expected = hashlib.sha256(
        f"APP{h['traceId']}{h['ts']}{h['nonce']}KEY".encode()
    ).hexdigest().upper()
    assert h["sign"] == expected
    assert "Authorization" not in h


# ---- call behavior -----------------------------------------------------------

async def test_call_unwraps_data():
    fake = _FakeAsyncClient(_ok({"total": 23, "items": []}))
    async with GeeLarkClient(GeeLarkConfig(token="t"), client=fake) as gl:
        data = await gl.phone_list()
    assert data["total"] == 23
    assert fake.calls[0]["json"] == {"page": 1, "pageSize": 100}


async def test_non_whitelisted_endpoint_blocked():
    fake = _FakeAsyncClient(_ok({}))
    async with GeeLarkClient(GeeLarkConfig(token="t"), client=fake) as gl:
        with pytest.raises(GeeLarkError, match="whitelist"):
            await gl.call("/open/v1/phone/start", {"ids": ["x"]})
    assert fake.calls == []  # never hit the wire


async def test_rate_limit_code_raises_specific_error():
    fake = _FakeAsyncClient(_err(40014, "locked"))
    async with GeeLarkClient(GeeLarkConfig(token="t"), client=fake) as gl:
        with pytest.raises(GeeLarkRateLimitError) as ei:
            await gl.wallet()
    assert ei.value.code == 40014


async def test_auth_code_raises_auth_error():
    fake = _FakeAsyncClient(_err(40003, "bad sign"))
    async with GeeLarkClient(GeeLarkConfig(token="t"), client=fake) as gl:
        with pytest.raises(GeeLarkAuthError):
            await gl.wallet()


async def test_generic_error_code():
    fake = _FakeAsyncClient(_err(42001, "phone gone"))
    async with GeeLarkClient(GeeLarkConfig(token="t"), client=fake) as gl:
        with pytest.raises(GeeLarkError) as ei:
            await gl.wallet()
    assert ei.value.code == 42001

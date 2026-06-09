"""Async GeeLark Open API client.

Talks directly to the GeeLark REST API (https://openapi.geelark.com) — the cloud
Android phone platform we use to post reels to TikTok / Instagram. Patterns
(endpoint whitelist, dual auth, response envelope) are ported from the proven
~/.hermes/skills/devops/awesome-geelark-skill client so the app has no runtime
dependency on that skill.

Phase 0 scope: READ-ONLY. Only balance + phone-listing endpoints are exposed.
Phone start/stop, app install, and RPA publish tasks are added in Phase 1 — the
whitelist below is intentionally small so we cannot accidentally mutate the fleet
or spend credits while validating credentials.

Auth (two supported methods, auto-selected):
  - Bearer token   : Authorization: Bearer <token>            (if GEELARK_TOKEN set)
  - Key + sign     : appId/ts/nonce/sign = SHA256(appId+traceId+ts+nonce+apiKey)
                     (if GEELARK_APP_ID + GEELARK_API_KEY set)

Config resolution order (per field): explicit kwarg -> environment variable ->
optional fallback to the GeeLark skill's config.json (so existing credentials
work out of the box). Nothing is hardcoded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openapi.geelark.com"

# Optional convenience fallback: the GeeLark skill stores appId/apiKey here.
_SKILL_CONFIG_PATH = (
    Path.home()
    / ".hermes/skills/devops/awesome-geelark-skill/assets/config.json"
)

# Read-only endpoint whitelist for Phase 0. Calling anything else raises —
# guessing endpoints is how you get rate-limit locks and accidental spend.
READONLY_ENDPOINTS: frozenset[str] = frozenset(
    {
        "/open/v1/pay/wallet",
        "/open/v1/phone/list",
        "/open/v1/phone/status",
    }
)

# GeeLark error codes that mean "rate limited" (see references/error_codes.md).
# 40014 locks the endpoint for 2 hours — treat all of these as hard backoff.
_RATE_LIMIT_CODES = {40007, 40014, 40017}


class GeeLarkError(Exception):
    """Base error for any non-success GeeLark API response or transport failure."""

    def __init__(self, message: str, *, code: int | None = None, trace_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.trace_id = trace_id


class GeeLarkAuthError(GeeLarkError):
    """Missing/invalid credentials, or signature/permission rejection by the API."""


class GeeLarkRateLimitError(GeeLarkError):
    """Rate limited (codes 40007/40014/40017). 40014 = 2-hour endpoint lock."""


@dataclass
class GeeLarkConfig:
    """GeeLark connection + auth config.

    Use :meth:`from_env` to build one from environment / skill-config fallback.
    """

    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
    app_id: str | None = None
    api_key: str | None = None

    @classmethod
    def from_env(cls, *, allow_skill_fallback: bool = True) -> "GeeLarkConfig":
        base_url = os.environ.get("GEELARK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        token = os.environ.get("GEELARK_TOKEN") or None
        app_id = os.environ.get("GEELARK_APP_ID") or None
        api_key = os.environ.get("GEELARK_API_KEY") or None

        if allow_skill_fallback and not token and not (app_id and api_key):
            skill = _load_skill_config()
            token = token or skill.get("token")
            app_id = app_id or skill.get("appId")
            api_key = api_key or skill.get("apiKey")

        return cls(base_url=base_url, token=token, app_id=app_id, api_key=api_key)

    @property
    def auth_method(self) -> str:
        if self.token:
            return "token"
        if self.app_id and self.api_key:
            return "sign"
        return "none"

    def validate(self) -> None:
        if self.auth_method == "none":
            raise GeeLarkAuthError(
                "No GeeLark credentials configured. Set GEELARK_TOKEN, or "
                "GEELARK_APP_ID + GEELARK_API_KEY (in .env or environment)."
            )


def _load_skill_config() -> dict[str, str]:
    """Best-effort read of the GeeLark skill's config.json. Never raises."""
    try:
        raw = json.loads(_SKILL_CONFIG_PATH.read_text())
        auth = raw.get("auth", {}) if isinstance(raw, dict) else {}
        return {
            "token": (auth.get("token") or "").strip() or None,
            "appId": (auth.get("appId") or "").strip() or None,
            "apiKey": (auth.get("apiKey") or "").strip() or None,
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


class GeeLarkClient:
    """Minimal async GeeLark client (read-only in Phase 0).

    Example:
        async with GeeLarkClient() as gl:
            wallet = await gl.wallet()
            phones = await gl.phone_list()
    """

    def __init__(
        self,
        config: GeeLarkConfig | None = None,
        *,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.config = config or GeeLarkConfig.from_env()
        self.config.validate()
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "GeeLarkClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- header construction -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        trace_id = str(uuid.uuid4())
        headers = {"Content-Type": "application/json", "traceId": trace_id}

        if self.config.auth_method == "token":
            headers["Authorization"] = f"Bearer {self.config.token}"
        else:  # sign
            ts = str(int(time.time() * 1000))
            nonce = trace_id[:6]
            raw = f"{self.config.app_id}{trace_id}{ts}{nonce}{self.config.api_key}"
            sign = hashlib.sha256(raw.encode()).hexdigest().upper()
            headers.update(
                {
                    "appId": self.config.app_id or "",
                    "ts": ts,
                    "nonce": nonce,
                    "sign": sign,
                }
            )
        return headers

    # ---- core call -----------------------------------------------------------

    async def call(self, endpoint: str, data: dict | None = None) -> dict:
        """POST to a whitelisted GeeLark endpoint and return the `data` payload.

        Raises GeeLarkAuthError / GeeLarkRateLimitError / GeeLarkError on failure.
        """
        if endpoint not in READONLY_ENDPOINTS:
            raise GeeLarkError(
                f"Endpoint {endpoint!r} is not in the Phase 0 read-only whitelist "
                f"({sorted(READONLY_ENDPOINTS)}). Publishing endpoints land in Phase 1."
            )
        if self._client is None:
            raise GeeLarkError("Client not started. Use `async with GeeLarkClient() as gl:`.")

        url = f"{self.config.base_url}{endpoint}"
        try:
            resp = await self._client.post(url, headers=self._headers(), json=data or {})
        except httpx.HTTPError as exc:
            raise GeeLarkError(f"GeeLark transport error calling {endpoint}: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise GeeLarkError(
                f"GeeLark returned non-JSON ({resp.status_code}) for {endpoint}: {resp.text[:200]!r}"
            ) from exc

        code = body.get("code")
        trace_id = body.get("traceId")
        if code == 0:
            return body.get("data") or {}

        msg = body.get("msg", "unknown error")
        if code in _RATE_LIMIT_CODES:
            raise GeeLarkRateLimitError(
                f"GeeLark rate limited on {endpoint} (code {code}): {msg}",
                code=code,
                trace_id=trace_id,
            )
        if code in (40003, 40013, 40015, 40016):  # sign/user/permission/IP-whitelist
            raise GeeLarkAuthError(
                f"GeeLark auth/permission error on {endpoint} (code {code}): {msg}",
                code=code,
                trace_id=trace_id,
            )
        raise GeeLarkError(
            f"GeeLark error on {endpoint} (code {code}): {msg}",
            code=code,
            trace_id=trace_id,
        )

    # ---- read-only convenience methods --------------------------------------

    async def wallet(self) -> dict:
        """Account balance / billing snapshot."""
        return await self.call("/open/v1/pay/wallet", {})

    async def phone_list(self, page: int = 1, page_size: int = 100) -> dict:
        """List cloud phones in the account (paginated)."""
        return await self.call("/open/v1/phone/list", {"page": page, "pageSize": page_size})

    async def phone_status(self, phone_ids: list[str]) -> dict:
        """Power/running status for the given phone IDs."""
        return await self.call("/open/v1/phone/status", {"ids": phone_ids})

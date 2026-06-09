#!/usr/bin/env python3
"""Read-only GeeLark connectivity check (Phase 0 de-risk).

Verifies credentials work and surveys the cloud-phone fleet. Makes ONLY
read-only calls (wallet, phone list) — never starts/stops phones or spends
credits.

Usage:
    # credentials from env (preferred)
    GEELARK_TOKEN=... python scripts/check_geelark.py
    # or key auth
    GEELARK_APP_ID=... GEELARK_API_KEY=... python scripts/check_geelark.py
    # or rely on the GeeLark skill's config.json fallback (appId+apiKey)
    python scripts/check_geelark.py
"""

import asyncio
import sys

from services.posting.geelark import (
    GeeLarkClient,
    GeeLarkConfig,
    GeeLarkError,
)


async def main() -> int:
    cfg = GeeLarkConfig.from_env()
    print(f"Base URL    : {cfg.base_url}")
    print(f"Auth method : {cfg.auth_method}")
    if cfg.auth_method == "none":
        print(
            "\n✗ No credentials found. Set GEELARK_TOKEN, or "
            "GEELARK_APP_ID + GEELARK_API_KEY."
        )
        return 2

    try:
        async with GeeLarkClient(cfg) as gl:
            wallet = await gl.wallet()
            print("\n✓ Auth OK — wallet reachable")
            print(f"  wallet: {wallet}")

            phones = await gl.phone_list()
            total = phones.get("total", phones.get("totalCount", "?"))
            items = phones.get("items") or phones.get("list") or phones.get("data") or []
            print(f"\n✓ Fleet: {total} phone(s) reported; {len(items)} in first page")
            for p in items[:10]:
                pid = p.get("id") or p.get("serialName") or "?"
                name = p.get("serialName") or p.get("remark") or p.get("name") or ""
                print(f"    - {pid}  {name}")
    except GeeLarkError as exc:
        print(f"\n✗ GeeLark error (code={getattr(exc, 'code', None)}): {exc}")
        return 1

    print("\nDone. (read-only — no phones started, no credits spent)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

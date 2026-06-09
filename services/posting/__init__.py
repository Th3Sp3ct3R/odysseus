"""Posting subsystem — publish finished reels to social platforms via GeeLark cloud phones.

Phase 0 (current): read-only GeeLark client (`wallet`, `phone_list`, `phone_status`)
to verify credentials and survey the device fleet. Publishing (instagramPubReels,
TikTok upload) is added in Phase 1.

See docs/design/reel-pipeline-and-geelark-posting.md.
"""

from services.posting.geelark import (
    GeeLarkClient,
    GeeLarkConfig,
    GeeLarkError,
    GeeLarkAuthError,
    GeeLarkRateLimitError,
)

__all__ = [
    "GeeLarkClient",
    "GeeLarkConfig",
    "GeeLarkError",
    "GeeLarkAuthError",
    "GeeLarkRateLimitError",
]

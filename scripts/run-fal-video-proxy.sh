#!/usr/bin/env bash
# Run the fal VIDEO proxy locally. DRY-RUN by default (no fal calls, no key needed).
#
#   scripts/run-fal-video-proxy.sh
#
# Loads services/fal_video_proxy/.env if present (you create it from .env.example
# and put your FAL_KEY there). The key is never printed by this script.
set -euo pipefail

PROXY_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/fal_video_proxy"
HOST="${FAL_VIDEO_PROXY_HOST:-127.0.0.1}"
PORT="${FAL_VIDEO_PROXY_PORT:-7011}"

if [ -f "$PROXY_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROXY_DIR/.env"
  set +a
  echo "loaded $PROXY_DIR/.env"
else
  echo "no .env found — running with current environment (dry-run default)"
fi

echo "fal video proxy → http://${HOST}:${PORT}  (dry_run=${FAL_VIDEO_PROXY_DRY_RUN:-true})"
cd "$PROXY_DIR"
exec uvicorn app:app --host "$HOST" --port "$PORT"

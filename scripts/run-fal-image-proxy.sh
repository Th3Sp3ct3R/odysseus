#!/usr/bin/env bash
# Run the fal image proxy locally. DRY-RUN by default (no fal calls, no key needed).
#
#   scripts/run-fal-image-proxy.sh
#
# It loads services/fal_image_proxy/.env if present (you create that file from
# .env.example and put your FAL_KEY there). The key is never printed by this script.
set -euo pipefail

PROXY_DIR="$(cd "$(dirname "$0")/.." && pwd)/services/fal_image_proxy"
HOST="${FAL_PROXY_HOST:-127.0.0.1}"
PORT="${FAL_PROXY_PORT:-7010}"

# Load .env (if you created one) without echoing its contents.
if [ -f "$PROXY_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROXY_DIR/.env"
  set +a
  echo "loaded $PROXY_DIR/.env"
else
  echo "no .env found — running with current environment (dry-run default)"
fi

echo "fal image proxy → http://${HOST}:${PORT}  (dry_run=${FAL_PROXY_DRY_RUN:-true})"
cd "$PROXY_DIR"
exec uvicorn app:app --host "$HOST" --port "$PORT"

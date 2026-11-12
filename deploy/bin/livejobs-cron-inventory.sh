#!/usr/bin/env bash
# Monthly Common Crawl inventory refresh, for cron. Scheduled on days 5-12; it is idempotent, so only the first
# run after a new crawl appears does real work. Skips quietly if yesterday's run is still going.
set -uo pipefail
ENV_FILE="${LIVEJOBS_ENV_FILE:-/etc/livejobs/livejobs.env}"
APP_DIR="${LIVEJOBS_APP_DIR:-/opt/livejobs}"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE" || { echo "cannot read $ENV_FILE" >&2; exit 2; }
set +a

echo "[$(date -u +%FT%TZ)] inventory refresh starting"
flock -n -E 99 "${LIVEJOBS_INVENTORY_LOCK_FILE:-/tmp/livejobs-inventory.lock}" "$APP_DIR/.venv/bin/livejobs" inventory --refresh
rc=$?
case "$rc" in
  0)  echo "[$(date -u +%FT%TZ)] inventory refresh finished (or already up to date)" ;;
  75) echo "[$(date -u +%FT%TZ)] inventory refresh incomplete (index server errors); the next scheduled run will resume it" ; exit 0 ;;
  99) echo "[$(date -u +%FT%TZ)] previous inventory run is still going; skipping" ; exit 0 ;;
  *)  echo "[$(date -u +%FT%TZ)] inventory refresh FAILED (exit $rc)" >&2 ; exit "$rc" ;;
esac

#!/usr/bin/env bash
# Hourly job fetch, for cron. Loads the service's environment (cron doesn't read systemd EnvironmentFile)
# and skips quietly if the previous run is still going.
set -uo pipefail
ENV_FILE="${LIVEJOBS_ENV_FILE:-/etc/livejobs/livejobs.env}"
APP_DIR="${LIVEJOBS_APP_DIR:-/opt/livejobs}"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE" || { echo "cannot read $ENV_FILE" >&2; exit 2; }
set +a

echo "[$(date -u +%FT%TZ)] sync starting"
# -n: don't wait for the lock. -E 99: a distinct exit code for "lock is held", so it isn't confused with a failed sync.
flock -n -E 99 "${LIVEJOBS_LOCK_FILE:-/tmp/livejobs-sync.lock}" "$APP_DIR/.venv/bin/livejobs" sync-once --slack 10
rc=$?
case "$rc" in
  0)  echo "[$(date -u +%FT%TZ)] sync finished" ;;
  99) echo "[$(date -u +%FT%TZ)] previous sync is still running; skipping this hour" ; exit 0 ;;
  *)  echo "[$(date -u +%FT%TZ)] sync FAILED (exit $rc)" >&2 ; exit "$rc" ;;
esac

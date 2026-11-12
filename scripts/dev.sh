#!/usr/bin/env bash
# Run jobmcp locally with one command: virtualenv, dependencies, MySQL, sample users, sync worker, site.
#
#   scripts/dev.sh                  # http://localhost:8000
#   scripts/dev.sh --port 9000      # another port
#   scripts/dev.sh --reset          # drop and recreate all tables first (dev databases only: name must end in _dev)
#   scripts/dev.sh --no-seed        # don't add/sync the sample boards (no internet needed)
#
# MySQL: if JOBMCP_DATABASE_URL is not set, the script uses the one from docker-compose.yml
# (mysql+pymysql://jobmcp:jobmcp@127.0.0.1:3306/jobmcp_dev) and starts it with Docker if it isn't running.
# To use your own MySQL 8 instead:
#   JOBMCP_DATABASE_URL='mysql+pymysql://user:pw@127.0.0.1:3306/jobmcp_dev?charset=utf8mb4' scripts/dev.sh
#
# Verification and password-reset emails are printed in this terminal instead of being sent.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/_db.sh
. scripts/_db.sh

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "jobmcp needs Python 3.10 or newer (found: $("$PYTHON" --version 2>&1)). Set PYTHON=/path/to/python3." >&2
  exit 1
fi

[ -d .venv ] || "$PYTHON" -m venv .venv
# Reinstall only when dependencies may have changed.
STAMP=.venv/.jobmcp-installed
if [ ! -f "$STAMP" ] || [ pyproject.toml -nt "$STAMP" ]; then
  echo "Installing dependencies..."
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e '.[dev]'
  touch "$STAMP"
fi

DEFAULT_URL='mysql+pymysql://jobmcp:jobmcp@127.0.0.1:3306/jobmcp_dev?charset=utf8mb4'
if [ -z "${JOBMCP_DATABASE_URL:-}" ]; then
  export JOBMCP_DATABASE_URL="$DEFAULT_URL"; IS_DEFAULT=true
else
  IS_DEFAULT=false
fi
ensure_mysql "$JOBMCP_DATABASE_URL" "$IS_DEFAULT" || exit 1

exec .venv/bin/jobmcp dev "$@"

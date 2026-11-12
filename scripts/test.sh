#!/usr/bin/env bash
# Run the test suite against MySQL.
#
#   scripts/test.sh                  # all tests
#   scripts/test.sh -k metrics -x    # arguments go to pytest
#
# Uses LIVEJOBS_TEST_DATABASE_URL, or the livejobs_test database from docker-compose.yml (started with Docker if
# needed). The tests DROP AND RECREATE EVERY TABLE in that database, so its name must end in _test.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/_db.sh
. scripts/_db.sh

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet -e '.[dev]'

DEFAULT_URL='mysql+pymysql://livejobs:livejobs@127.0.0.1:3306/livejobs_test?charset=utf8mb4'
if [ -z "${LIVEJOBS_TEST_DATABASE_URL:-}" ]; then
  export LIVEJOBS_TEST_DATABASE_URL="$DEFAULT_URL"; IS_DEFAULT=true
else
  IS_DEFAULT=false
fi
ensure_mysql "$LIVEJOBS_TEST_DATABASE_URL" "$IS_DEFAULT" || exit 1
exec .venv/bin/pytest "$@"

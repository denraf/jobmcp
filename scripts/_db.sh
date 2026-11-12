# Shared by dev.sh and test.sh (sourced, not run). Makes sure a MySQL is reachable at $1, starting the one from
# docker-compose.yml if the URL is the default and nothing is listening yet.

db_up() {  # is MySQL reachable at this URL?
  .venv/bin/python - "$1" <<'PY'
import sys
from sqlalchemy import create_engine, text
from jobmcp import db
try:
    with create_engine(db.mysql_url(sys.argv[1]), connect_args={"connect_timeout": 3}).connect() as c:
        c.execute(text("SELECT 1"))
except Exception:
    sys.exit(1)
PY
}

compose_cmd() {  # prints a compose command whose container runtime is actually running here, or nothing
  if docker info >/dev/null 2>&1; then  # Docker (Desktop, Colima, OrbStack, Linux engine)
    if docker compose version >/dev/null 2>&1; then echo "docker compose"; return; fi
    if command -v docker-compose >/dev/null 2>&1; then echo "docker-compose"; return; fi
  fi
  # Apple's `container` (macOS 26): container-compose is only a front end, `container system` is the service.
  if command -v container-compose >/dev/null 2>&1 && container system status >/dev/null 2>&1; then
    echo "container-compose"
  fi
}

compose_up() {  # start the db service from docker-compose.yml with the given compose command
  case "$1" in
    container-compose) container-compose up -d db ;;  # has no --wait; we poll for readiness below
    *) $1 up -d db ;;
  esac
}

ensure_mysql() {  # $1 = URL, $2 = true if it is the default (so we may start Docker's MySQL for it)
  local url="$1" is_default="$2"
  db_up "$url" && return 0
  if [ "$is_default" = true ]; then
    local compose; compose="$(compose_cmd)"
    if [ -n "$compose" ]; then  # compose_cmd only returns a command whose runtime is running
      echo "Starting MySQL with $compose..."
      compose_up "$compose"
      for _ in $(seq 1 60); do db_up "$url" && return 0; sleep 2; done
      echo "MySQL was started but did not accept connections after 2 minutes. Look at its logs ($compose; or 'container logs jobmcp-db')." >&2
      return 1
    fi
    # Say precisely what is missing: a tool that is installed but not running is the usual cause.
    if command -v container-compose >/dev/null 2>&1 && ! container system status >/dev/null 2>&1; then
      echo "Apple's container tooling is installed but its service is not running. Start it once with:" >&2
      echo "    brew install container   # if 'container' is missing" >&2
      echo "    container system start --enable-kernel-install" >&2
    elif command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
      echo "Docker is installed but its daemon is not running. Start Docker Desktop (or Colima/OrbStack) and re-run." >&2
    fi
  fi
  cat >&2 <<MSG

jobmcp runs on MySQL 8, and none is reachable at:
  $url

Pick one:
  1. A container runtime: Docker with the Compose plugin, or Apple's container + container-compose
     (macOS 26: brew install container container-compose && container system start --enable-kernel-install).
     Start it and re-run this script; it starts MySQL for you.
  2. Your own MySQL 8: create a database and user, then run with
       JOBMCP_DATABASE_URL='mysql+pymysql://USER:PASSWORD@127.0.0.1:3306/NAME?charset=utf8mb4' $0
     (for dev the database name must end in _dev; for tests, _test: those scripts drop and recreate tables).
MSG
  return 1
}

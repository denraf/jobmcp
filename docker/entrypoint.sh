#!/bin/sh
# Container entrypoint. Containers start in any order, so every service first waits for MySQL.
#
#   LIVEJOBS_RUN_MIGRATIONS=1  (the web service)  create/upgrade the tables, then create the first admin if needed
#   otherwise                  (the scheduler)    wait until the web service has finished doing that
#
# Privileges: the container starts as root only so the container runtime can finish setting it up (Apple's
# container-compose, for one, edits /etc/hosts inside each container as the container's default user, and cannot do
# that for a non-root image). Everything below, including the application itself, runs as the unprivileged
# `livejobs` user and cannot gain privileges back.
set -eu

DROP="setpriv --reuid=livejobs --regid=livejobs --init-groups --no-new-privs --"

# Fail closed: if the privilege drop doesn't work, the container stops. It never quietly carries on as root.
if [ "$(id -u)" = "0" ]; then
  run() { $DROP "$@"; }
else
  run() { "$@"; }  # already unprivileged (e.g. started with --user): nothing to drop
fi

run livejobs wait-db --timeout "${LIVEJOBS_DB_WAIT_SECONDS:-180}"

if [ "${LIVEJOBS_RUN_MIGRATIONS:-0}" = "1" ]; then
  run livejobs init-db
  run livejobs ensure-admin
else
  run livejobs wait-db --schema --timeout "${LIVEJOBS_DB_WAIT_SECONDS:-180}"
fi

if [ "$(id -u)" = "0" ]; then
  exec $DROP "$@"
fi
exec "$@"

#!/usr/bin/env bash
# Nightly MySQL dump, compressed, keeping the last 14. Credentials come from a MySQL option file
# (see docs/INSTALL.md, "Backups"), so no password appears on the command line or in `ps`.
set -euo pipefail
DEST="${LIVEJOBS_BACKUP_DIR:-/var/backups/livejobs}"
DB="${LIVEJOBS_DB_NAME:-livejobs}"
KEEP="${LIVEJOBS_BACKUP_KEEP:-14}"
CNF="${LIVEJOBS_MYSQL_DEFAULTS:-/etc/livejobs/backup.my.cnf}"

mkdir -p "$DEST"
OUT="$DEST/$DB-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
# --single-transaction: a consistent snapshot of InnoDB tables without locking the site.
# --no-tablespaces: the app user has no PROCESS privilege, and without this flag mysqldump 8 refuses to run.
mysqldump --defaults-extra-file="$CNF" --single-transaction --quick --no-tablespaces "$DB" | gzip -9 > "$OUT.partial"
mv "$OUT.partial" "$OUT"
echo "[$(date -u +%FT%TZ)] wrote $OUT ($(du -h "$OUT" | cut -f1))"

# prune
ls -1t "$DEST"/"$DB"-*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --

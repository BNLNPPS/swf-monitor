#!/usr/bin/env bash
# Nightly swfdb backup with built-in integrity verification.
#
# Dumps the system database (compressed pg_dump custom format) to the
# durable shared area on the large /data volume, verifies the fresh dump
# by reading its table of contents, and ages out old dumps (nightlies
# after KEEP_DAILY days; first-of-month dumps after KEEP_MONTHLY days).
# Runs standalone, from cron, or by hand; every run appends one line to
# backup.log, ERROR-prefixed on failure. Restore with:
#   pg_restore -h <host> -U <user> -d <db> --clean --if-exists <dump>
#
# On-volume dumps protect against logical loss (bad migration, table
# deletion, corruption), not against loss of /data itself — PGDATA
# shares the volume. Off-host replication is a separate step.
set -uo pipefail

ENV_FILE=${SWF_ENV_FILE:-/opt/swf-monitor/config/env/production.env}
BACKUP_DIR=${SWF_DB_BACKUP_DIR:-/data/swf-shared/db-backups}
LOG_FILE="$BACKUP_DIR/backup.log"
KEEP_DAILY=14     # days a nightly dump is kept
KEEP_MONTHLY=400  # days a first-of-month dump is kept
# The 1.1 GB database compresses to ~48 MB (2026-07); alert if a dump
# arrives at less than half that.
MIN_BYTES=20000000
MIN_TABLES=20

mkdir -p "$BACKUP_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
fail() { log "ERROR: $*"; exit 1; }

# Literal KEY=VALUE parse — the env file carries unquoted shell
# metacharacters in secret values, so it is never bash-sourced.
env_get() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
[ -r "$ENV_FILE" ] || fail "cannot read $ENV_FILE"
DB_NAME=$(env_get DB_NAME)
DB_USER=$(env_get DB_USER)
DB_HOST=$(env_get DB_HOST)
PGPASSWORD=$(env_get DB_PASSWORD)
export PGPASSWORD
[ -n "$DB_NAME" ] && [ -n "$DB_USER" ] || fail "DB_NAME/DB_USER missing from $ENV_FILE"
DB_HOST=${DB_HOST:-localhost}

STAMP=$(date +%Y%m%d)
DUMP="$BACKUP_DIR/swfdb-$STAMP.dump"
TMP="$DUMP.inprogress"

pg_dump -Fc -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -f "$TMP" \
    || fail "pg_dump failed for $DB_NAME@$DB_HOST"

# Integrity: the dump must carry a plausible table of contents and bulk.
ITEMS=$(pg_restore --list "$TMP" 2>/dev/null | grep -c "TABLE DATA")
[ "${ITEMS:-0}" -ge "$MIN_TABLES" ] \
    || fail "verification failed: only ${ITEMS:-0} TABLE DATA entries in $TMP"
SIZE=$(stat -c%s "$TMP")
[ "$SIZE" -ge "$MIN_BYTES" ] \
    || fail "dump suspiciously small: $SIZE bytes in $TMP"

mv "$TMP" "$DUMP"

# Retention: nightlies age out; first-of-month dumps are kept longer.
find "$BACKUP_DIR" -name 'swfdb-*.dump' ! -name 'swfdb-????01.dump' -mtime +"$KEEP_DAILY" -delete
find "$BACKUP_DIR" -name 'swfdb-????01.dump' -mtime +"$KEEP_MONTHLY" -delete
find "$BACKUP_DIR" -name '*.inprogress' -mtime +1 -delete

log "OK swfdb-$STAMP.dump: $SIZE bytes, $ITEMS tables verified"

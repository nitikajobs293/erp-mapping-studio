#!/usr/bin/env bash
# PostgreSQL backup with retention. Usage: DATABASE_URL=postgresql://user@host/db ./scripts/backup.sh
# Schedule daily (cron / Kubernetes CronJob / `docker compose --profile backup run backup`).
set -euo pipefail
: "${DATABASE_URL:?DATABASE_URL is required (postgresql://user@host:port/db)}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
URL="${DATABASE_URL/postgresql+psycopg2:/postgresql:}"
mkdir -p "$BACKUP_DIR"
FILE="$BACKUP_DIR/erpdm_$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --format=custom --no-owner --file "$FILE" "$URL"
pg_restore --list "$FILE" > /dev/null   # verify archive is readable
find "$BACKUP_DIR" -name 'erpdm_*.dump' -mtime +"$RETENTION_DAYS" -delete
echo "Backup written: $FILE"

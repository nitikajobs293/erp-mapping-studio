#!/usr/bin/env bash
# Restore a backup created by backup.sh. Usage: DATABASE_URL=... ./scripts/restore.sh backups/erpdm_X.dump
set -euo pipefail
: "${DATABASE_URL:?DATABASE_URL is required}"
FILE="${1:?usage: restore.sh <dump file>}"
URL="${DATABASE_URL/postgresql+psycopg2:/postgresql:}"
read -r -p "This will overwrite objects in the target database. Continue? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || exit 1
pg_restore --clean --if-exists --no-owner --dbname "$URL" "$FILE"
echo "Restore complete. Run: alembic upgrade head"

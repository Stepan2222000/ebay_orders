#!/usr/bin/env bash
# Накат всех миграций из db/migrations/ в порядке имени файла.
# Каждая миграция применяется в одной транзакции (psql -1 -v ON_ERROR_STOP=1)
# и фиксируется в schema_migrations.

set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -X -q -v ON_ERROR_STOP=1)
export PGPASSWORD="$POSTGRES_PASSWORD"

"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);" >/dev/null

shopt -s nullglob
applied_any=0
for f in db/migrations/*.sql; do
    ver=$(basename "$f" .sql)
    exists=$("${PSQL[@]}" -tA -c "SELECT 1 FROM schema_migrations WHERE version='$ver'")
    if [[ "$exists" == "1" ]]; then
        echo "skip  $ver (already applied)"
        continue
    fi
    echo "apply $ver"
    "${PSQL[@]}" -1 -f "$f"
    "${PSQL[@]}" -c "INSERT INTO schema_migrations(version) VALUES ('$ver');" >/dev/null
    applied_any=1
done

if [[ $applied_any -eq 0 ]]; then
    echo "nothing to apply"
fi

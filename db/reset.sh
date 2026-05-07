#!/usr/bin/env bash
# DEV-only: полностью сносит схему public и накатывает миграции заново.
# Используется только локально на разработческой БД.

set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -X -q -v ON_ERROR_STOP=1)
export PGPASSWORD="$POSTGRES_PASSWORD"

read -r -p "Drop schema public on $PGHOST:$PGPORT/$PGDATABASE and reapply? [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted"; exit 1; }

"${PSQL[@]}" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
exec ./db/apply.sh

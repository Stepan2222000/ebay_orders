#!/usr/bin/env bash
# Thin psql wrapper. Loads .env from project root, forwards args to psql.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing ${ENV_FILE} — copy .env.example and fill it in" >&2
  exit 2
fi

set -a
# shellcheck source=/dev/null
. "${ENV_FILE}"
set +a

: "${PGHOST:?PGHOST not set}"
: "${PGPORT:?PGPORT not set}"
: "${PGUSER:?PGUSER not set}"
: "${PGDATABASE:?PGDATABASE not set}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set}"

export PGPASSWORD="${POSTGRES_PASSWORD}"

exec psql --no-psqlrc -v ON_ERROR_STOP=1 "$@"

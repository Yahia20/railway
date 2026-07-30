#!/usr/bin/env bash
# Apply migrations in order, stopping at the first error.
#
#   ./db/migrate.sh "$DATABASE_PUBLIC_URL"
#
# Run from your laptop against Railway's PUBLIC url — the private
# *.railway.internal host only resolves inside the Railway network.
set -euo pipefail

DB_URL="${1:-${DATABASE_URL:-}}"
if [[ -z "$DB_URL" ]]; then
  echo "usage: $0 <database-url>   (or set DATABASE_URL)" >&2
  exit 2
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/migrations"

# Each file is idempotent only as a whole, not per-statement: re-running 002 on
# a populated database fails on the duplicate CREATE TABLE, which is the
# intended behaviour. Track what has been applied rather than re-running blind.
psql "$DB_URL" -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename    text PRIMARY KEY,
  applied_at  timestamptz NOT NULL DEFAULT now()
);
SQL

for f in "$DIR"/*.sql; do
  name="$(basename "$f")"
  applied=$(psql "$DB_URL" -tAc \
    "SELECT 1 FROM schema_migrations WHERE filename = '$name'")
  if [[ "$applied" == "1" ]]; then
    echo "skip    $name"
    continue
  fi
  echo "apply   $name"
  psql "$DB_URL" -v ON_ERROR_STOP=1 -q -1 -f "$f"
  psql "$DB_URL" -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO schema_migrations (filename) VALUES ('$name')"
done

echo
echo "done. tables:"
psql "$DB_URL" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"

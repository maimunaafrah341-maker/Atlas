#!/bin/sh
# Container entrypoint: bring the schema to head, optionally seed, then serve.
#
# - `set -e`: any failing step stops startup immediately, so a failed migration or seed
#   fails the deploy visibly (Render keeps the previous version live).
# - Seeding runs only AFTER migrations succeed, and only when SEED_CATALOG_ON_START=1 (ADR-022).
#   The seed is create-only, runs in one transaction, and itself refuses to run unless the
#   schema is at the migration head.
# - Migrations run here (not in a pre-deploy hook) because the free plan does not rely on one.
#   Atlas runs as a single instance, so there is never more than one migrator.
set -eu

cd "${ATLAS_BACKEND_DIR:-/app/backend}"

echo '{"level":"INFO","message":"running database migrations"}'
alembic upgrade head

if [ "${SEED_CATALOG_ON_START:-0}" = "1" ]; then
  echo '{"level":"INFO","message":"seeding demo catalogue (create-only)"}'
  python -m app.seed
else
  echo '{"level":"INFO","message":"catalogue seed skipped (SEED_CATALOG_ON_START is not 1)"}'
fi

exec uvicorn app.main:app_factory --factory \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --no-access-log

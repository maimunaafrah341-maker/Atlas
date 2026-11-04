#!/bin/sh
# Container entrypoint: bring the schema to head, then serve.
# Migrations run here (not in a pre-deploy hook) because the free plan does not rely on one.
# Atlas runs as a single instance, so there is never more than one migrator.
set -eu

cd /app/backend

echo '{"level":"INFO","message":"running database migrations"}'
alembic upgrade head

exec uvicorn app.main:app_factory --factory \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --no-access-log

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DB_OWNER_PASSWORD=$(grep '^DB_OWNER_PASSWORD=' .env.staging | cut -d= -f2)
DB_PASSWORD=$(grep '^DB_PASSWORD=' .env.staging | cut -d= -f2)
export DB_OWNER_PASSWORD DB_PASSWORD
exec env DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=career_test \
  REDIS_HOST=127.0.0.1 REDIS_PORT=6380 \
  SALLA_WEBHOOK_SECRET=drill WHATSAPP_APP_SECRET=drill \
  WHATSAPP_VERIFY_TOKEN=drill \
  .venv/bin/python scripts/drill_cv_analysis.py "$@"

#!/usr/bin/env bash
# Jalankan migrasi Alembic ke Cloud SQL dari VM.
#
# Usage:
#   bash deploy/vm/migrate-vm.sh            # upgrade head
#   bash deploy/vm/migrate-vm.sh current    # cek revisi yang terpasang
#   bash deploy/vm/migrate-vm.sh <command>  # perintah alembic lain
#
# Prasyarat:
#   - Service account VM punya role Cloud SQL Client
#   - CLOUDSQL_INSTANCE, DB_NAME, DB_USER, DB_PASS, CLOUDSQL_IP_TYPE ada di env
#     atau dibaca dari services/extraction/.env

set -euo pipefail

cd "$(dirname "$0")/../.."

ALEMBIC_CMD="${1:-upgrade head}"

# Baca env dari extraction/.env kalau tidak di-set dari luar
if [ -f services/extraction/.env ]; then
  set -a
  # shellcheck disable=SC1091
  source <(grep -v '^#' services/extraction/.env | grep -v '^$')
  set +a
fi

: "${CLOUDSQL_INSTANCE:?CLOUDSQL_INSTANCE harus di-set}"
: "${DB_NAME:?DB_NAME harus di-set}"

echo "=== Migrasi DB ==="
echo "Instance : $CLOUDSQL_INSTANCE"
echo "Database : $DB_NAME"
echo "Command  : alembic $ALEMBIC_CMD"
echo ""

docker compose -f docker-compose.vm.yml run --rm \
  --no-deps \
  -e CLOUDSQL_INSTANCE="$CLOUDSQL_INSTANCE" \
  -e DB_NAME="$DB_NAME" \
  -e DB_USER="${DB_USER:-}" \
  -e DB_PASS="${DB_PASS:-}" \
  -e CLOUDSQL_IP_TYPE="${CLOUDSQL_IP_TYPE:-PRIVATE}" \
  extraction \
  sh -c "python -m alembic -c /app/db/alembic.ini $ALEMBIC_CMD" 2>&1 \
  || {
    echo ""
    echo "GAGAL. Pastikan:"
    echo "  1. Service account VM punya role Cloud SQL Client"
    echo "  2. CLOUDSQL_INSTANCE dan DB_NAME benar"
    echo "  3. Image sudah dibangun: docker compose -f docker-compose.vm.yml build extraction"
    exit 1
  }

echo ""
echo "Migrasi selesai."

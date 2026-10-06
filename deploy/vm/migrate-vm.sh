#!/usr/bin/env bash
# Jalankan migrasi Alembic ke Cloud SQL dari VM.
#
# Usage:
#   bash deploy/vm/migrate-vm.sh            # upgrade head
#   bash deploy/vm/migrate-vm.sh current    # cek revisi yang terpasang
#   bash deploy/vm/migrate-vm.sh <command>  # perintah alembic lain
#
# Prasyarat:
#   - DATABASE_URL ada di env atau dibaca dari services/extraction/.env
#     (koneksi langsung ke Postgres via private IP, tanpa Cloud SQL connector)

set -euo pipefail

cd "$(dirname "$0")/../.."

ALEMBIC_CMD="${1:-upgrade head}"

# Baca env dari extraction/.env kalau tidak di-set dari luar.
# `tr -d '\r'` lebih dulu: .env yang dibuat/di-scp dari Windows berakhiran CRLF, dan tanpa ini
# baris kosongnya tersisa sebagai `\r` yang di-source jadi perintah ("$'\r': command not found").
if [ -f services/extraction/.env ]; then
  set -a
  # shellcheck disable=SC1091
  source <(tr -d '\r' < services/extraction/.env | grep -v '^#' | grep -v '^$')
  set +a
fi

: "${DATABASE_URL:?DATABASE_URL harus di-set (lihat services/extraction/.env)}"

echo "=== Migrasi DB ==="
echo "Command  : alembic $ALEMBIC_CMD"
echo ""

docker compose -f docker-compose.vm.yml run --rm \
  --no-deps \
  -e DATABASE_URL="$DATABASE_URL" \
  extraction \
  sh -c "python -m alembic -c /app/db/alembic.ini $ALEMBIC_CMD" 2>&1 \
  || {
    echo ""
    echo "GAGAL. Pastikan:"
    echo "  1. DATABASE_URL benar dan VM bisa menjangkau private IP-nya (port 5432)"
    echo "  2. Image sudah dibangun: docker compose -f docker-compose.vm.yml build extraction"
    exit 1
  }

echo ""
echo "Migrasi selesai."

#!/usr/bin/env bash
# Deploy semua service di VM menggunakan docker-compose.vm.yml.
#
# Usage:
#   bash deploy/vm/deploy-vm.sh           # build + up semua service
#   bash deploy/vm/deploy-vm.sh --no-build  # up tanpa build ulang
#   bash deploy/vm/deploy-vm.sh <service>   # build + restart satu service saja
#
# Prasyarat:
#   - Docker & docker compose plugin terinstall
#   - Repo di-clone ke VM (atau scp dari lokal)
#   - Service account VM punya role: Cloud SQL Client, Storage Object Viewer
#   - DB sudah dimigrasikan (bash deploy/vm/migrate-vm.sh)
#   - File .env tiap service sudah ada (lihat pengecekan di bawah)

set -euo pipefail

COMPOSE="docker compose -f docker-compose.vm.yml"
BUILD=true
SERVICE=""

for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=false ;;
    --*)        echo "Flag tidak dikenal: $arg" >&2; exit 1 ;;
    *)          SERVICE="$arg" ;;
  esac
done

cd "$(dirname "$0")/../.."

echo "=== nlm-k2 VM deploy ==="

# ---------------------------------------------------------------------------
# Cek .env — harus ada sebelum docker compose bisa inject env vars ke container
# ---------------------------------------------------------------------------
ALL_SERVICES=(orchestrator guardrails extraction structuring scoring)

# Kalau deploy satu service, cek hanya service itu; kalau semua, cek semua.
if [ -n "$SERVICE" ]; then
  SERVICES_TO_CHECK=("$SERVICE")
else
  SERVICES_TO_CHECK=("${ALL_SERVICES[@]}")
fi

MISSING=()
for svc in "${SERVICES_TO_CHECK[@]}"; do
  ENV_FILE="services/$svc/.env"
  if [ ! -f "$ENV_FILE" ]; then
    MISSING+=("$ENV_FILE")
  fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
  echo ""
  echo "ERROR: File .env berikut tidak ditemukan:"
  for f in "${MISSING[@]}"; do
    echo "  - $f"
  done
  echo ""
  echo "Buat file tersebut terlebih dahulu, lalu jalankan ulang script ini."
  echo "Contoh isi minimal (salin dari services/<nama>/.env.example jika ada):"
  echo ""
  echo "  CLOUDSQL_INSTANCE=<project>:<region>:<instance>"
  echo "  DB_NAME=bribrain_ocr"
  echo "  DB_USER="
  echo "  DB_PASS="
  echo "  CLOUDSQL_IP_TYPE=PRIVATE"
  echo "  API_KEY=<isi-api-key>"
  echo "  ENVIRONMENT=production"
  exit 1
fi

echo ">> Cek .env : OK (semua file ditemukan)"

# ---------------------------------------------------------------------------
# Cek variabel wajib di tiap .env yang sudah ada
# ---------------------------------------------------------------------------
INVALID=()
for svc in "${SERVICES_TO_CHECK[@]}"; do
  ENV_FILE="services/$svc/.env"
  # Baca nilai; strip komentar dan baris kosong
  get_val() {
    grep -v '^#' "$ENV_FILE" | grep -v '^$' | grep "^${1}=" | cut -d'=' -f2- | tr -d '[:space:]'
  }
  CLOUDSQL_INSTANCE=$(get_val CLOUDSQL_INSTANCE)
  DB_NAME=$(get_val DB_NAME)
  API_KEY=$(get_val API_KEY)
  if [ -z "$CLOUDSQL_INSTANCE" ] || [ -z "$DB_NAME" ] || [ -z "$API_KEY" ]; then
    INVALID+=("$ENV_FILE (CLOUDSQL_INSTANCE / DB_NAME / API_KEY belum diisi)")
  fi
done

if [ ${#INVALID[@]} -gt 0 ]; then
  echo ""
  echo "ERROR: Variabel wajib belum diisi di:"
  for f in "${INVALID[@]}"; do
    echo "  - $f"
  done
  exit 1
fi

echo ">> Validasi .env : OK (variabel wajib terisi)"
echo ""

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [ "$BUILD" = true ]; then
  if [ -n "$SERVICE" ]; then
    echo ">> Build: $SERVICE"
    $COMPOSE build "$SERVICE"
  else
    echo ">> Build: semua service"
    $COMPOSE build
  fi
fi

# ---------------------------------------------------------------------------
# Up
# ---------------------------------------------------------------------------
if [ -n "$SERVICE" ]; then
  echo ">> Restart: $SERVICE"
  $COMPOSE up -d --no-build "$SERVICE"
else
  echo ">> Up: semua service"
  $COMPOSE up -d --no-build
fi

echo ""
echo "=== Status ==="
$COMPOSE ps

echo ""
echo "=== Health check ==="
sleep 5
for port in 8040 8041 8042 8043 8044; do
  if curl -sf "http://localhost:$port/health" -o /dev/null 2>&1; then
    echo "  :$port  OK"
  else
    echo "  :$port  BELUM SIAP (mungkin masih loading weights/model)"
  fi
done

echo ""
echo "Lihat log: docker compose -f docker-compose.vm.yml logs -f"

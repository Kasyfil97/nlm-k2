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

if [ "$BUILD" = true ]; then
  if [ -n "$SERVICE" ]; then
    echo ">> Build: $SERVICE"
    $COMPOSE build "$SERVICE"
  else
    echo ">> Build: semua service"
    $COMPOSE build
  fi
fi

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

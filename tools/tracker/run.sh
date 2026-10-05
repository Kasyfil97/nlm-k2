#!/usr/bin/env bash
# Jalankan tracker (backend 8090 + Vite 5173) dari Git Bash / WSL / Linux.
#
#   tools/tracker/run.sh            backend + frontend; Ctrl+C mematikan semuanya
#   tools/tracker/run.sh --stack    plus: nyalakan Redis + kelima container + Postgres dulu
#
# Target diatur di tools/tracker/.env:
#   blok GKE aktif      -> port-forward ke namespace nlm-k2, hasil tiap
#                          tahap diambil dengan polling (callback dari pod tidak
#                          bisa masuk ke laptop)
#   blok GKE dikomentari -> stack lokal seperti biasa, callback langsung
#
# Prasyarat yang diperiksa: Redis di 6379 dan kelima service /health. Yang tidak
# terpenuhi hanya diperingatkan, kecuali Redis.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BACKEND_PORT="${PORT:-8090}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
export POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-5433}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mperingatan:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mgagal:\033[0m %s\n' "$*" >&2; exit 1; }

up() { curl -s -m 3 -o /dev/null "$1"; }

# PID Windows (netstat) atau Linux (ss/lsof) yang sedang LISTEN di port; kosong jika bebas.
port_owner() {
  if command -v netstat >/dev/null 2>&1 && [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
    netstat -ano 2>/dev/null | awk -v p=":$1" '$1=="TCP" && $2 ~ p"$" && $4=="LISTENING" {print $5; exit}'
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$1" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1
  elif command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1
  fi
}
port_free_or_die() {
  local port="$1" what="$2" pid
  pid="$(port_owner "$port")" || true
  [[ -z "$pid" ]] && return 0
  if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
    die "port $port ($what) sudah dipakai PID $pid. Sisa run sebelumnya? Matikan: taskkill //F //T //PID $pid"
  else
    die "port $port ($what) sudah dipakai PID $pid. Sisa run sebelumnya? Matikan: kill $pid"
  fi
}

# --- konfigurasi -------------------------------------------------------------
if [[ -f "$HERE/.env" ]]; then
  set -a; . "$HERE/.env"; set +a
fi
TRACKER_TARGET="${TRACKER_TARGET:-local}"

# Mode GKE: port-forward orchestrator ada di 9040 (lihat di bawah); .env lama belum punya barisnya.
if [[ "$TRACKER_TARGET" == "gke" ]]; then
  ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:9040}"
else
  ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:8040}"
fi
GUARDRAILS_URL="${GUARDRAILS_URL:-http://127.0.0.1:8041}"
EXTRACTION_URL="${EXTRACTION_URL:-http://127.0.0.1:8042}"
STRUCTURING_URL="${STRUCTURING_URL:-http://127.0.0.1:8043}"
SCORING_URL="${SCORING_URL:-http://127.0.0.1:8044}"
export ORCHESTRATOR_URL GUARDRAILS_URL EXTRACTION_URL STRUCTURING_URL SCORING_URL

if [[ "${1:-}" == "--stack" && "$TRACKER_TARGET" == "gke" ]]; then
  die "--stack hanya untuk mode lokal; komentari blok GKE di .env dulu"
fi

if [[ "${1:-}" == "--stack" ]]; then
  say "menyalakan Redis + container pipeline + Postgres"
  docker start nlm-k2-redis >/dev/null 2>&1 \
    || docker run -d --name nlm-k2-redis --restart unless-stopped -p 127.0.0.1:6379:6379 redis:7.4-alpine >/dev/null
  (cd "$ROOT" && docker compose -f docker-compose.yml -f docker-compose.db.yml up -d)
fi

# --- prasyarat ---------------------------------------------------------------
docker exec nlm-k2-redis redis-cli ping >/dev/null 2>&1 \
  || die "Redis tidak jalan. Jalankan dengan --stack, atau: docker start nlm-k2-redis"

PF_PIDS=()
if [[ "$TRACKER_TARGET" == "gke" ]]; then
  NS="${GKE_NAMESPACE:-nlm-k2}"
  RELEASE="${GKE_RELEASE:-nilam-ocr-kk}"
  command -v kubectl >/dev/null 2>&1 || die "kubectl tidak ada di PATH"
  kubectl -n "$NS" get svc >/dev/null 2>&1 \
    || die "tidak bisa membaca namespace $NS. Jalankan: gcloud container clusters get-credentials gc-ddb-dev-gke-cluster-01 --project ddb-kubecluster-dev-01 --location asia-southeast2"

  : >"$HERE/.port-forward.log"
  # Chart deploy/helm/nlm-k2: satu Deployment + Service per service (<release>-<nama>), satu port-forward per
  # service. Orchestrator adalah pintu masuk yang dipanggil tracker.
  say "target GKE: $NS/svc/$RELEASE-{orchestrator,guardrails,extraction,structuring,scoring} -> 127.0.0.1:9040-9044"
  for pair in "orchestrator|9040:8040" "guardrails|9041:8041" "extraction|9042:8042" "structuring|9043:8043" "scoring|9044:8044"; do
    svc="${pair%%|*}"; ports="${pair##*|}"
    kubectl -n "$NS" get "svc/$RELEASE-$svc" >/dev/null 2>&1 \
      || die "svc/$RELEASE-$svc tidak ada di $NS. Belum di-deploy? Deploy dulu dari main: deploy/helm/deploy.sh all"
    kubectl -n "$NS" port-forward "svc/$RELEASE-$svc" "$ports" >>"$HERE/.port-forward.log" 2>&1 &
    PF_PIDS+=($!)
  done

  for _ in $(seq 1 30); do
    up "$ORCHESTRATOR_URL/health" && up "$EXTRACTION_URL/health" && up "$GUARDRAILS_URL/health" && up "$STRUCTURING_URL/health" && up "$SCORING_URL/health" && break
    sleep 0.5
  done
  up "$ORCHESTRATOR_URL/health" && up "$EXTRACTION_URL/health" && up "$GUARDRAILS_URL/health" && up "$STRUCTURING_URL/health" && up "$SCORING_URL/health" \
    || die "port-forward tidak siap; lihat $HERE/.port-forward.log"
  say "hasil tiap tahap diambil dengan polling (TRACKER_POLL=${TRACKER_POLL:-0}); callback pod tetap ke Orkestrasi di cluster"
else
  say "target lokal: container di 127.0.0.1:8040-8044"
  # Compose membaca services/<nama>/.env.local. Backend mock = OCR sintetis dari isi file, bukan model sungguhan;
  # backend paddle memanggil server OCR tim ML (EXTRACTION_OCR_URL), yang harus terjangkau dari container.
  backend="$(sed -n 's/^EXTRACTION_BACKEND=//p' "$ROOT/services/extraction/.env.local" 2>/dev/null | tail -1)"
  say "extraction: EXTRACTION_BACKEND=${backend:-(bawaan)} (services/extraction/.env.local)"
fi

for svc in "orchestrator|$ORCHESTRATOR_URL" "guardrails|$GUARDRAILS_URL" "extraction|$EXTRACTION_URL" "structuring|$STRUCTURING_URL" "scoring|$SCORING_URL"; do
  name="${svc%%|*}"; url="${svc##*|}"
  up "$url/health" || warn "service $name ($url) tidak menjawab; tahap itu akan gagal"
done

python -c "import fastapi, httpx, redis" 2>/dev/null \
  || { say "memasang dependency backend"; python -m pip install -q -r "$HERE/backend/requirements.txt"; }
[[ -d "$HERE/frontend/node_modules" ]] \
  || { say "memasang dependency frontend"; (cd "$HERE/frontend" && npm install --no-audit --no-fund); }

# --- jalankan ------------------------------------------------------------------
port_free_or_die "$BACKEND_PORT" backend
port_free_or_die "$FRONTEND_PORT" frontend

cleanup() {
  say "mematikan tracker"
  kill "${BACKEND_PID:-}" "${FRONTEND_PID:-}" "${PF_PIDS[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

say "backend  -> http://127.0.0.1:$BACKEND_PORT"
PORT="$BACKEND_PORT" python "$HERE/backend/app.py" &
BACKEND_PID=$!

say "frontend -> http://127.0.0.1:$FRONTEND_PORT"
# exec node langsung (bukan lewat npm.cmd) supaya $! adalah proses Vite itu sendiri
# dan kill di cleanup benar-benar melepas port 5173 di Windows.
(cd "$HERE/frontend" && exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort) &
FRONTEND_PID=$!

for _ in $(seq 1 20); do
  up "http://127.0.0.1:$BACKEND_PORT/api/health" && up "http://127.0.0.1:$FRONTEND_PORT/" && break
  kill -0 "$BACKEND_PID" 2>/dev/null || die "backend mati; lihat log di atas"
  kill -0 "$FRONTEND_PID" 2>/dev/null || die "frontend (Vite) mati; lihat log di atas"
  sleep 0.5
done
up "http://127.0.0.1:$BACKEND_PORT/api/health" || die "backend tidak menjawab; lihat log di atas"
up "http://127.0.0.1:$FRONTEND_PORT/" || die "frontend tidak menjawab; lihat log di atas"
say "siap. Buka http://127.0.0.1:$FRONTEND_PORT  (Ctrl+C untuk berhenti)"
wait

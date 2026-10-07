#!/usr/bin/env bash
# Kelima service sebagai proses uvicorn biasa di mesin ini: tanpa image, tanpa Docker, tanpa database.
#
#   scripts/run_local.sh setup     # buat .venv (Python 3.11, uv) dan pasang dependensinya
#   scripts/run_local.sh up        # jalankan kelimanya di $HOST:8040-8044 (bawaan 0.0.0.0) dan tunggu sampai sehat
#   scripts/run_local.sh status    # siapa yang hidup
#   scripts/run_local.sh logs <service>
#   scripts/run_local.sh down      # hentikan
#
#   curl -X POST http://127.0.0.1:8040/v1/extract-ocr -H "X-API-Key: local-test" \
#        -F request_id=REQ_LOCAL_001 -F file=@test/data/kk_true.jpg
#
# Backend: guardrails `mock` (bobot K2Quality tidak ada di sini), extraction `paddle` ke server PaddleOCR v6 tim
# ML (OCR_URL), structuring `kk_model` + scoring `kk_field` dengan bobot di services/*/weights/. Tanpa
# DATABASE_URL tiap service menyimpan job di memori (hanya boleh di ENVIRONMENT=local): restart = job hilang,
# dan tahap-tahap saling menyerahkan job lewat HTTP. Env di bawah menimpa services/<nama>/.env kalau ada.
#
# Dependensi = requirements-dev.txt tanpa torch/torchvision/opencv: hanya backend `kk_quality` guardrails yang
# memakainya, dan ia memuatnya saat dibangun, bukan saat diimpor.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
RUN_DIR="${RUN_DIR:-/tmp/nlm-k2-local}"
API_KEY="${API_KEY:-local-test}"
OCR_URL="${OCR_URL:-http://10.213.128.67:8070}"
# 0.0.0.0: terjangkau dari luar VM. Antar-service tetap lewat 127.0.0.1 (bawaan *_SERVICE_URL).
HOST="${HOST:-0.0.0.0}"
SERVICES=(guardrails extraction structuring scoring orchestrator)
declare -A PORT=([orchestrator]=8040 [guardrails]=8041 [extraction]=8042 [structuring]=8043 [scoring]=8044)

service_env() {
  local common=(API_KEY="$API_KEY" ENVIRONMENT=local DATABASE_URL= PORT="${PORT[$1]}")
  case "$1" in
    orchestrator) echo "${common[@]}" PIPELINE_WAIT_SECONDS=60 ;;
    guardrails) echo "${common[@]}" GUARDRAILS_BACKEND=mock ;;
    extraction)
      echo "${common[@]}" EXTRACTION_BACKEND=paddle EXTRACTION_OCR_URL="$OCR_URL" EXTRACTION_OCR_TIMEOUT_SECONDS=60
      ;;
    structuring)
      echo "${common[@]}" STRUCTURING_BACKEND=kk_model STRUCTURING_MODEL_PATH=weights/kk_structuring_model.joblib
      ;;
    scoring) echo "${common[@]}" SCORING_BACKEND=kk_field SCORING_MODEL_PATH=weights/kk_trust_model.joblib ;;
  esac
}

running() { [ -f "$RUN_DIR/$1.pid" ] && kill -0 "$(cat "$RUN_DIR/$1.pid")" 2>/dev/null; }

case "${1:-}" in
  setup)
    command -v uv >/dev/null || { echo "uv tidak ditemukan" >&2; exit 1; }
    [ -d "$VENV" ] || uv venv --python 3.11 "$VENV"
    reqs="$(mktemp)"
    trap 'rm -f "$reqs"' EXIT
    # requirements-dev.txt dengan -r service dibuka, tanpa paket model guardrails `kk_quality`.
    (cd "$ROOT" && for f in services/{orchestrator,guardrails,extraction,structuring,scoring}/requirements.txt; do
      grep -vE '^\s*(#|$)|^(torch|torchvision|opencv-python-headless)==' "$f"
    done; grep -vE '^\s*(#|$|-r |--extra-index-url)' requirements-dev.txt) | sed 's/ *#.*//' | sort -u >"$reqs"
    (cd "$ROOT" && VIRTUAL_ENV="$VENV" uv pip install -q -r "$reqs")
    "$VENV/bin/python" -c "import ocr_common, sklearn, xgboost, fastapi; print('venv siap:', '$VENV')"
    ;;
  up)
    [ -x "$VENV/bin/python" ] || { echo "jalankan dulu: $0 setup" >&2; exit 1; }
    mkdir -p "$RUN_DIR"
    for s in "${SERVICES[@]}"; do
      if running "$s"; then echo "$s sudah hidup (pid $(cat "$RUN_DIR/$s.pid"))"; continue; fi
      # `exec`: the subshell becomes uvicorn, so `$!` is its pid and nothing else holds this script's stdout.
      # shellcheck disable=SC2046
      (cd "$ROOT/services/$s" && exec env $(service_env "$s") "$VENV/bin/python" -m uvicorn app.main:app \
        --host "$HOST" --port "${PORT[$s]}") </dev/null >"$RUN_DIR/$s.log" 2>&1 &
      echo $! >"$RUN_DIR/$s.pid"
    done
    for s in "${SERVICES[@]}"; do
      for _ in $(seq 60); do
        curl -fs "http://127.0.0.1:${PORT[$s]}/health" >/dev/null && continue 2
        running "$s" || break
        sleep 1
      done
      echo "$s (:${PORT[$s]}) tidak sehat; ekor lognya:" >&2
      tail -20 "$RUN_DIR/$s.log" >&2
      exit 1
    done
    echo "siap di $HOST: orchestrator :8040, guardrails :8041, extraction :8042, structuring :8043, scoring :8044"
    echo "API_KEY=$API_KEY, log di $RUN_DIR/<service>.log"
    ;;
  status)
    for s in "${SERVICES[@]}"; do
      if running "$s"; then echo "$s :${PORT[$s]} hidup (pid $(cat "$RUN_DIR/$s.pid"))"; else echo "$s :${PORT[$s]} mati"; fi
    done
    ;;
  logs)
    tail -f "$RUN_DIR/${2:?pakai: $0 logs <service>}.log"
    ;;
  down)
    for s in "${SERVICES[@]}"; do
      if running "$s"; then kill "$(cat "$RUN_DIR/$s.pid")"; echo "$s dihentikan"; fi
      rm -f "$RUN_DIR/$s.pid"
    done
    ;;
  *)
    echo "pakai: $0 setup|up|status|logs <service>|down" >&2
    exit 2
    ;;
esac

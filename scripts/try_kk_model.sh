#!/usr/bin/env bash
# Coba pasangan model structuring `kk_model` + scoring `kk_field` dengan bobot LOKAL, sebelum diunggah ke GCS.
#
#   scripts/try_kk_model.sh up      # build + jalankan extraction, structuring, scoring (127.0.0.1:18042-18044)
#   python scripts/try_kk_model.py test/data/kk_true.jpg ...
#   scripts/try_kk_model.sh down    # hentikan dan hapus containernya
#
# Bobot dibaca dari services/{structuring,scoring}/weights/ (di-mount read-only, diambil fetch_weights.py
# lewat *_MODEL_URI berupa path lokal -- jalur yang sama dengan gs:// di produksi). OCR memakai server
# PaddleOCR v6 tim ML (OCR_URL, bawaan seperti services/extraction/.env.example). Tidak butuh database:
# skrip python memakai endpoint sinkron/QC (`/v1/extraction/extract`, `/v1/structuring-direct`,
# `/v1/scoring-direct`), jadi tidak ada baris, callback, atau outbox yang tercipta.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OCR_URL="${OCR_URL:-http://10.213.128.67:8070}"
API_KEY="${API_KEY:-try-kk-model}"
PREFIX=try-kk-model

case "${1:-}" in
  up)
    for s in extraction structuring scoring; do
      docker build -q -f "$ROOT/services/$s/Dockerfile" -t "$PREFIX-$s:local" "$ROOT" >/dev/null
    done
    docker rm -f "$PREFIX-extraction" "$PREFIX-structuring" "$PREFIX-scoring" >/dev/null 2>&1 || true
    docker run -d --name "$PREFIX-extraction" -p 127.0.0.1:18042:8042 \
      -e API_KEY="$API_KEY" -e ENVIRONMENT=local -e EXTRACTION_BACKEND=paddle -e EXTRACTION_OCR_URL="$OCR_URL" \
      -e EXTRACTION_OCR_TIMEOUT_SECONDS=60 \
      -e EXTRACTION_OCR_QUERY='{"use_doc_orientation_classify": true, "use_doc_unwarping": false, "use_textline_orientation": false}' \
      "$PREFIX-extraction:local" sh -c 'exec uvicorn app.main:app --host 0.0.0.0 --port $PORT' >/dev/null
    # ^ CMD image extraction mengambil bobot `.pth` (backend kk_ocr) juga untuk `paddle`, yang tidak memakainya,
    #   dan keluar 2 tanpa EXTRACTION_WEIGHTS_URI -- dilewati di sini seperti docker-compose.override.yml.
    docker run -d --name "$PREFIX-structuring" -p 127.0.0.1:18043:8043 \
      -e API_KEY="$API_KEY" -e ENVIRONMENT=local -e STRUCTURING_BACKEND=kk_model \
      -e STRUCTURING_MODEL_URI=/src/kk_structuring_model.joblib \
      -v "$ROOT/services/structuring/weights:/src:ro" "$PREFIX-structuring:local" >/dev/null
    docker run -d --name "$PREFIX-scoring" -p 127.0.0.1:18044:8044 \
      -e API_KEY="$API_KEY" -e ENVIRONMENT=local -e SCORING_BACKEND=kk_field \
      -e SCORING_MODEL_URI=/src/kk_trust_model.joblib \
      -v "$ROOT/services/scoring/weights:/src:ro" "$PREFIX-scoring:local" >/dev/null
    for port in 18042 18043 18044; do
      for _ in $(seq 60); do
        curl -fs "http://127.0.0.1:$port/health" >/dev/null && continue 2
        sleep 1
      done
      echo "port $port tidak sehat setelah 60 dtk:" >&2
      docker ps -a --filter "name=$PREFIX" --format '{{.Names}} {{.Status}}' >&2
      exit 1
    done
    docker logs "$PREFIX-structuring" 2>&1 | grep -E "kk_model structurer ready" || { docker logs "$PREFIX-structuring" | tail; exit 1; }
    docker logs "$PREFIX-scoring" 2>&1 | grep -E "trust model loaded" || { docker logs "$PREFIX-scoring" | tail; exit 1; }
    echo "siap: extraction :18042, structuring :18043, scoring :18044 (API_KEY=$API_KEY)"
    ;;
  down)
    docker rm -f "$PREFIX-extraction" "$PREFIX-structuring" "$PREFIX-scoring" >/dev/null 2>&1 || true
    docker rmi "$PREFIX-extraction:local" "$PREFIX-structuring:local" "$PREFIX-scoring:local" >/dev/null 2>&1 || true
    echo "dihentikan"
    ;;
  *)
    echo "pakai: $0 up|down" >&2
    exit 2
    ;;
esac

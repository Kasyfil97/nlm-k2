# Base image dipin ke digest (manifest list, semua arsitektur) supaya build bisa diulang.
# Memperbarui: docker buildx imagetools inspect python:3.11-slim  -> salin "Digest" ke semua Dockerfile.
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

WORKDIR /app

# Dependensi dari lock file ber-hash (make lock): versi transitif dan isi setiap wheel terkunci.
COPY services/scoring/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# ocr_common dipasang tanpa resolusi dependensi: semua dependensinya sudah ada di lock.
# Disalin setelah dependensi supaya perubahan ocr_common tidak mengulang unduhan di atas.
COPY libs/ocr_common ./libs/ocr_common
RUN pip install --no-cache-dir --no-deps --no-build-isolation ./libs/ocr_common

RUN pip install --no-cache-dir google-cloud-storage

COPY scripts/fetch_weights.py ./scripts/fetch_weights.py
COPY scripts/gcs_wif.py ./scripts/gcs_wif.py
COPY services/scoring/app ./app

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p weights \
    && chown -R appuser /app
USER appuser

# Bobot TIDAK ikut image: diunduh dari SCORING_MODEL_URI setiap kali container start (lihat CMD).
# Default di bawah boleh ditimpa lewat env (.env, helm).
ENV PORT=8044 \
    WEIGHTS_DIR=/app/weights \
    SCORING_MODEL_URI=gs://gc-bribrain-dev-gcs-ocr-nilam-01/nilam-ocr-kk/scoring/kk_trust_model.joblib
EXPOSE 8044

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8044\")}/health', timeout=3)" || exit 1

# --force: unduh ulang setiap start, supaya restart container mengambil model terbaru di GCS.
CMD ["sh", "-c", "python scripts/fetch_weights.py scoring --force && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

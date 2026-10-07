# Base image dipin ke digest (manifest list, semua arsitektur) supaya build bisa diulang.
# Memperbarui: docker buildx imagetools inspect python:3.11-slim  -> salin "Digest" ke semua Dockerfile.
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

WORKDIR /app

# opencv-python-headless tidak butuh X11, tetapi wheel manylinux-nya masih menaut libglib. Tanpa ini
# `import cv2` gagal saat start, bukan saat build -- dan itu terlihat sebagai container yang mati
# berulang, bukan sebagai build merah. BELUM DIVERIFIKASI dengan build sungguhan (lihat catatan
# Unit 8: Docker tidak tersedia saat unit ini ditulis).
RUN apt-get update \
    && apt-get install --no-install-recommends -y libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Dependensi dari lock file ber-hash (make lock): torch/torchvision build +cpu dari index PyTorch,
# yang URL-nya tercatat di dalam lock, dan isi setiap wheel terkunci.
COPY services/guardrails/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# ocr_common dipasang tanpa resolusi dependensi: semua dependensinya sudah ada di lock.
# Disalin setelah dependensi supaya perubahan ocr_common tidak mengulang unduhan torch.
COPY libs/ocr_common ./libs/ocr_common
RUN pip install --no-cache-dir --no-deps --no-build-isolation ./libs/ocr_common

RUN pip install --no-cache-dir google-cloud-storage

COPY scripts/fetch_weights.py ./scripts/fetch_weights.py
COPY scripts/gcs_wif.py ./scripts/gcs_wif.py
COPY services/guardrails/app ./app

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p weights \
    && chown -R appuser /app
USER appuser

ENV PORT=8041 \
    GUARDRAILS_BACKEND=kk_quality \
    GUARDRAILS_WEIGHTS_DIR=weights \
    GUARDRAILS_DEVICE=cpu \
    WEIGHTS_DIR=/app/weights
EXPOSE 8041

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8041\")}/health', timeout=3)" || exit 1

CMD ["sh", "-c", "python scripts/fetch_weights.py guardrails && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

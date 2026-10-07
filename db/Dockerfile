# Base image dipin ke digest (manifest list, semua arsitektur) supaya build bisa diulang.
# Memperbarui: docker buildx imagetools inspect python:3.11-slim  -> salin "Digest" ke semua Dockerfile.
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

WORKDIR /app

COPY db/requirements.lock ./db/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r db/requirements.lock

COPY libs/ocr_common ./libs/ocr_common
RUN pip install --no-cache-dir --no-deps --no-build-isolation ./libs/ocr_common

COPY db ./db

# chown -R: file di-COPY sebagai root dengan mode sumbernya (0640 kalau repo di-clone dengan umask
# 027), jadi appuser tak bisa baca db/alembic.ini -> alembic diam-diam menganggap config kosong
# ("No 'script_location' key found"). Samakan dengan Dockerfile service lain.
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser /app
USER appuser

CMD ["python", "-m", "alembic", "-c", "db/alembic.ini", "upgrade", "head"]

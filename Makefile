SERVICES := orchestrator guardrails ekstraksi structuring scoring
PY ?= python
PORT_orchestrator := 8040
PORT_guardrails := 8041
PORT_ekstraksi := 8042
PORT_structuring := 8043
PORT_scoring := 8044

dev:
	$(PY) -m pip install -r requirements-dev.txt

# compose membaca services/<nama>/.env, yang di-gitignore. Tanpa berkas itu `make up` gagal dengan
# "env file not found" sebelum satu container pun dibangun -- menyalinnya satu per satu adalah
# footgun pertama yang ditemui siapa pun yang meng-clone repo ini.
env:
	@for s in $(SERVICES); do 		[ -f services/$$s/.env ] || { cp services/$$s/.env.example services/$$s/.env; echo "dibuat services/$$s/.env"; }; 	done
	@[ -f .env ] || { cp .env.example .env; echo "dibuat .env"; }
	@echo "Isi API_KEY di tiap services/<nama>/.env sebelum ENVIRONMENT selain local."

test: test-lib $(SERVICES:%=test-%)

# Lock file ber-hash untuk image Docker. Input: requirements.txt (pin langsung) + dependensi
# ocr_common dari pyproject.toml-nya; output: requirements.lock (semua versi transitif + hash,
# untuk Linux x86_64 / Python 3.11). Jalankan setelah mengubah salah satu input. Versi transitif
# yang sudah ada di lock dipertahankan (idempoten); menaikkannya: make lock LOCK_FLAGS=--upgrade
LOCK_FLAGS ?=
LOCK := $(PY) -m uv pip compile --generate-hashes --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --custom-compile-command "make lock" $(LOCK_FLAGS)
lock: $(SERVICES:%=lock-%) lock-db
lock-orchestrator:
	$(LOCK) services/orchestrator/requirements.txt libs/ocr_common/pyproject.toml -o services/orchestrator/requirements.lock
lock-guardrails:
	$(LOCK) services/guardrails/requirements.txt libs/ocr_common/pyproject.toml --extra-index-url https://download.pytorch.org/whl/cpu --emit-index-url -o services/guardrails/requirements.lock
# Ekstraksi dipisah dari aturan pola di bawahnya: backend OCR-nya memuat torch, dan indeks CPU
# PyTorch harus sudah ada di sini SEBELUM gerbang R6 membekukan Makefile -- kalau tidak, agen C
# tidak bisa meregenerasi locknya tanpa membuka beku lebih dulu.
lock-ekstraksi:
	$(LOCK) services/ekstraksi/requirements.txt libs/ocr_common/pyproject.toml --extra db \
		--extra-index-url https://download.pytorch.org/whl/cpu --emit-index-url \
		-o services/ekstraksi/requirements.lock
lock-structuring lock-scoring: lock-%:
	$(LOCK) services/$*/requirements.txt libs/ocr_common/pyproject.toml --extra db -o services/$*/requirements.lock
lock-db:
	$(LOCK) db/requirements.txt libs/ocr_common/pyproject.toml -o db/requirements.lock
# Gagal kalau ada lock yang ketinggalan dari requirements.txt / pyproject ocr_common.
#
# Dua hal yang tidak jelas dari bentuk aslinya:
#
#   `lock-check` meregenerasi KELIMA lock dan memeriksa semuanya, jadi definisi selesai satu agen
#   menyentuh berkas milik agen lain -- dan resolusi `uv pip compile` bergantung waktu, sehingga dua
#   agen bisa menghasilkan pin transitif berbeda untuk service yang bukan milik keduanya. Pakai
#   `lock-check-<service>` di gerbang R24; `lock-check` penuh milik integrator di fase 0 dan fase 2.
#
#   `test -z "$(git status ...)"` LOLOS di luar repo git: git menulis fatal ke stderr dan stdout
#   kosong, jadi gerbangnya hijau tanpa memeriksa apa pun. Karena itu keberadaan repo diperiksa dulu.
_require_git:
	@git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
		|| { echo "bukan repo git: lock-check akan lolos tanpa memeriksa apa pun. Jalankan 'git init'"; exit 1; }
lock-check: _require_git lock
	@test -z "$$(git status --porcelain -- services/*/requirements.lock db/requirements.lock)" \
		|| { git status --short -- services/*/requirements.lock db/requirements.lock; \
		echo "requirements.lock berubah atau belum di-commit; commit hasil 'make lock' di atas"; exit 1; }
lock-check-%: _require_git lock-%
	@test -z "$$(git status --porcelain -- services/$*/requirements.lock)" \
		|| { git status --short -- services/$*/requirements.lock; \
		echo "services/$*/requirements.lock berubah; commit hasil 'make lock-$*' di atas"; exit 1; }
# Gerbang R6: tidak ada sisa istilah pipeline lama di lingkup yang diperiksa. Lingkupnya
# penting -- lihat scripts/check_no_legacy_terms.py. `make check-legacy SCOPE=orchestrator`
# untuk satu service (gerbang R24). Namanya sengaja tidak memuat istilah yang dicarinya.
SCOPE ?= foundation
check-legacy:
	$(PY) scripts/check_no_legacy_terms.py $(SCOPE)
# Bawaannya seluruh repo. Dengan worktree per agen itu sudah cukup terisolasi -- pohon satu agen
# tidak memuat pekerjaan agen lain yang belum di-commit. `LINT_PATH` ada untuk memperpendek
# putaran saat mengerjakan satu service, bukan sebagai pengganti pemeriksaan penuh.
LINT_PATH ?= .
lint:
	$(PY) -m ruff check $(LINT_PATH)
	$(PY) -m ruff format --check $(LINT_PATH)
format:
	$(PY) -m ruff format $(LINT_PATH) && $(PY) -m ruff check --fix $(LINT_PATH)
typecheck: typecheck-lib $(SERVICES:%=typecheck-%)
openapi: $(SERVICES:%=openapi-%) openapi-gateway

test-lib:
	cd libs/ocr_common && $(PY) -m pytest -q
typecheck-lib:
	cd libs/ocr_common && $(PY) -m ty check ocr_common tests

test-%:
	cd services/$* && $(PY) -m pytest -q
typecheck-%:
	cd services/$* && $(PY) -m ty check app tests
openapi-%:
	cd services/$* && API_KEY=x ENVIRONMENT=local $(PY) -m ocr_common.web.openapi
openapi-gateway: $(SERVICES:%=openapi-%)
	$(PY) scripts/build_gateway_openapi.py
api-docs:
	$(PY) -m http.server 8088
run-%:
	cd services/$* && $(PY) -m uvicorn app.main:app --reload --port $(PORT_$*)

db-upgrade:
	$(PY) -m alembic -c db/alembic.ini upgrade head
db-check:
	$(PY) -m alembic -c db/alembic.ini check
db-revision:
	$(PY) -m alembic -c db/alembic.ini revision --autogenerate -m "$(m)"
db-external:
	$(PY) db/external/apply.py

# Keluar 2 = URI tidak di-set, yang bukan kegagalan: bobot memang tidak wajib untuk backend mock.
# Set <NAMA>_MODEL_URI ke direktori lokal atau awalan gs:// untuk benar-benar mengambilnya.
weights:
	$(PY) scripts/fetch_weights.py || [ $$? -eq 2 ]

build:
	docker compose build
up:
	docker compose up -d --build
up-%:
	docker compose up -d --build $*
up-db:
	docker compose -f docker-compose.yml -f docker-compose.db.yml up -d --build
down:
	docker compose -f docker-compose.yml -f docker-compose.db.yml down
ps:
	@docker ps --filter name=nlm-k2- --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
logs-%:
	docker compose logs -f $*

smoke:
	$(PY) scripts/smoke_e2e.py

.PHONY: dev env test _require_git check-legacy lint format typecheck lock lock-db lock-check openapi openapi-gateway api-docs test-lib typecheck-lib db-upgrade db-check db-revision db-external weights build up up-db down ps smoke

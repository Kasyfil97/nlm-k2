# nlm-k2

Pipeline OCR Kartu Keluarga. Lima service FastAPI yang berbagi satu pustaka, `libs/ocr_common`.

Strukturnya disalin dari `nilam-ocr-npwp` (pipeline OCR NPWP) dan disesuaikan untuk KK.
Kontraknya ada di [`docs/api-contract.md`](docs/api-contract.md).

| Service | Port | Peran |
|---|---|---|
| orchestrator | 8040 | satu-satunya pintu masuk; dipanggil Orkestrasi pusat |
| guardrails | 8041 | menilai kualitas gambar; selalu 200, vonis di `data.passed` |
| extraction | 8042 | OCR; tahap pertama pipeline asinkron |
| structuring | 8043 | membaca field dari baris OCR (model key per kotak); satu-satunya tahap yang boleh menolak isi |
| scoring | 8044 | trust model: P(field persis benar) per field; tahap terakhir, merakit hasil kontrak |

Extraction → structuring → scoring berjalan asinkron lewat outbox transaksional. Orchestrator
menunggu sebentar lalu menjawab 200, atau 202 kalau pipeline belum selesai.

## Status

Per pembaruan README ini:

| Service | Backend | Bobot | Keterangan |
|---|---|---|---|
| orchestrator | - | - | berjalan |
| guardrails | `kk_quality` (inti K2Quality) | enam artefak K2Quality + `crop_model.pt`, diunduh saat start | service menolak start tanpa artefaknya |
| extraction | `paddle` (dipakai Helm) | - | memanggil server PaddleOCR `POST /ocr` |
| structuring | `kk_model` (model key per kotak m04) | `services/structuring/weights/kk_structuring_model.joblib` | ada di repo |
| scoring | `kk_field` (trust model s11) | `services/scoring/weights/kk_trust_model.joblib` | ada di repo |

**Structuring dan scoring berpasangan.** Confidence `kk_field` hanya sah di atas structuring `kk_model`.
Ganti keduanya bersama, jangan salah satu saja.

> **Default di kode berbeda dengan deploy.** `structuring/app/config.py` masih memakai
> `STRUCTURING_BACKEND=mock` sebagai default. `mock` mengarang field dan mengabaikan teks OCR, dan hanya
> boleh jalan di `ENVIRONMENT=local`. `.env.example` dan Helm sudah menyetel `kk_model`. Pastikan
> `kk_model` aktif di setiap lingkungan selain lokal.

> **Batas uji ujung ke ujung.** Dokumen [`docs/decisions/2026-09-27-batas-uji-end-to-end.md`](docs/decisions/2026-09-27-batas-uji-end-to-end.md)
> ditulis saat structuring masih stub. Hijaunya `make smoke` membuktikan **pipanya** (transaksi,
> idempotensi, lease, outbox, dead letter, tabel outcome, bentuk kontrak). Dokumen itu **belum**
> membuktikan isi hasilnya untuk pasangan m04 + s11. Verifikasi itu perlu dilakukan ulang dengan model
> terpasang.

> **Angka confidence.** Trust model dilatih atas 1686 sel berlabel tangan dengan AUC 0.855 dan bin
> teratas 200/200 benar pada data held-out. Angka ini diukur di tingkat fitur pelatihan, bukan dari
> alur layanan end-to-end.

**Perubahan kontrak terakhir, selaras dengan nilam** (lihat
[`docs/decisions/2026-09-29-selaras-nilam.md`](docs/decisions/2026-09-29-selaras-nilam.md)):

- `pipeline_name_sequence` menggantikan `skip_guardrails`. Nama tahap OCR-nya `extraction`.
- Orchestrator mencatat setiap putusan guardrails di `nilam_ocr_kk.nilam_guardrails_results` (butuh `DATABASE_URL`, migrasi `0002`).
- PDF diterima bawaan; hanya halaman 1 yang dinilai dan dibaca.
- `EXTRACTION_BACKEND=paddle` memanggil server PaddleOCR di `POST /ocr`, dengan `poly` diteruskan utuh.
- Ada endpoint OCR sinkron `POST /v1/extraction/extract` yang menjawab `OcrPayload` §7.1.
- Tiap field `data` kini `{"value", "confidence": 0 | 1}` seperti nilam; `bin` dan `auto` dihapus.
- Threshold per request: `column_confidence_threshold` (per field kontrak), `guardrails_confidence_threshold`.
- `errors` selalu kode stabil (mis. `EMPTY_FILE`, `DOWNSTREAM_UNAVAILABLE`), dan setiap error membawa `pipeline_last_stage`.
- Jawaban `extract-ocr` tanpa `job_status`, `document_type`, `params`; kode HTTP yang menyatakan keadaan.
  `pipeline_last_stage` `null` pada 200/202, hanya menyebut asal error. Field form `params` diabaikan.
- Callback hasil sesuai kontrak Orkestrasi pusat: `result` = `data` jawaban 200, `guardrails` 0/1, penolakan
  sebagai `completed` + `result: null` + `guardrails: 1`, `409 RESULT_NOT_READY` dikirim ulang, batas umur
  `ORCHESTRATION_CALLBACK_MAX_AGE_SECONDS` (600), saklar `ORCHESTRATION_CALLBACK_ENABLED`.
- Endpoint QC satu tahap: `POST /v1/structuring-direct` dan `POST /v1/scoring-direct`.
- Semua tabel di schema `nilam_ocr_kk` dengan awalan `nilam_` (migrasi `0003`, lihat [`db/README.md`](db/README.md)).
- Tahap OCR menyimpan di `nilam_ocr_extraction_jobs`/`_results`; `nilam_ocr_results` menyimpan log jawaban (append-only)
  `extract-ocr` per `request_id` dari orchestrator (migrasi `0004`).
- Ambang hanya dari request: tanpa `guardrails_confidence_threshold` dokumen lolos guardrails (dengan
  `probability_bad`-nya); field tanpa `column_confidence_threshold` mendapat `confidence` berupa probabilitas
  (float), bukan 0/1.
- Upload dengan tipe yang tidak didukung (`application/octet-stream`, `jpg`, ...) dibaca dari tanda tangan berkasnya.
- Tools laptop: [`tools/tracker`](tools/tracker) (UI pipeline, outbox, callback, skenario gagal) dan
  [`tools/load-tester`](tools/load-tester) (k6).

Dokumen perencanaan:
- Requirements: [`docs/brainstorms/2026-09-26-nlm-k2-tiga-service-pertama-requirements.md`](docs/brainstorms/2026-09-26-nlm-k2-tiga-service-pertama-requirements.md)
- Rencana implementasi: [`docs/plans/2026-09-26-001-feat-nlm-k2-tiga-service-pertama-plan.md`](docs/plans/2026-09-26-001-feat-nlm-k2-tiga-service-pertama-plan.md)

## Mulai

```bash
make dev          # pasang dependensi pengembangan
make test         # pustaka + kelima service
make lint         # ruff
make up-db        # compose + PostgreSQL
make smoke        # uji ujung ke ujung
```

`make up` saja tidak menyalakan database; tahap-tahapnya butuh `make up-db`.

### Bobot model

Bobot tidak masuk git (gitignored), kecuali dua berkas structuring dan scoring di bawah yang ikut repo.

**Guardrails.** `services/guardrails/.env.example` menunjuk backend `kk_quality` (inti K2Quality), dan
service itu **menolak start** tanpa ketujuh artefaknya (enam dari K2Quality, plus `crop_model.pt` dari
`quality/crop_model` run `mnv3s_v1`). Ambil dulu:

```bash
GUARDRAILS_MODEL_URI=../K2Quality/models make weights
```

URI-nya boleh direktori lokal, `gs://`, `s3://`, atau `https://`; untuk entri banyak-berkas ia
diperlakukan sebagai awalan. Kelima artefak diverifikasi terhadap `model_hashes.json` yang ikut
diambil, dan artefak yang tidak lengkap atau tidak cocok dibatalkan alih-alih dipakai separuh.
`crop_model.pt` belum tercantum di `model_hashes.json` GCS: hash-nya ditambahkan bersamaan dengan deploy
kode yang mengunduhnya, karena `fetch_weights.py` versi lama akan gagal memverifikasi berkas yang tidak ia unduh.
Untuk pengembangan tanpa bobot sama sekali, setel `GUARDRAILS_BACKEND=mock` (hanya jalan di `ENVIRONMENT=local`).
Di container, ketujuh artefak diunduh ulang setiap start dari awalan
`gs://gc-bribrain-dev-gcs-ocr-nilam-01/nilam-ocr-kk/guardrails/` (`GUARDRAILS_MODEL_URI`).

**Structuring + scoring.** `STRUCTURING_BACKEND=kk_model` (model key per kotak m04) dan
`SCORING_BACKEND=kk_field` (trust model s11) dilatih bersama. Artefaknya ada di path yang sudah dibaca
masing-masing service dan ikut repo:

| service | path | dari |
|---|---|---|
| structuring | `services/structuring/weights/kk_structuring_model.joblib` | `STRUCTURING_MODEL_URI` |
| scoring | `services/scoring/weights/kk_trust_model.joblib` | `SCORING_MODEL_URI` |

Keduanya ditulis bersama oleh `scoring/training/export_nlm_k2.py` di ruang kerja training. Di container,
keduanya **diunduh ulang setiap container start** (`fetch_weights.py --force`, bukan saat build image) dari URI
masing-masing, jadi unggah kedua berkas itu bersamaan lalu restart kedua service. Default URI di image (bisa
ditimpa lewat env):

| service | URI |
|---|---|
| structuring | `gs://gc-bribrain-dev-gcs-ocr-nilam-01/nilam-ocr-kk/structuring/kk_structuring_model.joblib` |
| scoring | `gs://gc-bribrain-dev-gcs-ocr-nilam-01/nilam-ocr-kk/scoring/kk_trust_model.joblib` |

`make kk-model-parity` memastikan keluaran service sama persis dengan rantai training pada korpus
`../raw_ocr_v6`. Rinciannya di [`services/scoring/weights/README.md`](services/scoring/weights/README.md).

### Smoke test ujung ke ujung

`make smoke` menjalankan delapan jalur kontrak terhadap stack compose lokal. Ia **menolak jalan**
di luar `ENVIRONMENT=local` atau terhadap alamat non-lokal (R25a): ia membuat baris dan
menghapusnya lagi, termasuk di tabel outcome yang di produksi dimiliki Orkestrasi pusat.

Yang perlu disiapkan sekali, di ketiga `services/{extraction,structuring,scoring}/.env`:

```sh
ORCHESTRATION_OUTCOME_TABLE=orchestration_extract_ocr
PIPELINE_OUTBOX=true
PIPELINE_HANDOFF_BY_REFERENCE=true
```

lalu `make db-external` untuk membuat tabel tiruannya (DDL-nya di `db/external/`; tabel itu bukan
milik repo ini dan tidak disentuh migrasi Alembic).

Dua variabel opsional:

| Variabel | Untuk apa |
|---|---|
| `SMOKE_DATABASE_URL` | jalur tabel outcome dan pembersihan barisnya. Tanpa ini keduanya **dilewati**, bukan lulus |
| `SMOKE_DELAY_SECONDS` | lama tunda jalur 202; harus di atas `PIPELINE_WAIT_SECONDS` (bawaan 30) |

Jalur dead letter tidak bisa memicu keadaannya sendiri -- R8 melarang menambahkan hook baru ke
`ocr_common`, tempat outbox tinggal -- jadi ia memeriksa keadaan yang ada dan **melewati** diri
sendiri kalau tidak ada. Resepnya, yang sudah dijalankan dan terbukti:

```bash
printf 'PIPELINE_OUTBOX_MAX_AGE_SECONDS=2
PIPELINE_OUTBOX_INTERVAL_SECONDS=1
' >> services/extraction/.env
docker compose -f docker-compose.yml -f docker-compose.db.yml up -d --force-recreate extraction
docker stop nlm-k2-structuring-1          # penerima handoff dimatikan
# kirim satu job ke POST /v1/extraction/jobs, tunggu ~15 dtk
```

Baris outcome-nya jadi `failed` / `STRUCTURING` / `STRUCTURING_FAILED`, dan `GET /v1/extraction/outbox`
melaporkan `dead_letters: 1`. Setelah structuring dinyalakan lagi,
`POST /v1/extraction/outbox/release` mengirimkannya kembali: structuring dan scoring jadi `DONE` dan
baris outcome-nya maju ke `completed` / `SCORING`. Jangan lupa mengembalikan kedua kunci env itu.

## Database

nlm-k2 memakai database PostgreSQL sendiri di **instans tersendiri**, tidak berbagi instans dengan
nilam. Tabel tahap milik repo ini; tabel `orchestration_*` milik Orkestrasi pusat dan hidup di
database yang sama, sehingga penulisan outcome tetap satu transaksi dengan penyimpanan hasil.
Lihat [`db/README.md`](db/README.md).

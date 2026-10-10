# nlm-k2

Pipeline OCR Kartu Keluarga. Lima service FastAPI yang berbagi satu pustaka, `libs/ocr_common`.

Strukturnya disalin dari `nilam-ocr-npwp` (pipeline OCR NPWP) dan sedang disesuaikan untuk KK.
Kontraknya ada di [`docs/api-contract.md`](docs/api-contract.md).

| Service | Port | Peran |
|---|---|---|
| orchestrator | 8040 | satu-satunya pintu masuk; dipanggil Orkestrasi pusat |
| guardrails | 8041 | menilai kualitas gambar; selalu 200, vonis di `data.passed` |
| extraction | 8042 | OCR; tahap pertama pipeline asinkron |
| structuring | 8043 | membaca field dari baris OCR; satu-satunya tahap yang boleh menolak isi |
| scoring | 8044 | trust model: P(field persis benar), terkalibrasi + bin; tahap terakhir, merakit hasil kontrak |

Extraction → structuring → scoring berjalan asinkron lewat outbox transaksional. Orchestrator
menunggu sebentar lalu menjawab 200, atau 202 kalau pipeline belum selesai.

## Status

Batch pertama sedang berjalan: orchestrator, guardrails, dan extraction. **Structuring dan scoring
memakai pasangan model terlatih** (`STRUCTURING_BACKEND=kk_model` m04 + `SCORING_BACKEND=kk_field` s11,
artefak di `services/{structuring,scoring}/weights/`; lihat "Bobot model"). Catatan di bawah ini
ditulis sebelum pasangan itu ada, saat structuring masih stub.

> **Baca ini sebelum membaca `make smoke` yang hijau.** Yang teruji ujung ke ujung adalah **pipanya**
> — transaksi, idempotensi, lease, outbox, dead letter, tabel outcome, bentuk kontrak — dan model OCR
> sungguhan. Yang **belum** teruji adalah **isinya**: structuring masih stub yang mengarang field dan
> tidak membaca teks OCR, jadi tidak ada satu pun field di `data` yang pernah diextraction dari sebuah
> gambar, dan dokumen yang bukan Kartu Keluarga pun dijawab `200`. Batasnya diukur dan ditulis di
> [`docs/decisions/2026-09-27-batas-uji-end-to-end.md`](docs/decisions/2026-09-27-batas-uji-end-to-end.md).

> **Dan batas yang sama berlaku untuk `confidence`.** Trust model di scoring dilatih atas 1686 sel
> berlabel tangan dan angkanya terukur (AUC 0.855; bin teratas 200/200 benar pada data held-out), tapi
> ia membaca fitur yang **structuring** hasilkan — dan stub itu memancarkan fitur sintetis. Vektornya
> lengkap dan setiap sel berbeda, cukup untuk membuktikan ia sampai utuh dan penyelarasan
> posisionalnya benar; confidence yang dihitung darinya belum mengatakan apa pun tentang sebuah
> dokumen. Yang membuatnya bermakna adalah port K2Regex-v2 di structuring, bukan pekerjaan lanjutan di
> scoring.

**Diselaraskan dengan nilam (kontrak draf 12, 29 September 2026)**, lihat
[`docs/decisions/2026-09-29-selaras-nilam.md`](docs/decisions/2026-09-29-selaras-nilam.md):

- `pipeline_name_sequence` menggantikan `skip_guardrails`. Nama tahap OCR-nya `extraction`.
- Orchestrator mencatat setiap putusan guardrails di `nilam_ocr_kk.nilam_guardrails_results` (butuh `DATABASE_URL`, migrasi `0002`).
- PDF diterima bawaan; hanya halaman 1 yang dinilai dan dibaca.
- `EXTRACTION_BACKEND=paddle` memanggil server PaddleOCR di `POST /ocr`, dengan `poly` diteruskan utuh.
- Ada endpoint OCR sinkron `POST /v1/extraction/extract` yang menjawab `OcrPayload` §7.1.

**Draf 13 (update nilam 29 September):**

- Tiap field `data` kini `{"value", "confidence": 0 | 1}` seperti nilam; `bin` dan `auto` dihapus.
- Threshold per request: `column_confidence_threshold` (per field kontrak), `guardrails_confidence_threshold`.
- `errors` selalu kode stabil (mis. `EMPTY_FILE`, `DOWNSTREAM_UNAVAILABLE`), dan setiap error membawa `pipeline_last_stage`.

**Draf 15 (update nilam 29 September – 5 Oktober)**, rinciannya di
[`docs/api-contract.md`](docs/api-contract.md) dan [`integration.md`](integration.md):

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

`services/guardrails/.env.example` menunjuk backend `kk_quality` (inti K2Quality), dan service itu
**menolak start** tanpa keenam artefaknya. Ambil dulu:

```bash
GUARDRAILS_MODEL_URI=../K2Quality/models make weights
```

URI-nya boleh direktori lokal, `gs://`, `s3://`, atau `https://`; untuk entri banyak-berkas ia
diperlakukan sebagai awalan. Kelima artefak diverifikasi terhadap `model_hashes.json` yang ikut
diambil, dan artefak yang tidak lengkap atau tidak cocok dibatalkan alih-alih dipakai separuh.

Bobotnya gitignored. Untuk pengembangan tanpa bobot sama sekali, setel `GUARDRAILS_BACKEND=mock`
(hanya jalan di `ENVIRONMENT=local`).

**Structuring + scoring berpasangan.** `STRUCTURING_BACKEND=kk_model` (model key per kotak m04) dan
`SCORING_BACKEND=kk_field` (trust model s11) dilatih bersama, dan confidence s11 hanya sah di atas
structuring m04. Artefaknya ada di path yang sudah dibaca masing-masing service dan ikut repo:

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

Guardrails sama: keenam artefak K2Quality diunduh ulang setiap start (hanya untuk `GUARDRAILS_BACKEND=kk_quality`)
dari awalan `gs://gc-bribrain-dev-gcs-ocr-nilam-01/nilam-ocr-kk/guardrails/` (`GUARDRAILS_MODEL_URI`) dan
diverifikasi terhadap `model_hashes.json`.
 `make kk-model-parity` memastikan keluaran service sama persis dengan rantai training
pada korpus `../raw_ocr_v6`. Rinciannya di [`services/scoring/weights/README.md`](services/scoring/weights/README.md).

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

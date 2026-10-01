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

Batch pertama sedang berjalan: orchestrator, guardrails, dan extraction. **Scoring sudah memakai trust
model yang sungguhan** (`SCORING_BACKEND=calibrated`, artefak di `services/scoring/weights/`);
structuring masih stub.

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
- Orchestrator mencatat setiap putusan guardrails di `guardrails_results` (butuh `DATABASE_URL`, migrasi `0002`).
- PDF diterima bawaan; hanya halaman 1 yang dinilai dan dibaca.
- `EXTRACTION_BACKEND=paddle` memanggil server PaddleOCR di `POST /ocr`, dengan `poly` diteruskan utuh.
- Ada endpoint OCR sinkron `POST /v1/extraction/extract` yang menjawab `OcrPayload` §7.1.

**Draf 13 (update nilam 29 September):**

- Tiap field `data` kini `{"value", "confidence": 0 | 1}` seperti nilam; `bin` dan `auto` dihapus.
- Threshold per request: `column_confidence_threshold` (per field kontrak), `guardrails_confidence_threshold`.
- `errors` selalu kode stabil (mis. `EMPTY_FILE`, `DOWNSTREAM_UNAVAILABLE`), dan setiap error membawa `pipeline_last_stage`.

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

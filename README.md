# nlm-k2

Pipeline OCR Kartu Keluarga. Lima service FastAPI yang berbagi satu pustaka, `libs/ocr_common`.

Strukturnya disalin dari `nilam-ocr-npwp` (pipeline OCR NPWP) dan sedang disesuaikan untuk KK.
Kontraknya ada di [`docs/api-contract.md`](docs/api-contract.md).

| Service | Port | Peran |
|---|---|---|
| orchestrator | 8040 | satu-satunya pintu masuk; dipanggil Orkestrasi pusat |
| guardrails | 8041 | menilai kualitas gambar; selalu 200, vonis di `data.passed` |
| ekstraksi | 8042 | OCR; tahap pertama pipeline asinkron |
| structuring | 8043 | membaca field dari baris OCR; satu-satunya tahap yang boleh menolak isi |
| scoring | 8044 | trust model; tahap terakhir, merakit hasil kontrak |

Ekstraksi → structuring → scoring berjalan asinkron lewat outbox transaksional. Orchestrator
menunggu sebentar lalu menjawab 200, atau 202 kalau pipeline belum selesai.

## Status

Batch pertama sedang berjalan: orchestrator, guardrails, dan ekstraksi. Structuring dan scoring
hadir sebagai stub supaya siklus 202→200 bisa diuji ujung ke ujung.

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

### Smoke test ujung ke ujung

`make smoke` menjalankan delapan jalur kontrak terhadap stack compose lokal. Ia **menolak jalan**
di luar `ENVIRONMENT=local` atau terhadap alamat non-lokal (R25a): ia membuat baris dan
menghapusnya lagi, termasuk di tabel outcome yang di produksi dimiliki Orkestrasi pusat.

Yang perlu disiapkan sekali, di ketiga `services/{ekstraksi,structuring,scoring}/.env`:

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
' >> services/ekstraksi/.env
docker compose -f docker-compose.yml -f docker-compose.db.yml up -d --force-recreate ekstraksi
docker stop nlm-k2-structuring-1          # penerima handoff dimatikan
# kirim satu job ke POST /v1/ekstraksi/jobs, tunggu ~15 dtk
```

Baris outcome-nya jadi `failed` / `STRUCTURING` / `STRUCTURING_FAILED`, dan `GET /v1/ekstraksi/outbox`
melaporkan `dead_letters: 1`. Setelah structuring dinyalakan lagi,
`POST /v1/ekstraksi/outbox/release` mengirimkannya kembali: structuring dan scoring jadi `DONE` dan
baris outcome-nya maju ke `completed` / `SCORING`. Jangan lupa mengembalikan kedua kunci env itu.

## Database

nlm-k2 memakai database PostgreSQL sendiri di **instans tersendiri**, tidak berbagi instans dengan
nilam. Tabel tahap milik repo ini; tabel `orchestration_*` milik Orkestrasi pusat dan hidup di
database yang sama, sehingga penulisan outcome tetap satu transaksi dengan penyimpanan hasil.
Lihat [`db/README.md`](db/README.md).

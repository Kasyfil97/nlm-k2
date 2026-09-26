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

## Database

nlm-k2 memakai database PostgreSQL sendiri di **instans tersendiri**, tidak berbagi instans dengan
nilam. Tabel tahap milik repo ini; tabel `orchestration_*` milik Orkestrasi pusat dan hidup di
database yang sama, sehingga penulisan outcome tetap satu transaksi dengan penyimpanan hasil.
Lihat [`db/README.md`](db/README.md).

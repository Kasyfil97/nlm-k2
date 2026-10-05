# Database

Satu database PostgreSQL dipakai bersama oleh repo ini **dan** oleh Orkestrasi pusat
(`bribrain_ocr_kk` di Cloud SQL), di **instans tersendiri** — bukan instans yang dipakai nilam.
Dua alasan: PII Kartu Keluarga tidak masuk ke instans yang daftar aksesnya disusun untuk dokumen
pajak,
dan matriks hak di bawah baru bisa ditegakkan di instans yang tidak punya peran lintas-database
milik tim lain.

Karena databasenya dibagi, penting jelas dua hal: tabel mana milik siapa, dan siapa boleh
membaca apa.

## Schema dan nama tabel

Semua tabel milik repo ini tinggal di schema **`nilam_ocr_kk`**, bukan di `public`, dan setiap namanya
berawalan **`nilam_`** (`nilam_ocr_kk.nilam_ocr_jobs`, `nilam_ocr_kk.nilam_testing_pipeline_outbox`, ...):
penamaan klien, sama seperti schema nilam. Kode membaca keduanya dari
`ocr_common.pipeline.database` (`PIPELINE_SCHEMA`, `TABLE_PREFIX`); tes SQLite memetakan schema itu ke tanpa
schema.

Migrasi `0003_nilam_naming` memindahkan ke-16 tabel dari `public` dan mengganti namanya, beserta indeks,
primary key, foreign key, dan sequence `id`-nya, dalam satu transaksi tanpa menyalin baris. `env.py`
memindahkan tabel versi `public.ocr_kk_alembic_version` menjadi `nilam_ocr_kk.nilam_ocr_kk_alembic_version`
sebelum revisi mana pun jalan. CI memeriksanya dengan `db/check_schema_move.py`.

**Tidak kompatibel ke belakang:** pod dengan image lama gagal query-nya di antara migrasi dan
penggantiannya, dan query di luar repo ini (dashboard, tim Orkestrasi) harus memakai nama baru. Peran
service juga butuh `USAGE` pada schema `nilam_ocr_kk`; hibah pada tabelnya ikut pindah.

**Satu berkas DDL:** [`schema.sql`](schema.sql) membuat schema dan ke-16 tabel (plus indeks, foreign key, dan
tabel versi Alembic yang dicap di `0003_nilam_naming`) untuk database **kosong**, tanpa Alembic:
`psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql`. Idempoten. Skema hasilnya identik dengan
`alembic upgrade head` (dibandingkan lewat `pg_dump --schema-only`). Untuk database yang sudah berisi tabel di
`public`, tetap pakai migrasi: berkas ini tidak memindahkan baris. Buat ulang berkas ini setiap ada migrasi baru.

## Peta tabel

| Tabel | Pemilik schema | Ditulis | Dibaca | Isi |
|---|---|---|---|---|
| `nilam_ocr_jobs`, `nilam_ocr_results` | **repo ini** | extraction | extraction (`GET /v1/extraction/jobs/{request_id}`), orchestrator lewat API itu | status dan hasil tahap OCR |
| `nilam_structuring_jobs`, `nilam_structuring_results` | **repo ini** | structuring | structuring lewat API-nya (orchestrator) | status dan field hasil structuring |
| `nilam_scoring_jobs`, `nilam_scoring_results` | **repo ini** | scoring | scoring lewat API-nya (orchestrator) | status dan skor trust model |
| `nilam_pipeline_outbox` | **repo ini** | ketiga tahap (dalam transaksi job), relay | relay tiap service, `GET /v1/<tahap>/outbox` | callback dan handoff yang belum terkirim (`PIPELINE_OUTBOX`). Baris dihapus setelah terkirim; yang gagal permanen (4xx, atau 5xx lebih lama dari `PIPELINE_OUTBOX_MAX_AGE_SECONDS`) tetap ada sebagai dead letter dengan `failed_at` + `last_error`, tidak pernah diambil lagi oleh relay, dan dilepas manual dengan `failed_at = NULL, next_attempt_at = now()`. `ds` dipakai untuk membersihkan dead letter lama |
| `nilam_testing_ocr_jobs`/`_results`, `nilam_testing_structuring_jobs`/`_results`, `nilam_testing_scoring_jobs`/`_results`, `nilam_testing_pipeline_outbox` | **repo ini** | ketiga tahap lewat endpoint `-test` (`TESTING_ENDPOINTS`) | tahap itu sendiri, orchestrator lewat `GET /v1/<tahap>/jobs-test/{request_id}` | salinan persis tabel tahap dan outbox untuk load test tim ML (baseline `0001`). Tidak pernah dibaca Orkestrasi; boleh di-`TRUNCATE` kapan saja setelah tes. Lihat README, "Endpoint Testing" |
| `nilam_guardrails_results`, `nilam_testing_guardrails_results` | **repo ini** | orchestrator (best-effort, `DATABASE_URL`; `-test` menulis `nilam_testing_`) | orchestrator (`GET /v1/extract-ocr/{request_id}` untuk request yang tidak pernah sampai tahap) | satu baris per putusan guardrails, termasuk yang ditolak: `passed`, `verdict`, `confidence`, `threshold`, `n_pages`, `pipeline_name_sequence`, dan `report` utuh (`probability_bad`). Append-only; migrasi `0002`, porting dari nilam |
| `nilam_ocr_kk_alembic_version` | **repo ini** | Alembic | Alembic | versi migrasi repo ini; namanya sengaja tidak `alembic_version` supaya tidak bentrok dengan migrasi tim lain di instans yang sama |
| `ocr.orchestration_api_events` | **orkestrasi** | orkestrasi; ketiga tahap menambah baris keadaan akhir kalau `ORCHESTRATION_API_EVENTS_TABLE` diisi | orkestrasi | log API orkestrasi, append-only. Lihat bagian di bawah tabel ini |
| `orchestration_extract_ocr` | **Orkestrasi pusat** | ketiga tahap (dalam transaksi job) kalau `ORCHESTRATION_OUTCOME_TABLE` diisi | Orkestrasi pusat | **kanal hasil yang sesungguhnya.** Satu baris per `request_id`; kontraknya kolom `downstream_status`. Barisnya **monoton di `completed`**: penulisan terlambat dari relay yang menyerah atau eksekusi kembar ditolak, dicatat WARNING, dan dihitung `pipeline_outcome_writes_suppressed_total`. Migrasi di sini tidak pernah membuat atau mengubahnya |
| `orchestration_*` lainnya, `auth_*`, `datahub_lookup_log` (schema `ocr`) | **Orkestrasi pusat** | orkestrasi | orkestrasi | di luar repo ini. Migrasi di sini tidak pernah membuat atau mengubahnya |

Guardrails tidak punya tabel. Orchestrator hanya punya `nilam_guardrails_results` (seperti nilam); status tahap tetap dibacanya lewat API, bukan lewat database.

## Hak akses (R28)

Hibah dibatasi **di kedua arah**, bukan hanya untuk service nlm-k2. Kalau hanya satu sisi yang
dibatasi, peran Orkestrasi pusat di database yang sama tetap bisa membaca setiap kartu.

| Peran | `nilam_ocr_*` | `nilam_structuring_*` | `nilam_scoring_*` | `nilam_pipeline_outbox` | tabel outcome | `nilam_guardrails_results` |
|---|---|---|---|---|---|---|
| extraction | SELECT, INSERT, UPDATE | — | — | SELECT, INSERT, UPDATE, DELETE | INSERT, UPDATE | — |
| structuring | SELECT (baca hasil hulu) | SELECT, INSERT, UPDATE | — | SELECT, INSERT, UPDATE, DELETE | INSERT, UPDATE | — |
| scoring | SELECT | SELECT | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE, DELETE | INSERT, UPDATE | — |
| orchestrator | — | — | — | — | — | SELECT, INSERT |
| **Orkestrasi pusat** | **—** | **—** | **—** | **—** | miliknya sendiri | **—** |
| migrasi (Alembic) | DDL | DDL | DDL | DDL | — | DDL |

Tidak ada peran tahap yang mendapat DDL atas tabelnya sendiri: itu milik peran migrasi.

Orkestrasi pusat tidak mendapat `SELECT` pada tabel tahap. Satu-satunya yang mereka butuhkan dari
sini adalah tabel outcome, sementara `nilam_ocr_results` memuat teks OCR **seluruh** kartu dan
`nilam_structuring_results` memuat 26 field internal (alamat, tanggal lahir, agama, nama orang tua).

Tabel `nilam_testing_*` adalah salinan persis dan mewarisi baris yang sama persis.

**Terbuka, dan bukan keputusan sepihak nlm-k2:** apakah Orkestrasi pusat terhubung dengan peran yang
bisa kita batasi, atau dengan peran pemilik yang tidak bisa. Kalau yang kedua, tabel ini jadi
kesepakatan lintas tim, bukan sesuatu yang bisa ditegakkan baseline.

### Inventaris data sensitif

| Tempat | Isi |
|---|---|
| `nilam_ocr_results.result` | teks OCR seluruh kartu — setiap NIK, nama, alamat yang terbaca |
| `nilam_structuring_results.result` | 11 field dokumen + 15 per anggota |
| `nilam_scoring_results.result` | skor saja, tetapi `payload` menyimpan fitur yang diskor |
| tabel outcome `result_data` | sembilan field kontrak, termasuk NIK tiap anggota |
| `nilam_guardrails_results.report` | skor kualitas gambar saja, tanpa isi kartu; `request_id` menautkannya ke tahap |
| **`nilam_ocr_jobs.input`** | presigned URL — **setara kredensial pembawa** ke gambar KK itu sendiri |

`nilam_ocr_jobs.input` dikosongkan di transaksi `complete()`/`fail()` milik job, bukan lewat sapuan
retensi `ds`: §3.1 menjanjikan job terlantar bisa dijalankan ulang dari URL itu, dan janji itu hanya
berlaku selagi job masih `PROCESSING`.

### Retensi

Setiap tabel punya kolom `ds` (`YYYYMMDD`) justru untuk ini. Jendela retensinya belum ditetapkan dan
perlu disepakati dengan pemilik data; yang sudah pasti, pembersihannya digantung pada `ds` dan
dijalankan di luar service (job terjadwal), bukan oleh pipeline.

Ketiga tahap juga bisa menulis status request ke `orchestration_extract_ocr` di transaksi
yang sama dengan penyimpanan hasilnya, kalau `ORCHESTRATION_OUTCOME_TABLE` diisi (default
mati). Yang ditulis: `processing` + tahapnya saat job diklaim, `completed` + `result_data`
oleh scoring, dan `failed` + `error_code` saat gagal. **Kontraknya adalah kolom `downstream_status`**:
Orkestrasi hanya membaca kolom itu (`processing` | `completed` | `failed`) untuk menjawab polling
client; kolom lain (`downstream_stage`, `status_code`, `error_code`, `error_message`, `result_data`)
adalah data pendamping. Request yang ditolak guardrails (400) tidak pernah menulis kolom ini,
karena tidak ada tahap yang berjalan; Orkestrasi menjawab client dari respons sinkron itu. Tabel itu **milik orkestrasi**, jadi
kolomnya mereka yang menambahkan; DDL yang dibutuhkan (termasuk `request_id` unik) ada di
[external/orchestration_extract_ocr.sql](external/orchestration_extract_ocr.sql) dan bisa
dipasang ke PostgreSQL lokal dengan `make db-external`.

Orkestrasi di dev membaca hasil dari log API-nya, `ocr.orchestration_api_events`. Kalau
`ORCHESTRATION_API_EVENTS_TABLE=ocr.orchestration_api_events` diisi, tiap tahap **menambah** satu
baris di transaksi yang sama dengan tabel job-nya sendiri (double write), hanya untuk keadaan akhir:

| Keadaan | `endpoint` | `status_code` | `downstream_status` | `downstream_stage` | `error_code` |
|---|---|---|---|---|---|
| selesai (scoring) | `GET_OCR_RESULT` | 200 | `COMPLETED` | `SCORING` | kosong |
| gagal di satu tahap | `GET_OCR_RESULT` | 422 | `FAILED` | `EXTRACTION`, `STRUCTURING`, atau `SCORING` | `OCR_FAILED`, `STRUCTURING_FAILED`, `SCORING_FAILED` |
| ditolak aturan structuring | `GET_OCR_RESULT` | 400 | `FAILED` | `STRUCTURING` | `DOWNSTREAM_VALIDATION_ERROR` |

`result_data` mengikuti bentuk baris polling orkestrasi sendiri: `{result, status, document_type,
error_code, error_message, created_at, updated_at}`. `result` berisi data kontrak `extract-ocr`
(sembilan field: `no_kk`, `nama_kepala_keluarga`, dan `anggota_keluarga[]`) plus `document_type`
dan `guardrails`, dan kosong kalau gagal atau ditolak. Untuk penolakan, `error_message` adalah
alasan dari gerbang validitas KK (bahasa Indonesia). Tabel ini tidak punya kunci unik per `request_id`, jadi request yang dijalankan ulang
mendapat baris baru; **baris terbaru per `request_id` adalah keadaannya**. Kalau tabel ini gagal
ditulis, penulisan job ikut dibatalkan. DDL tiruannya ada di
[external/orchestration_api_events.sql](external/orchestration_api_events.sql).

## Sumber kebenaran

Kolom didefinisikan **sekali** di [`libs/ocr_common/ocr_common/pipeline/tables.py`](../libs/ocr_common/ocr_common/pipeline/tables.py).
Migrasi Alembic dan `create_all` di test memakai definisi yang sama, dan `make db-check`
gagal kalau keduanya menyimpang.

## Perintah

```bash
export DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/bribrain_ocr_kk

make db-upgrade                  # jalankan migrasi sampai revisi terakhir
make db-check                    # gagal kalau definisi tabel di kode beda dengan database
make db-revision m="tambah kolom X"   # buat revisi baru dari selisihnya, lalu PERIKSA hasilnya
```

Database yang tabelnya sudah ada (mis. dev yang dulu dipasang manual) cukup dijalankan
`make db-upgrade`: revisi baseline memakai `CREATE TABLE IF NOT EXISTS`, jadi tabel dan
datanya dibiarkan, dan database itu tercatat berada di revisi baseline.

## Deploy

```bash
DB_HOST=<alamat postgres> deploy/helm/migrate-db.sh          # upgrade head
DB_HOST=<alamat postgres> deploy/helm/migrate-db.sh current  # lihat revisi sekarang
```

Script mengambil `DATABASE_URL` dari Secret release, menggantikan host-nya dengan
`DB_HOST`, lalu menjalankan Alembic di dalam image `db/Dockerfile`. **Jalankan sebelum**
men-deploy image yang membutuhkan perubahan tabelnya.

Untuk PostgreSQL lokal, `make up-db` menjalankan migrasi lebih dulu lewat service
`migrate` di `docker-compose.db.yml`; service lain baru start setelah migrasi selesai.

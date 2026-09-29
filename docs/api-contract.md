# nlm-k2 — Kontrak API orchestrator

Spesifikasi kontrak untuk service **orchestrator** `nlm-k2` (OCR Kartu Keluarga), mengikuti pola
[nilam-ocr-npwp](../nilam-ocr-npwp): satu pintu masuk sinkron di depan rantai tahap asinkron.

Dokumen ini adalah **kontrak, bukan laporan implementasi**. Belum ada kode `nlm-k2`; yang ada adalah
`K2Orchestrator`, `K2Quality`, `K2Extractor`, `K2Regex-v2` dengan kontrak lama yang berbeda.
Bagian [12](#12-selisih-dengan-k2orchestrator-sekarang) merinci apa yang berubah.

Mekanisme pipeline — penolakan, tabel outcome, outbox, idempotensi — **mengikuti nilam persis**, supaya
mesin tahap (`ocr_common/pipeline`) bisa dipakai tanpa bercabang.

Status: draf 12, 29 September 2026.

---

## Daftar isi

1. [Topologi dan port](#1-topologi-dan-port)
   · [1.1 Tidak ada penilaian legibilitas per field](#11-tidak-ada-penilaian-legibilitas-per-field)
2. [Konvensi bersama](#2-konvensi-bersama)
   · [2.6 Tabel outcome](#26-tabel-outcome-dan-cara-hasil-sampai-ke-pemanggil)
3. [`POST /v1/extract-ocr` — pintu masuk](#3-post-v1extract-ocr--pintu-masuk)
4. [`GET /v1/extract-ocr/{request_id}` — keadaan request](#4-get-v1extract-ocrrequest_id--keadaan-request)
5. [orchestrator → guardrails](#5-orchestrator--guardrails)
6. [orchestrator → ekstraksi](#6-orchestrator--ekstraksi)
7. [ekstraksi → structuring](#7-ekstraksi--structuring)
8. [structuring → scoring](#8-structuring--scoring)
9. [`GET /v1/<tahap>/jobs/{request_id}`](#9-get-v1tahapjobsrequest_id--dibaca-orchestrator)
10. [Skema data bersama](#10-skema-data-bersama)
11. [Endpoint operasional dan probe](#11-endpoint-operasional-dan-probe)
12. [Selisih dengan K2Orchestrator sekarang](#12-selisih-dengan-k2orchestrator-sekarang)
13. [Environment variables](#13-environment-variables)

---

## 1. Topologi dan port

| Service | Port | Peran | Berasal dari |
|---|---|---|---|
| **orchestrator** | 8040 | Pintu masuk tunggal. Cek file → minta guardrails menilai → serahkan ke ekstraksi → tunggu sampai `PIPELINE_WAIT_SECONDS`. Stateless. | `K2Orchestrator` (dirombak) |
| **guardrails** | 8041 | Sinkron, internal. Vonis `accepted` / `reject` untuk keseluruhan dokumen sebelum pipeline jalan. | `K2Quality` (XGBoost + NR-IQA) |
| **ekstraksi** | 8042 | Tahap async 1. OCR PaddleOCR — orientasi dan pelurusan ditangani di dalamnya. | `K2Extractor` |
| **structuring** | 8043 | Tahap async 2. Kotak teks → 11 field dokumen + `anggota_keluarga`. **Satu-satunya tahap yang boleh menolak dokumen.** | `K2Regex-v2` (layout parser + CRF) |
| **scoring** | 8044 | Tahap async 3, terakhir. Confidence per field terkalibrasi; menulis hasil akhir. | baru |

Port berurutan mengikuti urutan pipeline.

**Pemanggilnya adalah Orkestrasi pusat** — service yang sama yang memanggil nilam, dan yang **berbagi
PostgreSQL** dengan ketiga tahap di sini. BRIspot ada di belakangnya dan tidak pernah berbicara langsung
ke nlm-k2. Itu yang membuat tabel outcome ([2.6](#26-tabel-outcome-dan-cara-hasil-sampai-ke-pemanggil))
menjadi kanal yang benar-benar terbaca, bukan sekadar catatan internal.

Alur panggilan:

```
Orkestrasi pusat ──► orchestrator :8040   POST /v1/extract-ocr   SATU-SATUNYA PANGGILAN MASUK
              orchestrator ──► guardrails :8041  POST /v1/guardrails/check     SINKRON
                 reject ◄── 400 DOWNSTREAM_VALIDATION_ERROR, tidak ada tahap yang jalan
                 accept ──► ekstraksi :8042      POST /v1/ekstraksi/jobs       202 segera
                            ekstraksi   ──► structuring :8043  POST /v1/structuring/jobs   202
                            structuring ──► scoring :8044      POST /v1/scoring/jobs       202
              orchestrator menunggu maks. PIPELINE_WAIT_SECONDS sambil polling
                 GET /v1/{ekstraksi,structuring,scoring}/jobs/{request_id} tiap 0,5 dtk
              ◄── 200 completed | 202 processing | 400 ditolak | 422 satu tahap gagal
```

Ketiga tahap async memakai mesin yang sama (padanan `ocr_common/pipeline/stage.py` di nilam): klaim job
dalam satu transaksi, jawab 202, kerja di background, simpan hasil + antrekan handoff di satu transaksi,
relay outbox mengirim ke tahap berikutnya.

**Hanya ada dua titik penolakan**, sama seperti nilam:

| Titik | Kapan | Bagaimana sampai ke orchestrator |
|---|---|---|
| model guardrails | sebelum pipeline jalan | respons sinkron `POST /v1/guardrails/check` |
| aturan structuring | tahap 2 | `reject_reason` di dalam payload hasil structuring |

Ekstraksi dan scoring **tidak pernah menolak**. Kegagalan di sana selalu job `FAILED` → `422`, bukan `400`.
Alasannya dijelaskan di [7.4](#74-gerbang-validitas-kk).

**Yang sengaja tidak ada.**

- **Tidak ada service orientasi dan rectifier** — PaddleOCR di dalam tahap ekstraksi menangani keduanya.
- **PDF hanya dibaca halaman pertamanya** (draf 12, seperti nilam menerima PDF): lebih dari
  `MAX_DOCUMENT_PAGES` halaman ditolak `400` di orchestrator; guardrails menilai dan ekstraksi membaca
  halaman 1 saja, karena parser tata letak membaca satu bingkai halaman.
- **Tidak ada pengecekan kualitas crop.** `K2QualityDL` tidak dipakai di nlm-k2. Satu-satunya penilaian
  kualitas gambar adalah model guardrails, yang menilai dokumen **secara keseluruhan** sebelum OCR.
  Konsekuensinya di [1.1](#11-tidak-ada-penilaian-legibilitas-per-field).

### 1.1 Tidak ada penilaian legibilitas per field

`K2Orchestrator` sekarang punya gerbang legibilitas per crop (`K2QualityDL`, MobileNetV2) yang menolak
dokumen ketika potongan teksnya tidak terbaca. Gerbang itu **tidak ada** di nlm-k2. Yang perlu diketahui:

- Satu-satunya sinyal legibilitas per field adalah **`ocr_conf`** — skor rekognisi PaddleOCR pada kotak
  yang menghasilkan nilai itu ([7.3](#73-hasil-tahap-structuring)). Itu mengukur keyakinan mesin
  rekognisi, bukan keterbacaan potongan gambarnya, dan keduanya bisa berbeda: OCR bisa yakin pada bacaan
  yang salah.
- Di tingkat dokumen tersisa `guardrail_probability` dari model guardrails, plus `avg_doc_score` /
  `min_doc_score` yang diturunkan dari skor rekognisi.
- Dokumen buram yang lolos guardrails tetap diproses sampai selesai dan dijawab `200`; yang menandainya
  hanyalah `confidence` rendah dan `auto: false` per field dari tahap scoring. Penyaringan akhir bergantung
  pada trust model dan ambang di sisi pemanggil, bukan pada gerbang keras.
- Terukur, `ocr_conf` memang menanggung peran itu dengan cukup baik: ia fitur **terpenting** di trust model
  (permutation importance 0.0743, ~27× `crf_conf`). Yang belum terpecahkan adalah kasus "OCR yakin tapi
  salah" — semua fitur teks yang ada bersifat self-consistency atau leksikon, tidak satu pun bukti
  independen bahwa bacaannya benar.

Kalau nanti gerbang itu dibutuhkan, tempat masuknya adalah tahap async baru **setelah structuring** —
parser sudah tahu kotak mana yang menghasilkan tiap field, jadi yang dinilai hanya potongan yang relevan.
Itu menuntut hasil structuring membawa indeks kotak per field dan `file_url` mengalir sampai tahap itu.

---

## 2. Konvensi bersama

### 2.1 Envelope

Setiap respons JSON, sukses maupun gagal, memakai bentuk yang sama:

```json
{
  "status_code": 200,
  "status_desc": "OK",
  "message": "OCR extraction completed successfully",
  "data": null,
  "errors": null,
  "request_id": "REQ_..."
}
```

- `status_code` selalu sama dengan status HTTP.
- Sukses: `errors` null. Gagal: `data` null.
- `message` untuk manusia; **jangan** dijadikan cabang logika — pakai `errors`.
- Endpoint `extract-ocr` menambah `document_type`, `job_status`, `guardrails`, dan `params` sejajar `data`.
  Artinya di [3.2](#32-matriks-hasil-dan-arti-job_status--guardrails).

### 2.2 Autentikasi

Semua endpoint kecuali `/health`, `/ready`, `/metrics` mewajibkan header `X-API-Key`. Salah atau tidak ada
→ `401`. Tiap service menerima `API_KEY` plus daftar `API_KEYS` supaya rotasi kunci bisa dua nilai hidup
bersamaan; perbandingan konstan-waktu. Service selain orchestrator hanya dijangkau dari dalam namespace.

### 2.3 `request_id` dan korelasi log

- Dibuat pemanggil dan dikirim di form `POST /v1/extract-ocr`. Orchestrator **mengadopsi** id itu:
  envelope error, header `X-Request-ID` respons, baris log, dan semua panggilan keluar memakainya.
- Endpoint tanpa `request_id` sendiri memakai header `X-Request-ID`; tanpa itu dibuatkan `REQ_<uuid>`.
- Id diikat ke contextvar oleh middleware untuk tiap request, oleh pipeline untuk tiap job background dan
  tiap pengiriman outbox, lalu diteruskan sebagai header `X-Request-ID` ke service berikutnya. Satu filter
  `request_id` di Cloud Logging menampilkan seluruh perjalanan lintas kelima pod.
- Format bebas, maksimal 100 karakter. Prefiks yang dipakai internal: `REQ_` (dibuat sendiri),
  `TEST_<run_id>_` (endpoint load test).

### 2.4 Kode error

| HTTP | `errors` | Arti | Aman diulang? |
|---|---|---|---|
| 400 | `UNSUPPORTED_DOCUMENT_TYPE` | `document_type` bukan `kk` | tidak |
| 400 | `DOWNSTREAM_VALIDATION_ERROR` | dokumen ditolak model guardrails atau aturan structuring | tidak |
| 400 | = `message` | file kosong / bukan JPEG, PNG, atau PDF / PDF lebih dari `MAX_DOCUMENT_PAGES` halaman, `file`+`file_url` dua-duanya atau tidak ada, host `file_url` tidak diizinkan | tidak |
| 401 | = `message` | `X-API-Key` salah atau tidak ada | tidak |
| 404 | = `message` | `request_id` tidak dikenal (hanya di `GET`) | tidak |
| 413 | = `message` | berkas melebihi `MAX_UPLOAD_BYTES` | tidak |
| 422 | `INVALID_PARAMS` | `params` bukan JSON object / string | tidak |
| 422 | `INVALID_PIPELINE_SEQUENCE` | `pipeline_name_sequence` bukan urutan yang sah (3.1) | tidak |
| 422 | `VALIDATION_ERROR` | field wajib tidak dikirim atau salah tipe | tidak |
| 422 | `OCR_FAILED` / `STRUCTURING_FAILED` / `SCORING_FAILED` | satu tahap gagal | ya, kirim ulang |
| 500 | = `message` | service ini atau modelnya gagal | ya |
| 503 | = `message` | dependensi tidak terjangkau | ya |
| 504 | = `message` | dependensi tidak menjawab dalam timeout | ya |

`status_desc` mengikuti reason phrase HTTP: `OK`, `Accepted`, `Bad Request`, `Unauthorized`, `Forbidden`,
`Not Found`, `Payload Too Large`, `Unprocessable Entity`, `Internal Server Error`, `Service Unavailable`,
`Gateway Timeout`.

### 2.5 Idempotensi

`request_id` yang sama dikirim ulang: guardrails dinilai lagi, tapi pipeline **tidak** mengerjakan apa pun
dua kali. Pengecualian yang boleh diklaim ulang: job berstatus `FAILED`, atau job `PROCESSING` yang sudah
melewati `PIPELINE_JOB_LEASE_SECONDS` (default 300 detik — artinya proses yang menjalankannya mati tanpa
mencatat apa pun). Klaim ulang atomik: dari dua kiriman bersamaan hanya satu yang menang.

### 2.6 Tabel outcome, dan cara hasil sampai ke pemanggil

Ada **dua jalur** hasil sampai ke pemanggil, sama seperti nilam. Keduanya boleh aktif bersamaan.

**Jalur 1 — polling.** Orkestrasi pusat memanggil `GET /v1/extract-ocr/{request_id}`. Orchestrator membaca
ketiga tahap lewat API dan merakit jawabannya. Tidak butuh akses database. Ini jalur yang selalu ada.

**Jalur 2 — tabel outcome.** Kalau `ORCHESTRATION_OUTCOME_TABLE` diisi, tiap tahap meng-*upsert* satu baris
per `request_id` **di transaksi yang sama** dengan penyimpanan hasilnya, sehingga tidak pernah ada keadaan
"hasil tersimpan tapi pemanggil tidak tahu" atau sebaliknya.

| Kolom | Isi |
|---|---|
| `request_id` | PK / unik |
| `document_type` | `kk` |
| `downstream_status` | **kolom kontrak**: `processing` → `completed` \| `failed` |
| `downstream_stage` | `OCR` \| `STRUCTURING` \| `SCORING` — tahap terkini atau tahap yang gagal |
| `status_code` | `202` saat diklaim, `200` selesai, `400` ditolak, `422` gagal |
| `error_code` | `DOWNSTREAM_VALIDATION_ERROR`, atau `<TAHAP>_FAILED` |
| `error_message` | alasan, bahasa Indonesia untuk penolakan |
| `result_data` | data kontrak `extract-ocr` (9 field), hanya diisi scoring |
| `ds` | `YYYYMMDD`, untuk partisi/pembersihan |

Kapan ditulis:

| Keadaan | `status_code` | `downstream_status` | `downstream_stage` |
|---|---|---|---|
| job diklaim sebuah tahap | 202 | `processing` | tahap itu |
| scoring selesai | 200 | `completed` | `SCORING` |
| sebuah tahap gagal | 422 | `failed` | tahap yang gagal |
| handoff mati permanen | 422 | `failed` | tahap **berikutnya** yang tidak pernah menerima |
| ditolak aturan structuring | 400 | `failed` | `STRUCTURING` |

Dokumen yang ditolak **model guardrails** tidak pernah menulis baris ini: tidak ada tahap yang berjalan,
dan jawabannya sudah final di respons sinkron `POST /v1/extract-ocr`.

**Pemilik dan pembacanya adalah Orkestrasi pusat**, sama seperti di nilam. Orkestrasi pusat adalah
pemanggil `POST /v1/extract-ocr` dan sebuah service yang **berbagi PostgreSQL yang sama** dengan ketiga
tahap, jadi ia benar-benar bisa membaca baris ini. BRIspot tidak berbicara langsung ke nlm-k2; ia ada di
belakang Orkestrasi pusat.

Konsekuensinya jalur 2 adalah kanal yang sungguh berfungsi, bukan cadangan teoretis:
`ORCHESTRATION_OUTCOME_TABLE` **diisi di produksi**, dan keadaan akhir sebuah request selalu punya tempat
mendarat walaupun polling terlewat.

### Peta kepemilikan tabel

Satu database dipakai bersama, jadi penting jelas tabel mana milik siapa.

| Tabel | Pemilik | Ditulis | Dibaca |
|---|---|---|---|
| `ocr_jobs` / `ocr_results` | **nlm-k2** | ekstraksi | ekstraksi lewat API-nya; orchestrator lewat API itu |
| `structuring_jobs` / `structuring_results` | **nlm-k2** | structuring | idem |
| `scoring_jobs` / `scoring_results` | **nlm-k2** | scoring | idem |
| `pipeline_outbox` | **nlm-k2** | ketiga tahap (dalam transaksi job) dan relay | relay tiap service, `GET /v1/<tahap>/outbox` |
| `guardrails_results` | **nlm-k2** | orchestrator, satu baris per putusan guardrails (best-effort) | orchestrator, untuk `GET` request yang tidak pernah sampai tahap (4) |
| tabel outcome (`ORCHESTRATION_OUTCOME_TABLE`) | **Orkestrasi pusat** | ketiga tahap, dalam transaksi job | Orkestrasi pusat |

Tabel outcome **milik mereka**, jadi kolomnya mereka yang menambahkan dan migrasi nlm-k2 tidak pernah
menyentuhnya. Yang dibutuhkan nlm-k2 dari tabel itu: `request_id` unik, plus kolom pada tabel di atas.
Sediakan DDL tiruannya di repo (padanan `db/external/` di nilam) supaya bisa diuji di PostgreSQL lokal.

Guardrails **tidak punya tabel**. Orchestrator hanya punya `guardrails_results` (draf 12, seperti nilam):
setiap putusan guardrails, termasuk yang ditolak, dengan `pipeline_name_sequence` request-nya. Status tahap
tetap dibacanya lewat API, bukan lewat database. Tanpa `DATABASE_URL` tidak ada yang dicatat, dan
request tetap dijawab.

---

## 3. `POST /v1/extract-ocr` — pintu masuk

`Content-Type: multipart/form-data`. Header `X-API-Key` wajib.

### 3.1 Request

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `request_id` | string | ya | dibuat pemanggil, maks. 100 karakter |
| `document_type` | string | tidak | default `kk`; selain itu 400 `UNSUPPORTED_DOCUMENT_TYPE` |
| `file` | file | salah satu | JPEG, PNG, atau PDF, maks. `MAX_UPLOAD_BYTES` (default 5 MB). PDF maks. `MAX_DOCUMENT_PAGES` (2) halaman, lebih → 400; **hanya halaman 1 yang dinilai dan dibaca** |
| `file_url` | string | salah satu | URL yang diunduh service ini (mis. presigned MinIO GET). Host harus terdaftar di `FILE_URL_ALLOWED_HOSTS`; redirect tidak diikuti. Di luar `local`: `https` wajib, dan host terdaftar yang me-resolve ke alamat privat atau loopback tetap ditolak |
| `params` | string | tidak | JSON object; tidak ditafsirkan, dikembalikan apa adanya di `params`. JSON tidak valid → 422 `INVALID_PARAMS` |
| `pipeline_name_sequence` | string[] | tidak | service yang dijalankan, berurutan, dari `guardrails`, `ekstraksi`, `structuring`, `scoring`. Boleh dikirim sebagai field berulang atau satu string JSON array. Guardrails boleh ditinggalkan di depan dan ujungnya boleh dipotong; yang di tengah **tidak** boleh dilewati dan urutannya tidak boleh berubah. Tidak dikirim = keempatnya. Tidak sah → 422 `INVALID_PIPELINE_SEQUENCE`, tidak ada yang berjalan. Menggantikan `skip_guardrails` (draf 12) |

`file` dan `file_url` **tepat satu**, tidak boleh dua-duanya dan tidak boleh kosong keduanya.

**`pipeline_name_sequence`, sama seperti nilam** kecuali nama tahap OCR-nya: `ekstraksi` (nama nilam,
`extraction`, ditolak sebagai service tak dikenal). Contoh sah: keempatnya (bawaan), `[guardrails, ekstraksi]`,
`[ekstraksi, structuring]`, `[guardrails]`. Tidak sah: `[ekstraksi, scoring]`, `[structuring, scoring]`.
Service terakhir mengakhiri request: hasilnya menjadi `data` **apa adanya** — laporan guardrails, hasil
OCR (7.1), hasil structuring (7.3), atau sembilan field setelah scoring — dan `pipeline_last_stage`
menyebut service itu. Tanpa `guardrails`, cek file tetap berjalan dan aturan structuring tetap menolak.
Urutannya ikut ke setiap tahap (form ke ekstraksi, badan handoff, `input` job), jadi job basi yang
dijalankan ulang berhenti di tempat yang sama.

```bash
curl -X POST http://nlm-k2.nlm-k2.svc.cluster.local:8040/v1/extract-ocr \
  -H "X-API-Key: $API_KEY" \
  -F "request_id=REQ_001" \
  -F "document_type=kk" \
  -F 'params={"branch":"0206","refno":"PK19039Y8U"}' \
  -F "file=@kk.jpg"
```

**Kenapa `file_url` disarankan.** Kalau request memakai `file_url`, orchestrator meneruskan **URL**-nya
(bukan byte-nya) ke `/v1/ekstraksi/jobs`. Ekstraksi menyimpan URL itu di kolom `input` job-nya, sehingga
job yang ditinggalkan pod mati bisa dijalankan ulang otomatis. Upload inline tidak disimpan, jadi job
OCR-nya `FAILED` dan minta kirim ulang. Presigned URL karena itu harus hidup lebih lama dari
`PIPELINE_JOB_LEASE_SECONDS`.

### 3.2 Matriks hasil, dan arti `job_status` / `guardrails`

Satu tabel untuk semua keadaan yang mungkin dijawab `POST /v1/extract-ocr`. Cabangkan logika pada
`errors`, bukan pada `message`.

| Keadaan | HTTP | `job_status` | `data` | `guardrails` | `errors` |
|---|---|---|---|---|---|
| Selesai | 200 | `completed` | 9 field | `0` | null |
| Masih berjalan | 202 | `processing` | null | null | null |
| Ditolak model guardrails | 400 | `failed` | null | `1` | `DOWNSTREAM_VALIDATION_ERROR` |
| Ditolak aturan structuring | 400 | `failed` | null | `1` | `DOWNSTREAM_VALIDATION_ERROR` |
| Satu tahap gagal | 422 | `failed` | null | `0` | `OCR_FAILED` / `STRUCTURING_FAILED` / `SCORING_FAILED` |
| Ditolak sebelum dinilai | 400 / 413 / 422 | null | null | null | kode masing-masing (2.4) |

**`job_status`** — `completed`, `processing`, atau `failed`; `null` kalau request ditolak sebelum ada yang
diproses (tipe file salah, `params` rusak, kunci API salah).

**`guardrails`** — bukan skor, melainkan penanda tiga nilai:

- `0` — dokumen lolos penyaringan dan pipeline berjalan.
- `1` — dokumen **ditolak**, oleh model guardrails atau oleh aturan structuring.
- `null` — belum diketahui (202), atau request ditolak sebelum penyaringan sempat jalan.

Dengan `pipeline_name_sequence` tanpa `guardrails` nilainya tetap `0` saat berhasil, walau model tidak
pernah dijalankan; dalam mode itu `1` hanya bisa berasal dari aturan structuring.

**`pipeline_last_stage`** — service tempat jawaban ini berasal, dengan nama seperti di
`pipeline_name_sequence`: service terakhir urutan saat `completed`; yang menolak (`guardrails`,
`structuring`) atau gagal; yang masih berjalan saat 202. `null` kalau request ditolak sebelum service
pipeline mana pun dipanggil.

### 3.3 Response `200` — selesai dalam waktu tunggu

```json
{
  "status_code": 200,
  "status_desc": "OK",
  "message": "OCR extraction completed successfully",
  "data": {
    "no_kk":                {"value": "3273012345678901", "confidence": 0.9913, "bin": 10, "auto": true},
    "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "confidence": 0.9041, "bin": 9, "auto": true},
    "anggota_keluarga": [
      {
        "nama_lengkap":                       {"value": "BUDI SANTOSO", "confidence": 0.9886, "bin": 10, "auto": true},
        "nik":                                {"value": "3273011203850001", "confidence": 0.9902, "bin": 10, "auto": true},
        "pendidikan":                         {"value": "S1", "confidence": 0.9951, "bin": 10, "auto": true},
        "jenis_pekerjaan":                    {"value": "KARYAWAN SWASTA", "confidence": 0.9833, "bin": 10, "auto": true},
        "status_hubungan_dalam_rumah_tangga": {"value": "KEPALA KELUARGA", "confidence": 0.9914, "bin": 10, "auto": true},
        "ayah":                               {"value": "SUTRISNO", "confidence": 0.9602, "bin": 9, "auto": true},
        "ibu":                                {"value": "SITI AMINAH", "confidence": 0.9418, "bin": 8, "auto": false}
      },
      {
        "nama_lengkap":                       {"value": "SITI NURHALIZA", "confidence": 0.9077, "bin": 6, "auto": false},
        "nik":                                {"value": "3273015506880002", "confidence": 0.9701, "bin": 9, "auto": true},
        "pendidikan":                         {"value": "SLTA/SEDERAJAT", "confidence": 0.9724, "bin": 9, "auto": false},
        "jenis_pekerjaan":                    {"value": "MENGURUS RUMAH TANGGA", "confidence": 0.6318, "bin": 2, "auto": false},
        "status_hubungan_dalam_rumah_tangga": {"value": "ISTRI", "confidence": 0.9130, "bin": 6, "auto": false},
        "ayah":                               {"value": "AHMAD DAHLAN", "confidence": 0.9355, "bin": 8, "auto": true},
        "ibu":                                {"value": "RATNA SARI", "confidence": 0.8802, "bin": 5, "auto": false}
      }
    ]
  },
  "errors": null,
  "request_id": "REQ_001",
  "document_type": "kk",
  "job_status": "completed",
  "guardrails": 0,
  "params": {"branch": "0206", "refno": "PK19039Y8U"}
}
```

Aturan isi `data`:

- **Hanya sembilan field yang keluar**: 2 field dokumen (`no_kk`, `nama_kepala_keluarga`) dan 7 field per
  anggota. Tidak ada field lain, walau tahap structuring mengekstraksi lebih banyak.
- Bentuk tiap field selalu `{"value": <string>, "confidence": <float>, "bin": <1..10>, "auto": <bool>}` —
  **tidak pernah** `null` sebagai pengganti objek. Field yang tidak ditemukan:
  `{"value": "", "confidence": 0.0, "bin": 1, "auto": false}`.
- **`confidence` adalah probabilitas terkalibrasi bahwa nilai itu PERSIS benar** (kapital & spasi
  dirapikan), langsung dari tahap scoring. Satu angka untuk dua hal sekaligus: penempatan kolom benar
  DAN teks OCR benar, karena keduanya harus benar agar string akhirnya sama.
  Sebelumnya kunci ini bendera `0 | 1`; itu membuang satu-satunya hal yang dibutuhkan untuk triase —
  field di 0.52 dan field di 0.99 bukan klaim yang sama. Tidak pernah `null`: pembaca tidak boleh harus
  menguji null sebelum membandingkan.
- **`bin`** menempatkan `confidence` di salah satu dari sepuluh bin model, 1 terendah. Tepinya **sengaja
  tidak selebar sama**: bin teratas dipotong tepat di titik presisi 100% terukur pada data held-out, dan
  tidak ada kisi 0.1 yang jatuh pas di situ. `bin: 10` adalah klaim terkuat yang pipeline ini buat.
- **`auto`** bernilai `true` kalau `confidence` melewati ambang yang dikalibrasi **khusus untuk field itu**
  — titik di atas mana setiap field sejenis benar pada data held-out. Ambangnya berbeda per field karena
  memang harus: terukur, `ayah` sudah aman di 0.935 sementara `pendidikan` butuh 0.993, dan satu angka
  global menjatuhkan cakupan yang bisa dipakai ke nol. Ambangnya ikut di dalam hasil scoring
  ([8.3](#83-hasil-tahap-scoring)), bukan di konfigurasi, supaya `auto` di sini identik dengan `auto` di
  baris outcome **secara konstruksi** — tidak ada env var yang harus dijaga sinkron. Field tanpa ambang
  sendiri jatuh ke `FIELD_CONFIDENCE_THRESHOLD`.
- Baca `auto` untuk memutuskan apakah manusia harus melihat; baca `confidence` untuk memutuskan apa yang
  dilihat lebih dulu.
- **100% bukan jaminan.** Angka presisi bin teratas diukur pada data held-out (200/200 sel saat draf ini),
  dan batas bawah Clopper-Pearson 95%-nya **98.51%** — itu angka yang boleh dijanjikan ke pemanggil.
- **Kedua kunci dokumen dan ketujuh kunci tiap anggota selalu ada**, walau nilainya `""`.
- `anggota_keluarga` adalah array dengan urutan sesuai baris pada kartu.

### 3.3.1 Proyeksi dari hasil structuring ke `data`

Tahap structuring tetap mengekstraksi **11 field dokumen + 15 field per anggota** ([7.3](#73-hasil-tahap-structuring))
— itu yang disimpan di `structuring_results` dan terbaca lewat `GET /v1/structuring/jobs/{request_id}`.
Orchestrator memproyeksikan sebagian kecilnya ke `data`, sekaligus mengganti dua nama:

| Kunci di `data` (kontrak keluar) | Field internal structuring |
|---|---|
| `no_kk` | `nomor_kk` |
| `nama_kepala_keluarga` | `nama_kepala_keluarga` |
| `anggota_keluarga[].nama_lengkap` | `nama_lengkap` |
| `anggota_keluarga[].nik` | `nik` |
| `anggota_keluarga[].pendidikan` | `pendidikan` |
| `anggota_keluarga[].jenis_pekerjaan` | `jenis_pekerjaan` |
| `anggota_keluarga[].status_hubungan_dalam_rumah_tangga` | `status_hubungan_dalam_keluarga` |
| `anggota_keluarga[].ayah` | `ayah` |
| `anggota_keluarga[].ibu` | `ibu` |

Hanya dua baris yang **berganti nama** — `nomor_kk` → `no_kk` dan `status_hubungan_dalam_keluarga` →
`status_hubungan_dalam_rumah_tangga`; tujuh sisanya memakai nama yang sama persis. Kartu Keluarga sendiri
mencetak "STATUS HUBUNGAN DALAM KELUARGA"; kontrak keluar memakai `status_hubungan_dalam_rumah_tangga`
sesuai permintaan konsumen. Kalau ternyata itu keliru, perbaiki di tabel ini saja — nama internal di
structuring dan scoring tidak ikut berubah.

Sembilan field ini disebut **field kontrak** di seluruh dokumen.

Field yang **tidak** keluar tapi tetap diekstraksi dan tersimpan: `alamat`, `rt`, `rw`, `desa_kelurahan`,
`kecamatan`, `kabupaten_kota`, `provinsi`, `kode_pos`, `tanggal_dikeluarkan`, dan per anggota
`jenis_kelamin`, `tempat_lahir`, `tanggal_lahir`, `agama`, `golongan_darah`, `status_perkawinan`,
`tanggal_perkawinan`, `kewarganegaraan`.

### 3.4 Response `202` — belum selesai saat waktu tunggu habis

```json
{"status_code": 202, "status_desc": "Accepted",
 "message": "OCR job accepted; still processing",
 "data": null, "errors": null, "request_id": "REQ_001",
 "document_type": "kk", "job_status": "processing", "guardrails": null,
 "params": {"branch": "0206"}}
```

Pipeline **tetap berjalan**; orchestrator hanya berhenti menonton. Ambil hasilnya dengan
`GET /v1/extract-ocr/{request_id}`.

### 3.5 Response `400` — dokumen ditolak

Bentuknya sama untuk kedua sumber penolakan; yang membedakan hanya `message`.

```json
{"status_code": 400, "status_desc": "Bad Request",
 "message": "Kualitas gambar terlalu rendah, mohon unggah foto yang lebih jelas",
 "data": null, "errors": "DOWNSTREAM_VALIDATION_ERROR", "request_id": "REQ_001",
 "document_type": "kk", "job_status": "failed", "guardrails": 1,
 "params": {"branch": "0206"}}
```

| Sumber | Tahap | Contoh `message` |
|---|---|---|
| model guardrails | sebelum pipeline | `Kualitas gambar terlalu rendah, mohon unggah foto yang lebih jelas` |
| aturan structuring | structuring | `Gambar tidak memuat teks yang terbaca, mohon unggah foto Kartu Keluarga` |
| aturan structuring | structuring | `Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil ekstraksi tidak lengkap` |

Semua `message` berbahasa Indonesia dan boleh ditampilkan langsung ke pengguna akhir. Penolakan oleh
guardrails terjadi **sebelum** tahap mana pun berjalan, jadi tidak ada baris job dan
`GET /v1/extract-ocr/{request_id}` untuk request itu menjawab `404`.

Hanya dua sumber ini yang bisa menolak dokumen. Tidak ada gerbang yang menolak karena tulisannya buram
tapi terbaca — lihat [1.1](#11-tidak-ada-penilaian-legibilitas-per-field).

### 3.6 Response `422` — satu tahap gagal

```json
{"status_code": 422, "status_desc": "Unprocessable Entity",
 "message": "ekstraksi OCR model is unavailable",
 "data": null, "errors": "OCR_FAILED", "request_id": "REQ_001",
 "document_type": "kk", "job_status": "failed", "guardrails": 0,
 "params": null}
```

`errors` menyebut tahapnya: `OCR_FAILED`, `STRUCTURING_FAILED`, `SCORING_FAILED`.

### 3.7 Anggaran waktu

`received_at` diambil di baris pertama handler, sebelum `params` di-parse. Anggaran
`PIPELINE_WAIT_SECONDS` (default **30 detik** untuk KK) dipakai berurutan oleh: baca/unduh file →
guardrails → `POST /v1/ekstraksi/jobs` → polling. Sisa untuk polling dihitung
`PIPELINE_WAIT_SECONDS - (now - received_at)`, tidak di-reset. Satu `asyncio.timeout` membungkus polling
ketiga tahap, jadi anggarannya dibagi bertiga, bukan per tahap.

Polling: `GET /v1/<tahap>/jobs/{request_id}` berurutan (OCR dulu sampai `DONE`, baru structuring, lalu
scoring), tiap `PIPELINE_POLL_INTERVAL_SECONDS` (default 0,5 detik). `404` dan `PROCESSING` sama-sama
berarti "tidur lalu tanya lagi". `503` / `504` dari sebuah tahap dicatat `WARNING` dan **diulang**, bukan
langsung digagalkan.

---

## 4. `GET /v1/extract-ocr/{request_id}` — keadaan request

Kontrak yang sama dengan `POST`, **tanpa menunggu**: status tiap tahap dibaca sekali, berurutan.

```bash
curl http://nlm-k2.nlm-k2.svc.cluster.local:8040/v1/extract-ocr/REQ_001 \
  -H "X-API-Key: $API_KEY"
```

| Keadaan | HTTP | `job_status` | `guardrails` | `errors` |
|---|---|---|---|---|
| selesai | 200 | `completed` + `data` | `0` | null |
| masih berjalan | 202 | `processing` | null | null |
| ditolak aturan structuring | 400 | `failed` | `1` | `DOWNSTREAM_VALIDATION_ERROR` |
| satu tahap gagal | 422 | `failed` | `0` | `<TAHAP>_FAILED` |
| tidak dikenal | 404 | – | – | = `message` |
| sebuah tahap tak terbaca | 503 / 504 | – | – | = `message` |

- `params` **selalu** `null` di sini (tidak disimpan). `document_type` selalu `kk`.
- Tahap yang dibaca berhenti di service terakhir `pipeline_name_sequence` yang tersimpan di job ekstraksi.
- Request yang **tidak punya job tahap** dijawab dari putusan guardrails terakhirnya di
  `guardrails_results` (draf 12, seperti nilam), sama dengan jawaban `POST`-nya: `400` kalau ditolak
  guardrails, `200` dengan laporan sebagai `data` kalau guardrails satu-satunya service-nya.
- `404` berarti tidak ada job tahap **dan** tidak ada putusan yang menjawab: `POST`-nya belum dinilai,
  ditolak sebelum dinilai, lolos tetapi handoff ke ekstraksi gagal, atau tidak ada `DATABASE_URL`.
- Penolakan aturan structuring terbaca di sini karena `reject_reason` ikut di dalam payload hasil
  structuring ([7.3](#73-hasil-tahap-structuring)) — orchestrator membacanya lewat
  `GET /v1/structuring/jobs/{request_id}`, tanpa perlu akses database.
- **Keterbatasan endpoint ini:** handoff antar tahap yang mati permanen (semua retry habis, atau menjadi
  dead letter di outbox) meninggalkan tahap berikutnya tanpa baris job, sehingga endpoint ini tetap
  menjawab `202`. Keadaan finalnya ada di tabel outcome, yang pada saat itu sudah ditandai `failed` +
  `<TAHAP BERIKUTNYA>_FAILED` ([2.6](#26-tabel-outcome-dan-cara-hasil-sampai-ke-pemanggil)), dan di
  `GET /v1/<tahap>/outbox`. Karena Orkestrasi pusat membaca tabel itu langsung, request tidak menggantung
  selamanya — tapi **untuk keadaan final, tabel outcome yang berlaku, bukan endpoint ini**.

---

## 5. orchestrator → guardrails

### 5.1 Request

```
POST http://nlm-k2-guardrails:8041/v1/guardrails/check
X-API-Key: <GUARDRAILS_API_KEY | API_KEY>
X-Request-ID: REQ_001
Content-Type: multipart/form-data
```

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `request_id` | string | ya | diteruskan apa adanya |
| `file` | file | salah satu | byte gambar yang sudah lolos cek tipe/ukuran di orchestrator |
| `file_url` | string | salah satu | dipakai kalau orchestrator menerima `file_url` dan `GUARDRAILS_FETCH_URL=true` |
| `threshold` | float | tidak | override `GUARDRAILS_THRESHOLD` untuk request ini (0.01–0.99); normalnya tidak dikirim |

### 5.2 Response — **selalu `200`**

Guardrails hanya menilai, tidak pernah menolak request. Vonisnya ada di `data.passed`.

```json
{
  "status_code": 200, "status_desc": "OK", "message": "OK",
  "errors": null, "request_id": "REQ_001",
  "data": {
    "passed": true,
    "reason": null,
    "document": {
      "verdict": "accepted",
      "confidence": 0.9713,
      "probability_bad": 0.0287,
      "threshold_used": 0.5
    }
  }
}
```

Saat ditolak:

```json
{
  "data": {
    "passed": false,
    "reason": "Kualitas gambar terlalu rendah, mohon unggah foto yang lebih jelas",
    "document": {
      "verdict": "reject",
      "confidence": 0.8821,
      "probability_bad": 0.8821,
      "threshold_used": 0.5
    }
  }
}
```

Saat gambarnya tidak bisa dinilai sama sekali — tidak bisa didekode, atau dimensinya di luar rentang
yang dilatihkan ke checkpoint:

```json
{
  "data": {
    "passed": false,
    "reason": "Gambar tidak bisa dibaca, mohon unggah ulang foto Kartu Keluarga",
    "document": {
      "verdict": "unassessable",
      "confidence": null,
      "probability_bad": null,
      "threshold_used": 0.5
    }
  }
}
```

- `probability_bad` adalah keluaran mentah model XGBoost `K2Quality` (`is_bad` = `probability >= threshold`).
- `confidence` = keyakinan pada vonis: `probability_bad` kalau `reject`, `1 - probability_bad` kalau `accepted`.
- **`unassessable` adalah vonis, bukan galat.** §5.2 mewajibkan selalu `200`, dan inti model melempar
  di lusinan tempat pada jalur ini; memetakannya ke `500` akan melanggar aturan itu. Ia dipisahkan dari
  `reject` karena "kami menilai ini buruk" dan "kami tidak bisa menilainya" butuh alarm dan perbaikan
  yang berbeda. `passed` tetap `false`: pipeline tidak boleh jalan atas gambar yang belum dinilai.
- `confidence` dan `probability_bad` **null** pada `unassessable`. Tahap scoring harus menanganinya,
  bukan mengasumsikan ada angka — meski pada praktiknya dokumen itu tidak pernah sampai ke sana.
- `reason` berbahasa Indonesia dan dipakai langsung sebagai `message` pada respons 400 orchestrator.
- **Blok `data` ini diteruskan apa adanya** ke `/v1/ekstraksi/jobs` sebagai field form `guardrails`, lalu
  mengalir ke structuring dan scoring tanpa diubah. `probability_bad` menjadi fitur `guardrail_probability`
  di trust model tahap scoring.

Model ini biner (bagus / tidak bagus) dan menilai gambar **secara keseluruhan**, jadi `reason`-nya tidak
bisa menyebut bagian mana yang bermasalah. Ini satu-satunya penilaian kualitas gambar di seluruh pipeline,
sehingga `GUARDRAILS_THRESHOLD` (default 0.5) adalah satu-satunya tuas untuk menolak foto yang buruk.

### 5.3 Kegagalan

Guardrails tidak terjangkau → orchestrator menjawab `503`; tidak menjawab dalam
`GUARDRAILS_TIMEOUT_SECONDS` (default 20 dtk) → `504`. **Tidak ada retry** untuk guardrails: pemanggil
boleh kirim ulang. Pipeline tidak dimulai.

---

## 6. orchestrator → ekstraksi

### 6.1 Request

```
POST http://nlm-k2-ekstraksi:8042/v1/ekstraksi/jobs
X-API-Key: <EKSTRAKSI_API_KEY | API_KEY>
X-Request-ID: REQ_001
Content-Type: multipart/form-data
```

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `request_id` | string | ya | sama dengan yang dikirim pemanggil |
| `document_type` | string | tidak | default `kk` |
| `guardrails` | string (JSON object) | tidak | `data` dari 5.2, di-serialize. **Dihilangkan** kalau `pipeline_name_sequence` tanpa `guardrails`; bukan objek JSON → 400 |
| `pipeline_name_sequence` | string (JSON array) | tidak | urutan dari 3.1; tidak sah atau tanpa `ekstraksi` → 400. Kalau `ekstraksi` yang terakhir, job berakhir di sini: tidak ada handoff, dan callback `OCR` `DONE` membawa `final: true` |
| `file` | file | salah satu | byte gambar, kalau pemanggil mengunggah inline |
| `file_url` | string | salah satu | URL asli, kalau pemanggil mengirim `file_url` — diteruskan, bukan byte-nya |

Retry: `PIPELINE_RETRY_ATTEMPTS` kali (default 3) dengan backoff eksponensial dari
`PIPELINE_RETRY_DELAY_SECONDS` (default 0,5 dtk), **hanya** untuk 5xx dan tidak terjangkau. 4xx tidak
diulang dan diteruskan ke pemanggil.

### 6.2 Response `202`

```json
{
  "status_code": 202, "status_desc": "Accepted",
  "message": "OCR job accepted", "errors": null, "request_id": "REQ_001",
  "data": {"request_id": "REQ_001", "stage": "OCR", "status": "PROCESSING", "duplicate": false}
}
```

`status` adalah `PROCESSING` untuk job baru, atau status terkini kalau `duplicate: true`.

**Validasi isi terjadi di background, bukan di sini.** Gambar rusak atau `document_type` tidak didukung →
muncul sebagai job `FAILED`, bukan 4xx. Yang langsung ditolak hanya bentuk request yang salah (400 intake,
401, 422).

### 6.3 Yang dikerjakan tahap ekstraksi

1. Satu transaksi klaim: `INSERT ocr_jobs (PROCESSING, input: {document_type, guardrails, file_url})
   ON CONFLICT DO NOTHING` + upsert baris outcome `{downstream_status: processing, stage: OCR}`.
2. `file_url`? unduh; selain itu pakai byte dari payload.
3. **PaddleOCR** (deteksi + rekognisi). Backend `paddle` (draf 12, sama dengan nilam) memanggil server
   PaddleOCR tim ML di `POST /ocr` dan meneruskan `poly` apa adanya — **tidak** dijadikan `bbox` tegak
   seperti di nilam, karena parser tata letak mengukur kemiringannya. `kk_ocr` menjalankan PP-OCRv5 di
   dalam proses. PDF: hanya halaman 1.
4. Satu transaksi hasil: `UPSERT ocr_results` + `UPDATE ocr_jobs DONE` + `INSERT pipeline_outbox`
   (handoff ke structuring).

**Tahap ini tidak pernah menolak dokumen.** Gambar yang tidak bisa dibuka, model OCR tidak terjangkau,
`document_type` tidak didukung → job `FAILED` + `OCR_FAILED` → `422`. Gambar kosong **bukan** kegagalan:
OCR menghasilkan `texts: []`, job tetap `DONE`, dan penolakannya terjadi di structuring (7.4). Ini
mengikuti nilam, di mana "dokumen blur / blank" adalah salah satu aturan structuring, bukan cek di tahap
OCR — supaya semua penolakan berbasis isi punya satu tempat dan satu kanal.

Tahap ini juga **tidak** memanggil klasifikator crop dan **tidak** membawa extractor KK apa pun.

---

## 7. ekstraksi → structuring

### 7.1 Request

```
POST http://nlm-k2-structuring:8043/v1/structuring/jobs
X-API-Key: <STRUCTURING_API_KEY | API_KEY>
X-Request-ID: REQ_001
Content-Type: application/json
```

```json
{
  "request_id": "REQ_001",
  "document_type": "kk",
  "guardrails": { "...dari 5.2, diteruskan apa adanya..." },
  "ocr": {
    "engine": "paddle",
    "model": "PP-OCRv6_server_det+PP-OCRv6_server_rec",
    "elapsed_ms": 1842.5,
    "text_regions_count": 214,
    "avg_doc_score": 0.9813,
    "min_doc_score": 0.7441,
    "texts": [
      {"text": "KARTU KELUARGA", "score": 0.9999,
       "poly": [[515, 100], [984, 148], [978, 202], [509, 154]]},
      {"text": "3273012345678901", "score": 0.9991,
       "poly": [[520, 210], [1100, 214], [1100, 262], [520, 258]]}
    ]
  }
}
```

**`texts` memakai bentuk PP-OCRv6 `{text, score, poly}`** — bentuk yang sudah dipakai `K2Regex-v2`, bukan
bentuk lama `[[poly, [text, score]]]` dari `K2Extractor`. Adaptasinya tanggung jawab tahap ekstraksi.
`poly` selalu 4 titik × 2 koordinat, dalam piksel gambar yang **sudah** diluruskan PaddleOCR — bukan
koordinat pada foto yang diunggah.

**`texts` boleh kosong.** `K2Regex-v2` sekarang mewajibkan `min_length=1` pada `/v1/ocr_postprocess`;
endpoint job yang baru ini **tidak boleh** mewarisi batasan itu, karena gambar kosong harus sampai ke
aturan structuring untuk ditolak di sana, bukan ditolak `422` di batas skema. Endpoint sinkron
`/v1/ocr_postprocess` yang lama boleh tetap `min_length=1`.

`avg_doc_score` dan `min_doc_score` dihitung dari `texts[].score`; keduanya menjadi fitur trust model.
Saat `texts` kosong, keduanya `null`.

**Handoff by reference.** Dengan `PIPELINE_HANDOFF_BY_REFERENCE=true` (butuh `DATABASE_URL` yang sama di
ketiga tahap), blok `ocr` **dihilangkan** dan structuring membacanya sendiri dari `ocr_results`. Ini
penting untuk KK: satu kartu bisa menghasilkan 200+ kotak teks, dan payload sebesar itu ikut tersimpan di
baris `pipeline_outbox`. Penerima menerima kedua bentuk; `ocr` yang tidak ada dan tidak ditemukan di
database membuat job `FAILED` dengan pesan yang menyebutkannya. Tanpa `DATABASE_URL`, body tanpa `ocr`
ditolak `422`.

### 7.2 Response `202`

Sama dengan 6.2, dengan `"stage": "STRUCTURING"`.

### 7.3 Hasil tahap structuring

Isi `structuring_results.result` adalah objek `data.ocr_result` K2Regex-v2 — struktur datar yang sama,
tanpa penggantian nama — dengan **dua perubahan**: tiap field membawa dua skor (`ocr_conf`, `crf_conf`)
menggantikan `conf` tunggal, dan ada satu kunci tingkat atas baru, `reject_reason`.

```json
{
  "nomor_kk":             {"value": "3273012345678901", "ocr_conf": 0.9991, "crf_conf": null},
  "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "ocr_conf": 0.9873, "crf_conf": null},
  "alamat":               {"value": "JL. MERDEKA NO. 12", "ocr_conf": 0.9642, "crf_conf": null},
  "desa_kelurahan":       {"value": "CIHAPIT", "ocr_conf": 0.9810, "crf_conf": null},
  "rt":                   {"value": "003", "ocr_conf": 0.9755, "crf_conf": null},
  "rw":                   {"value": "007", "ocr_conf": 0.9755, "crf_conf": null},
  "kecamatan":            {"value": "BANDUNG WETAN", "ocr_conf": 0.9888, "crf_conf": null},
  "kabupaten_kota":       {"value": "KOTA BANDUNG", "ocr_conf": 0.9901, "crf_conf": null},
  "provinsi":             {"value": "JAWA BARAT", "ocr_conf": 0.9934, "crf_conf": null},
  "kode_pos":             {"value": "40114", "ocr_conf": 0.9702, "crf_conf": null},
  "tanggal_dikeluarkan":  {"value": "12-03-2019", "ocr_conf": 0.9219, "crf_conf": null},
  "anggota_keluarga": [
    {
      "nama_lengkap":                   {"value": "BUDI SANTOSO", "ocr_conf": 0.9954, "crf_conf": 0.9931},
      "nik":                            {"value": "3273011203850001", "ocr_conf": 0.9975, "crf_conf": 0.9887},
      "jenis_kelamin":                  {"value": "LAKI-LAKI", "ocr_conf": 0.9968, "crf_conf": 0.9954},
      "tempat_lahir":                   {"value": "BANDUNG", "ocr_conf": 0.9891, "crf_conf": 0.9803},
      "tanggal_lahir":                  {"value": "12-03-1985", "ocr_conf": 0.9903, "crf_conf": 0.9877},
      "agama":                          {"value": "ISLAM", "ocr_conf": 0.9944, "crf_conf": 0.9912},
      "pendidikan":                     {"value": "S1", "ocr_conf": 0.9840, "crf_conf": 0.9102},
      "jenis_pekerjaan":                {"value": "KARYAWAN SWASTA", "ocr_conf": 0.9611, "crf_conf": 0.8774},
      "golongan_darah":                 {"value": "O", "ocr_conf": 0.7120, "crf_conf": 0.4415},
      "status_perkawinan":              {"value": "KAWIN", "ocr_conf": 0.9821, "crf_conf": 0.9688},
      "tanggal_perkawinan":             {"value": "08-08-2010", "ocr_conf": 0.9560, "crf_conf": 0.9341},
      "status_hubungan_dalam_keluarga": {"value": "KEPALA KELUARGA", "ocr_conf": 0.9788, "crf_conf": 0.9440},
      "kewarganegaraan":                {"value": "WNI", "ocr_conf": 0.9972, "crf_conf": 0.9955},
      "ayah":                           {"value": "SUTRISNO", "ocr_conf": 0.9705, "crf_conf": 0.8312},
      "ibu":                            {"value": "SITI AMINAH", "ocr_conf": 0.9682, "crf_conf": 0.8190}
    }
  ],
  "reject_reason": null
}
```

Aturan bentuk — ini yang mengikat, bukan contoh di atas:

- **Struktur datar.** Kesebelas field dokumen adalah kunci tingkat atas, **bukan** di bawah `fields`.
  `anggota_keluarga` dan `reject_reason` adalah kunci ke-12 dan ke-13.
- **Kunci nomor KK adalah `nomor_kk`**, bukan `no_kk`. Parser memang memancarkan `nomor_kk`; penggantian
  nama ke `no_kk` terjadi di orchestrator ([3.3.1](#331-proyeksi-dari-hasil-structuring-ke-data)).
- Tiap field `{value, ocr_conf, crf_conf}`. `value` selalu string; `""` kalau tidak ditemukan,
  **tidak pernah** `null`.
- **`ocr_conf`** = skor rekognisi terendah di antara kotak OCR pembentuk nilai itu. Ada untuk field dokumen
  maupun field anggota. `null` kalau field tidak ditemukan.
- **`crf_conf`** = marginal forward-backward penempatan kolom. **Selalu `null` untuk field dokumen** (regex
  /posisional, tidak pernah lewat Viterbi) dan untuk sel anggota yang tidak ditempatkan Viterbi.
- Keduanya **tidak dilebur**: keduanya gagal dengan cara berbeda — `ocr_conf` rendah berarti mesin
  rekognisi ragu pada teksnya, `crf_conf` rendah berarti teksnya terbaca tapi tidak jelas masuk kolom mana.
  Peleburan keduanya menjadi satu P(benar) adalah tugas tahap scoring.
- **`features`** = vektor masukan trust model untuk field itu, **hanya ada pada sembilan field kontrak**
  dan `null` di semua field lain. Nama-namanya dipatok `ocr_common.kk.MEMBER_CELL_FEATURES` (46 nama, sel
  anggota) dan `DOC_CELL_FEATURES` (13 nama, field dokumen). Lihat
  [7.3.1](#731-kenapa-ada-features-dan-kenapa-ia-lahir-di-sini).
- **`reject_reason`** = alasan pertama yang menolak dokumen, bahasa Indonesia; `null` kalau dokumen
  diterima. Perannya dijelaskan di [7.4](#74-gerbang-validitas-kk).
- Tiap anggota selalu membawa kelima-belas kunci, walau nilainya `""`.
- **Tidak ada** `document_type`, `flag`, maupun `flag_reason`. nilam punya `flag` / `flag_reason` sebagai
  penanda lunak untuk trust model; `K2Regex-v2` belum memproduksi padanannya, jadi keduanya sengaja tidak
  dikarang di sini.
- Parser yang tidak menemukan kotak apa pun tetap mengembalikan objek lengkap: semua `value: ""`,
  kedua skor `null`, `anggota_keluarga: []`, dan `reject_reason` terisi.

#### Dari mana kedua skor itu diambil

Keduanya **sudah dihitung** `kk_layout_parser.structure(..., debug=True)` dan tersedia di `_meta`; yang
perlu berubah hanya `build_kk_response`, yang sekarang membuang yang pertama:

| Kunci respons | Sumber di `_meta` |
|---|---|
| field dokumen `.ocr_conf` | `_meta.conf[<field>]` |
| field dokumen `.crf_conf` | selalu `null` |
| `anggota_keluarga[i].<field>.ocr_conf` | `_meta.conf["anggota_keluarga"][i][<field>]` |
| `anggota_keluarga[i].<field>.crf_conf` | `_meta.crf_conf[i][<field>]` |

Tiga hal yang perlu diperhatikan saat mengimplementasikannya:

1. **`rt` dan `rw` berbagi satu `ocr_conf`.** `_meta.conf` menyimpannya sebagai satu kunci `rt_rw`, karena
   keduanya dipotong dari satu kotak yang sama. Salin nilai yang sama ke kedua field.
2. **`nama_kepala_keluarga` lewat `consensus()`** atas empat sumber (label identitas, baris KEPALA KELUARGA,
   tanda tangan, dan `ayah` milik ANAK). Kalau konsensus memilih sumber selain label identitas, `_meta.conf`
   tidak menggambarkan nilai yang dikembalikan. Angkanya tetap dipakai sebagai perkiraan, tapi tim ML perlu
   tahu bahwa untuk field ini `ocr_conf` lebih lemah maknanya dibanding field lain.
3. **Nilai yang diperbaiki `repair_from_nik` / `unify_names`** membawa `ocr_conf` dari bacaan **sebelum**
   perbaikan. `_meta.repairs` dan `_meta.name_fixes` mencatat mana saja yang tersentuh; kalau nanti terbukti
   penting sebagai fitur, tambahkan penanda `corrected` per field (aditif, tidak memecah kontrak ini).

`GET /v1/structuring/jobs/{request_id}` membungkus objek ini di `data.result` mengikuti envelope 2.1.

### 7.3.1 Kenapa ada `features`, dan kenapa ia lahir di sini

**Kedua skor di atas tidak cukup.** Diukur atas 1686 sel berlabel tangan dari 97 dokumen, `crf_conf`
mencapai AUC **0.502** terhadap benar/salah — tidak lebih baik dari koin. Sebabnya struktural dan sudah
diketahui: marginal forward-backward dinormalisasi atas himpunan kolom yang ada, **tanpa state "bukan kolom
mana pun"**, jadi ia mengukur ketegasan pilihan relatif terhadap kolom lain di baris yang sama, bukan
probabilitas nilainya benar. Ia pun saturasi mendekati 1.0 pada sel yang posisinya bersih.

Sinyal yang bekerja **sudah dihitung parser lalu dibuang**: skor emisi mentah, geometri sel, jarak ke
kandidat vocab runner-up, dan hasil cek silang terhadap NIK. Dengan vektor itu, model yang sama naik ke AUC
**0.855**, dan itulah yang membuat bin teratas berpresisi 100% mungkin sama sekali.

Vektornya lahir **di structuring**, bukan dirakit ulang di scoring, dan itu bukan pilihan gaya: fitur yang
dihitung di dua tempat adalah cara paling andal menghasilkan model yang bagus saat latih lalu buruk saat
dipakai. Scoring hanya menyusunnya menjadi matriks.

Tiga fitur terpenting, dari permutation importance (penurunan AUC saat diacak, rata-rata 5 fold):

| Fitur | Pengaruh | Catatan |
|---|---|---|
| `ocr_min` (= `ocr_conf`) | 0.0743 | **dominan**; sudah ada di kontrak ini |
| `confusable_ratio` | 0.0393 | rasio karakter mudah tertukar (`O/0`, `I/1`, `S/5`) pada nilainya |
| `emis_assigned_min` | 0.0345 | skor emisi MENTAH, bukan marginal yang sudah dinormalisasi |
| `marg_min` (≈ `crf_conf`) | 0.0027 | angka yang dulu satu-satunya dikirim, ~27× lebih lemah dari `ocr_min` |

Yang perlu dicatat untuk yang akan mem-port K2Regex-v2: fitur turunan marginal (`margin_min`, entropi,
logit margin) **tidak membantu** — diuji dan nol pengaruhnya. Yang berguna dari CRF justru emisi mentah.

Empat fitur tidak datang dari sini karena structuring tidak memilikinya, dan scoring menambahkannya
sendiri: untuk sel anggota `ocr_min` dan `crf_conf` (dari field itu sendiri), untuk field dokumen
`ocr_conf` plus `avg_doc_score` / `min_doc_score` / `text_regions_count` dari agregat OCR.

**Selama structuring belum mem-port K2Regex-v2, `features` dari backend `mock` bersifat sintetis.** Ia
lengkap dan setiap sel berbeda — cukup untuk membuktikan vektornya sampai utuh dan penyelarasan
posisionalnya benar — tapi confidence yang dihitung darinya tidak mengatakan apa pun tentang sebuah
dokumen. Batas itu sama dengan yang sudah dicatat untuk isi `data`.

### 7.4 Gerbang validitas KK

Ini **satu-satunya gerbang isi** di seluruh pipeline, dan mekanismenya mengikuti nilam persis.

**Aturan structuring yang menghasilkan `reject_reason`:**

| Kondisi | `reject_reason` |
|---|---|
| tidak ada kotak teks sama sekali | `Gambar tidak memuat teks yang terbaca, mohon unggah foto Kartu Keluarga` |
| `nomor_kk` kosong atau `"Not found"` | `Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil ekstraksi tidak lengkap` |
| tidak ada anggota dengan `nik` **dan** `nama_lengkap` terisi | idem |

Kalau lebih dari satu kondisi terpenuhi, yang dipakai adalah yang pertama menurut urutan di atas.

**Bagaimana alasan itu sampai ke orchestrator.** Ini bagian yang mudah salah, jadi eksplisit:

1. Aturan structuring menulis `reject_reason` **ke dalam objek hasil** (7.3) — bukan ke tempat lain.
2. Hook `rejection` di tahap structuring hanya **membaca** kunci itu:
   `reject_reason = result.get("reject_reason") or None`.
3. Kalau terisi, mesin tahap: menyimpan job **`DONE`** (hasilnya tetap terbaca), **tidak** melakukan
   handoff ke scoring, dan menandai baris outcome `400` + `DOWNSTREAM_VALIDATION_ERROR`.
4. Orchestrator membaca ulang `reject_reason` dari `data.result` lewat
   `GET /v1/structuring/jobs/{request_id}`, dan mengubahnya menjadi `400` + `DOWNSTREAM_VALIDATION_ERROR`
   + `guardrails: 1` dengan `reject_reason` sebagai `message`.

Langkah 4 adalah alasan `reject_reason` **harus** ada di dalam payload hasil dan tidak boleh sekadar
diturunkan di dalam tahap: orchestrator stateless dan hanya melihat tahap lewat API. Kalau alasannya hanya
ada di baris outcome, orchestrator tidak akan pernah bisa menjawab 400 — job `DONE` tanpa penanda tidak
bisa dibedakan dari handoff yang masih di jalan, dan akan terbaca `202` selamanya.

Status job tetap `PROCESSING` | `DONE` | `FAILED`; tidak ada status `REJECTED`. Dokumen yang ditolak adalah
job yang **berhasil** — hasilnya valid, isinya yang tidak layak diteruskan.

---

## 8. structuring → scoring

### 8.1 Request

```
POST http://nlm-k2-scoring:8044/v1/scoring/jobs
X-API-Key: <SCORING_API_KEY | API_KEY>
X-Request-ID: REQ_001
Content-Type: application/json
```

```json
{
  "request_id": "REQ_001",
  "document_type": "kk",
  "guardrails":  { "...dari 5.2..." },
  "ocr":         { "...dari 7.1, dipakai untuk avg/min_doc_score..." },
  "structuring": { "...objek datar dari 7.3, apa adanya..." }
}
```

`structuring` memakai nama internal (`nomor_kk`, `status_hubungan_dalam_keluarga`), bukan nama kontrak
keluar. Scoring bekerja dengan nama internal; penggantian nama hanya terjadi di orchestrator.

Handoff ini hanya pernah terjadi untuk dokumen dengan `reject_reason: null` — dokumen yang ditolak berhenti
di structuring.

`ocr` dan `structuring` boleh dihilangkan kalau pengirim memakai handoff by reference; scoring membacanya
dari `ocr_results` dan `structuring_results`.

### 8.2 Response `202`

Sama dengan 6.2, dengan `"stage": "SCORING"`.

### 8.3 Hasil tahap scoring

```json
{
  "document_type": "kk",
  "fields": {
    "nomor_kk": 0.9412,
    "nama_kepala_keluarga": 0.8871
  },
  "anggota_keluarga": [
    {
      "nama_lengkap": 0.9702, "nik": 0.9655, "pendidikan": 0.8410,
      "jenis_pekerjaan": 0.7733, "status_hubungan_dalam_keluarga": 0.9218,
      "ayah": 0.8064, "ibu": 0.7951
    }
  ],
  "model": "kk-trust-hgb-isotonic-v1",
  "thresholds": {
    "nama_lengkap": 0.9652, "nik": 0.9637, "pendidikan": 0.9929, "jenis_pekerjaan": 0.9811,
    "status_hubungan_dalam_keluarga": 0.9786, "ayah": 0.9353, "ibu": 0.9671,
    "nama_kepala_keluarga": 0.8596
  },
  "bin_edges": {
    "fields": [0.0, 0.583, 0.827, 0.831, 0.832, 0.86, 0.867, 0.902, 0.944, 1.0],
    "anggota_keluarga": [0.0, 0.631, 0.776, 0.839, 0.899, 0.925, 0.943, 0.965, 0.979, 0.9874, 1.0]
  },
  "payload": { "...fitur persis yang diskor, sebagai jejak audit..." }
}
```

Tiap angka adalah **P(nilai field ini PERSIS sama dengan kebenaran)** — kapital & spasi dirapikan — hasil
fusi sinyal + kalibrasi isotonic. Satu angka untuk dua hal sekaligus: penempatan kolom benar DAN teks OCR
benar, karena keduanya harus benar agar string akhirnya sama. Field yang nilainya kosong mendapat `null`,
dan begitu juga field yang tidak bisa diskor secara jujur (lihat di bawah). Tidak ada skor dokumen dan
tidak ada keputusan terima/tolak: ambang milik pemanggil.

**`thresholds`** = confidence di atas mana setiap sampel held-out field itu benar, satu per field kontrak,
memakai nama internal. Ia ikut di dalam hasil **bukan di konfigurasi**, karena ia properti model yang
dilatih, bukan properti deployment — dan karena itulah yang membuat `auto` di respons `extract-ocr`
identik dengan `auto` di baris outcome secara konstruksi, bukan dengan menjaga dua env var tetap sinkron.
Ambang harus berbeda per field: terukur, `ayah` sudah aman di 0.935 sementara `pendidikan` butuh 0.993, dan
satu angka global menjatuhkan cakupan pada presisi 100% ke nol. Field yang **tidak ada** di sini tidak
punya titik seperti itu di data latih — `nomor_kk` salah satunya, hanya 5 sel bersih dari 97 — dan jatuh ke
`FIELD_CONFIDENCE_THRESHOLD`. Ketiadaannya adalah informasi, bukan cacat.

**`bin_edges`** = tepi bin dari rendah ke tinggi, terpisah untuk kedua keluarga model. Sengaja tidak
selebar sama: bin teratas dipotong tepat di titik presisi 100% terukur, yang tidak jatuh pas di kisi mana
pun. `null` pada keduanya berarti sepuluh bin lebar sama.

**Dua keluarga model, bukan satu.** Sel anggota punya `crf_conf` dan seluruh internal CRF; field dokumen
tidak punya `crf_conf` sama sekali, jadi buktinya datang dari tempat lain (`kepala_votes` — empat saksi
independen yang `consensus()` pilih satu — plus agregat OCR). Satu model atas keduanya harus belajar
mengabaikan separuh masukannya tergantung sebuah flag, dan diukur atas campuran dua base rate yang jauh
berbeda.

**Field yang nilainya ada tapi `features`-nya tidak akan diskor `null`, bukan diberi angka terkaan.** Itu
keadaan ketika structuring belum memancarkan vektor fitur ([7.3.1](#731-kenapa-ada-features-dan-kenapa-ia-lahir-di-sini)),
dan mengarang confidence di situ adalah satu-satunya kegagalan yang tahap ini ada untuk mencegah: angka
yang terlihat berwibawa dan dihitung dari apa pun. Nama field yang terdampak ditulis ke log sebagai
`WARNING`.

**Angka yang terukur saat draf ini** (1686 sel berlabel, 97 dokumen, GroupKFold per dokumen, isotonic
bersarang):

| | anggota | dokumen |
|---|---|---|
| sel berlabel | 1686 | 162 |
| akurasi exact | 86.89% | 83.95% |
| AUC out-of-fold | **0.855** | 0.815 |
| AUC `crf_conf` mentah, sebagai pembanding | 0.502 | 0.619 (`ocr_conf`) |
| cakupan pada presisi 100%, ambang per field | **26.6%** (449 sel, nol salah) | 30.9% (50 sel) |
| batas bawah Clopper-Pearson 95% | **99.34%** | 94.18% |

Angka 100% itu **diukur pada held-out, bukan dijamin**. Yang boleh dijanjikan ke pemanggil adalah batas
bawah Clopper-Pearson-nya. Untuk menjanjikan 99.9% dibutuhkan ~3000 sel berurutan tanpa satu pun salah di
bin teratas; yang tersedia sekarang 1686 sel seluruhnya.

**Scoring hanya menilai sembilan field kontrak** (2 dokumen + 7 anggota), bukan seluruh 11 + 15 yang
diekstraksi structuring. Alasannya: tiap field butuh kalibratornya sendiri, dan melatih 26 kalibrator untuk
9 angka yang dipakai adalah pemborosan. Kunci di sini memakai **nama internal**; penggantian nama ke
kontrak keluar terjadi di orchestrator (3.3.1).

`payload` menyimpan persis apa yang diskor supaya angka-angka itu bisa direproduksi tanpa menjalankan ulang
pipeline.

### 8.4 Endpoint sinkron untuk tim ML

```
POST /v1/scoring/confidence
Content-Type: application/json
```

```json
{
  "document_type": "kk",
  "guardrail_probability": 0.0287,
  "avg_doc_score": 0.9813,
  "min_doc_score": 0.7441,
  "n_anggota": 4,
  "fields": {
    "nomor_kk":             {"value": "3273012345678901", "ocr_conf": 0.9991, "crf_conf": null},
    "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "ocr_conf": 0.9873, "crf_conf": null}
  },
  "anggota_keluarga": [
    {
      "nama_lengkap":                   {"value": "BUDI SANTOSO", "ocr_conf": 0.9954, "crf_conf": 0.9931},
      "nik":                            {"value": "3273011203850001", "ocr_conf": 0.9975, "crf_conf": 0.9887},
      "pendidikan":                     {"value": "S1", "ocr_conf": 0.9840, "crf_conf": 0.9102},
      "jenis_pekerjaan":                {"value": "KARYAWAN SWASTA", "ocr_conf": 0.9611, "crf_conf": 0.8774},
      "status_hubungan_dalam_keluarga": {"value": "KEPALA KELUARGA", "ocr_conf": 0.9788, "crf_conf": 0.9440},
      "ayah":                           {"value": "SUTRISNO", "ocr_conf": 0.9705, "crf_conf": 0.8312},
      "ibu":                            {"value": "SITI AMINAH", "ocr_conf": 0.9682, "crf_conf": 0.8190}
    }
  ]
}
```

Blok per field di sini adalah **potongan langsung** dari hasil structuring (7.3) — nama kunci dan maknanya
sama persis, jadi scoring tidak perlu menerjemahkan apa pun, **termasuk `features`** yang di contoh ini
dipersingkat. `n_anggota` diturunkan dari panjang `anggota_keluarga`. Semua nilai boleh `null`; field yang
nilainya `null` mendapat confidence `null`. Endpoint ini tidak membuat job dan tidak menyentuh database —
untuk kalibrasi dan evaluasi model.

Tiga catatan untuk tim ML:

- **Field dokumen dan field anggota adalah dua keluarga model.** `crf_conf` selalu `null` untuk field
  dokumen, jadi kalibratornya bekerja dengan `ocr_conf`, `DOC_CELL_FEATURES`, dan fitur tingkat dokumen
  (`avg/min_doc_score`, `text_regions_count`). Field anggota punya dua skor plus 46 fitur per sel.
- **Kirimkan `features`.** Tanpa blok itu setiap field anggota diskor `null`: dua skor saja tidak memisahkan
  nilai benar dari nilai salah (`crf_conf` AUC 0.502), jadi endpoint ini tidak akan mengarang angka untuk
  menutupinya. Nama-namanya di `ocr_common.kk.MEMBER_CELL_FEATURES` / `DOC_CELL_FEATURES`.
- **`guardrail_probability` belum dipakai model mana pun.** Korpus GT tidak punya hasil guardrails, jadi
  kolomnya selalu kosong saat latih dan dibuang. Ia tetap diterima di badan request supaya tidak perlu
  perubahan kontrak nanti, tapi begitu guardrails masuk GT model harus **dilatih ulang** — bukan sekadar
  diberi kolomnya.
- **Tidak ada sinyal legibilitas terpisah** ([1.1](#11-tidak-ada-penilaian-legibilitas-per-field)), jadi
  `ocr_conf` harus menanggung peran itu sendirian — dan terukur, ia memang fitur terpenting yang ada
  (permutation importance 0.0743, ~27× `crf_conf`).
- **Tidak ada fitur `flag`**: hasil structuring tidak memancarkannya, jadi sinyal "ada yang mencurigakan"
  harus diturunkan dari fitur-fitur di atas, bukan dari penanda siap pakai.
- **Plafon yang diketahui.** Semua fitur teks yang ada bersifat self-consistency atau leksikon; tidak satu
  pun bukti independen bahwa OCR-nya benar. Yang paling mungkin menembusnya adalah kesepakatan recognizer
  kedua (atau rec yang sama dengan preprocessing berbeda), bukan model yang lebih besar.

### 8.5 Menutup request

Scoring adalah tahap terakhir: tidak ada handoff, tidak ada baris outbox. Dalam **satu transaksi**:
`UPSERT scoring_results` + `UPDATE scoring_jobs DONE` + upsert baris outcome
`{status_code: 200, downstream_status: completed, downstream_stage: SCORING, result_data: <data kontrak
extract-ocr>}`.

Karena scoring merakit `result_data` dalam bentuk kontrak, ia memakai `FIELD_CONFIDENCE_THRESHOLD` juga.
**Nilainya harus sama persis dengan yang dipakai orchestrator**, kalau tidak baris outcome dan respons
`extract-ocr` bisa berbeda untuk request yang sama.

**Kewajiban yang dibawa dari `K2Orchestrator` dan tidak boleh hilang:** hasil akhir mengandung PII (NIK,
nama, alamat). Sebelum ditulis ke tabel audit, hasil itu harus dienkripsi (Fernet, `PII_ENCRYPTION_KEY`)
dan disertai blind index atas `nomor_kk` supaya baris tetap bisa dicari tanpa menyimpan data pribadi dalam
bentuk terbaca. Audit bersifat *fail-closed*: kalau enkripsi tidak terkonfigurasi atau insert audit gagal,
job ditandai `FAILED`, bukan diam-diam sukses. Tanggung jawab ini pindah dari orchestrator ke **scoring**,
karena scoring yang memegang hasil akhir.

---

## 9. `GET /v1/<tahap>/jobs/{request_id}` — dibaca orchestrator

Tersedia di ketiga tahap dengan bentuk identik. Internal; dipakai orchestrator untuk polling dan
`GET /v1/extract-ocr/{request_id}`, serta untuk debugging.

```json
{
  "status_code": 200, "status_desc": "OK", "message": "OK",
  "errors": null, "request_id": "REQ_001",
  "data": {
    "request_id": "REQ_001",
    "stage": "STRUCTURING",
    "status": "DONE",
    "result": { "...hasil tahap itu; untuk structuring termasuk reject_reason..." },
    "error_message": null,
    "created_at": "2026-09-26T04:12:30.118Z",
    "updated_at": "2026-09-26T04:12:33.481Z",
    "pipeline_name_sequence": ["guardrails", "ekstraksi", "structuring", "scoring"]
  }
}
```

- `stage`: `OCR` | `STRUCTURING` | `SCORING`.
- `status`: `PROCESSING` | `DONE` | `FAILED`. **Tidak ada `REJECTED`** — dokumen yang ditolak adalah job
  `DONE` yang `result.reject_reason`-nya terisi (7.4).
- `error_message` hanya terisi saat `FAILED`. Alasan penolakan **tidak** di sini, melainkan di
  `result.reject_reason`.
- `404` berarti tahap ini belum pernah menerima job untuk `request_id` itu.
- `created_at` = job diterima tahap ini, `updated_at` = selesai. Selisihnya adalah durasi tahap.
- `pipeline_name_sequence`: urutan yang dikirim bersama job; `null` untuk job tanpa urutan (seluruh pipeline).
- Sumber status resmi untuk pemanggil tetap `GET /v1/extract-ocr/{request_id}`; endpoint ini untuk
  rekonsiliasi dan debugging.

---

## 10. Skema data bersama

Definisi dipakai ulang di beberapa hop. Semua objek menerima field tambahan tanpa error (`extra: allow`),
supaya penambahan field di hulu tidak memecah hilir.

### `ContractField`

| Field | Tipe | Keterangan |
|---|---|---|
| `value` | string | `""` kalau tidak ditemukan, tidak pernah `null` |
| `confidence` | float 0–1 | P(nilai ini persis benar) dari trust model; `0.0` kalau tidak ada nilai atau tidak ada skor. **Tidak pernah `null`** |
| `bin` | int 1–10 | bin model atas `confidence`; tepinya tidak selebar sama, bin teratas dipotong di titik presisi 100% terukur |
| `auto` | bool | `confidence` melewati ambang yang dikalibrasi untuk field itu ([8.3](#83-hasil-tahap-scoring)); field tanpa ambang sendiri memakai `FIELD_CONFIDENCE_THRESHOLD` |

### `GuardrailsResult`

| Field | Tipe | Keterangan |
|---|---|---|
| `passed` | bool | true: dokumen boleh lanjut ke OCR |
| `reason` | string \| null | alasan penolakan, bahasa Indonesia; null kalau lolos |
| `document.verdict` | `accepted` \| `reject` \| `unassessable` | `unassessable` = gambar tidak bisa dinilai; tetap `200`, `passed: false` |
| `document.confidence` | float 0–1 \| null | keyakinan pada vonis; null kalau `unassessable` |
| `document.probability_bad` | float 0–1 \| null | keluaran mentah model; fitur trust model; null kalau `unassessable` |
| `document.threshold_used` | float | ambang yang berlaku saat penilaian |

### `OcrPayload`

| Field | Tipe | Keterangan |
|---|---|---|
| `engine` | string | backend OCR, mis. `paddle` |
| `model` | string \| null | identitas model yang dilaporkan service model |
| `elapsed_ms` | float | waktu di dalam model |
| `text_regions_count` | int | jumlah kotak teks; boleh `0` |
| `avg_doc_score`, `min_doc_score` | float \| null | agregat `texts[].score`; `null` saat `texts` kosong |
| `texts[]` | array | `{text, score, poly}` PP-OCRv6; boleh kosong |

### `StructuringPayload`

Bentuk K2Regex-v2: datar, 11 field dokumen sebagai kunci tingkat atas + `anggota_keluarga[]` × 15 field
(tiap field `{value, ocr_conf, crf_conf}`) + `reject_reason`. Tidak ada `document_type`, `flag`, atau
`flag_reason`. Bentuk lengkap dan aturannya di [7.3](#73-hasil-tahap-structuring).

### Daftar field

Ada **dua daftar** yang tidak boleh tertukar.

**A. Nama internal** — dipakai structuring dan scoring, tersimpan di `structuring_results` dan
`scoring_results`, terbaca lewat `GET /v1/<tahap>/jobs/{request_id}`:

- Dokumen (11): `nomor_kk`, `nama_kepala_keluarga`, `alamat`, `desa_kelurahan`, `rt`, `rw`, `kecamatan`,
  `kabupaten_kota`, `provinsi`, `kode_pos`, `tanggal_dikeluarkan`.
- Anggota (15): `nama_lengkap`, `nik`, `jenis_kelamin`, `tempat_lahir`, `tanggal_lahir`, `agama`,
  `pendidikan`, `jenis_pekerjaan`, `golongan_darah`, `status_perkawinan`, `tanggal_perkawinan`,
  `status_hubungan_dalam_keluarga`, `kewarganegaraan`, `ayah`, `ibu`.

`no_paspor` dan `no_kitap` sengaja tidak diekstraksi. Scoring hanya menilai sembilan di antaranya —
padanan internal dari daftar B.

**B. Nama kontrak keluar** — hanya muncul di `data` pada `POST` / `GET /v1/extract-ocr`:

- Dokumen (2): `no_kk`, `nama_kepala_keluarga`.
- Anggota (7): `nama_lengkap`, `nik`, `pendidikan`, `jenis_pekerjaan`,
  `status_hubungan_dalam_rumah_tangga`, `ayah`, `ibu`.

Urutan di atas adalah urutan kontrak. Pemetaan A → B ada di [3.3.1](#331-proyeksi-dari-hasil-structuring-ke-data).

---

## 11. Endpoint operasional dan probe

| Service | Method | Path | API key | Keterangan |
|---|---|---|---|---|
| semua | GET | `/health` | tidak | Liveness. Hanya "proses hidup"; tidak menyentuh dependensi. `backends` menunjukkan implementasi aktif, `storage`: `postgres` \| `memory` |
| semua | GET | `/ready` | tidak | Readiness. 200 `{status: ready, checks}` kalau dependensi wajib menjawab (database untuk ketiga tahap), 503 `not_ready` kalau tidak. Tahap berikutnya dan service model sengaja tidak diperiksa |
| semua | GET | `/metrics` | tidak | Prometheus text exposition |
| tiga tahap | GET | `/v1/<tahap>/jobs/{request_id}` | ya | Status dan hasil satu tahap ([9](#9-get-v1tahapjobsrequest_id--dibaca-orchestrator)) |
| tiga tahap | GET | `/v1/<tahap>/outbox` | ya | `{enabled, stage, pending, retrying, oldest_pending_seconds, dead_letters}` |
| tiga tahap | POST | `/v1/<tahap>/outbox/release` | ya | query `request_id` opsional → dead letter diantrekan ulang; 409 kalau outbox mati |
| ekstraksi | POST | `/v1/ekstraksi/extract` | ya | Sinkron, debug (draf 12, seperti nilam `/v1/extraction/extract`). `file` atau `file_url`; menjawab `OcrPayload` 7.1 yang sama dengan hasil job, tanpa menyimpan apa pun, jadi `data`-nya bisa dikirim langsung ke `/v1/ocr_postprocess` |
| structuring | POST | `/v1/ocr_postprocess` | ya | Sinkron, debug. Endpoint `K2Regex-v2` yang ada sekarang, dipertahankan apa adanya (`texts` tetap `min_length=1`) |
| scoring | POST | `/v1/scoring/confidence` | ya | Sinkron, debug. Kalibrasi dan evaluasi trust model ([8.4](#84-endpoint-sinkron-untuk-tim-ml)) |

Metrik: `http_requests_total{service,method,path,status}` dan `http_request_duration_seconds` berlabel
template rute; di tahap pipeline `pipeline_jobs_total{stage,outcome=done|rejected|failed|crashed|interrupted}`,
`pipeline_job_duration_seconds`, `pipeline_stale_jobs_reclaimed_total`,
`pipeline_outbox_deliveries_total{kind,outcome}`, dan gauge backlog `pipeline_outbox_pending`, `_retrying`,
`_dead_letters`, `_oldest_pending_seconds`.

Alert yang disarankan: `pipeline_jobs_total{outcome!="done"}` naik,
`pipeline_outbox_oldest_pending_seconds` melewati `PIPELINE_OUTBOX_STALE_AFTER_SECONDS`, dan
`pipeline_outbox_dead_letters > 0`.

Karena tidak ada gerbang legibilitas, ada baiknya memantau distribusi `ocr_conf` dan `min_doc_score` dari
hasil yang tersimpan: turunnya angka itu adalah satu-satunya tanda awal bahwa mutu foto yang masuk memburuk.

---

## 12. Selisih dengan K2Orchestrator sekarang

Yang harus berubah untuk memenuhi kontrak ini. Tandai sebagai **breaking** untuk klien yang memanggil
`K2Orchestrator` hari ini.

Pemanggilnya juga berpindah: `K2Orchestrator` dipanggil klien aplikasi langsung, sedangkan nlm-k2
dipanggil **Orkestrasi pusat**, yang lalu meneruskan ke BRIspot. Jadi sebagian perubahan "breaking" di
bawah ditanggung Orkestrasi pusat sebagai integrator baru, bukan oleh klien lama yang harus mengubah
kodenya sendiri.

| Aspek | K2Orchestrator sekarang | nlm-k2 (kontrak ini) | Breaking? |
|---|---|---|---|
| Envelope | `{response_code, error_message, request_id, timestamp, data}` | `{status_code, status_desc, message, data, errors, request_id}` | **ya** |
| Pintu masuk | `POST /api/v1/process` | `POST /v1/extract-ocr` | **ya** |
| `request_id` | wajib UUID, diambil dulu lewat `GET /get_request_id`, sekali pakai (409 kalau dipakai ulang) | bebas ≤100 karakter, dibuat pemanggil, **idempoten** (kiriman ulang 202 `duplicate: true`) | **ya** |
| Baca hasil | `GET /api/v1/get_result?request_id=` | `GET /v1/extract-ocr/{request_id}` | **ya** |
| Sifat panggilan | sinkron penuh, satu request menjalankan 7 step sampai selesai (`pipeline_timeout_seconds: 60`) | gerbang sinkron + rantai async; 200 kalau selesai dalam `PIPELINE_WAIT_SECONDS`, 202 kalau belum | **ya** |
| Orientasi & rectifier | dipanggil orchestrator (`ORIENTATION_URL`, `DOC_RECTIFIER_URL`) | **dihapus** — ditangani PaddleOCR di tahap ekstraksi | tidak (internal) |
| PDF | dikonversi orchestrator lewat PyMuPDF | diterima; **hanya halaman 1** yang dinilai dan dibaca, lebih dari `MAX_DOCUMENT_PAGES` → 400 | tidak untuk PDF satu halaman |
| **Crop quality (`K2QualityDL`)** | gerbang per crop, paralel dengan postprocessor; dokumen dengan mayoritas crop buruk ditolak 422 | **dihapus seluruhnya** — tidak ada penilaian legibilitas per field; lihat [1.1](#11-tidak-ada-penilaian-legibilitas-per-field) | **ya** — dokumen buram yang dulu ditolak kini dijawab 200 dengan `confidence` rendah |
| Kurasi crop | extractor KK ke-dua yang di-*vendor* ke orchestrator (1211 baris) | **dihapus** bersama gerbangnya | tidak |
| Gerbang blank | Step 5B di orchestrator, `text_regions_count == 0` → 422 | **pindah ke aturan structuring** → 400 `DOWNSTREAM_VALIDATION_ERROR`; ekstraksi tidak pernah menolak | **ya** (kode status berubah) |
| Gerbang validitas KK | Step 7 di orchestrator, setelah postprocessor → 422 | aturan structuring menulis `reject_reason`; orchestrator menjawab 400 | **ya** |
| Bentuk OCR ke postprocessor | `{ocr_text: [[poly,[text,score]]], processing_time, text_regions_count}` | `{ocr: {texts: [{text, score, poly}], ...}}` — bentuk v6 yang sudah dipakai `K2Regex-v2` | tidak (internal) |
| Bentuk hasil structuring | `data.ocr_result` K2Regex-v2, tiap field `{value, conf}` | struktur sama, tiap field `{value, ocr_conf, crf_conf}`, plus kunci `reject_reason` | **ya** bagi pembaca `conf` |
| `texts` kosong | – | endpoint job **harus** menerimanya (`min_length` 0), supaya gambar kosong sampai ke aturan structuring | tidak |
| Isi `data` | seluruh keluaran postprocessor | **9 field saja**; sisanya tetap diekstraksi dan tersimpan, tapi tidak dikembalikan | **ya** |
| Nama field | `no_kk`, `status_hubungan_dalam_keluarga` | `no_kk` (sama), `status_hubungan_dalam_rumah_tangga` (berubah) | **ya** |
| Confidence | `conf` CRF mentah diteruskan apa adanya | tahap **scoring** mengkalibrasinya; kontrak keluar memakai `confidence` 0/1 | **ya** |
| Penolakan | `422` untuk semua gerbang | `400` + `errors: DOWNSTREAM_VALIDATION_ERROR` + `guardrails: 1` | **ya** |
| Status HTTP downstream mati | `502` untuk semua | `503` tidak terjangkau, `504` timeout, `500` jawaban ≥400 | **ya** |
| Penyimpanan | orchestrator punya DB (`ocr_kk_orchestrator_log`, `..._request`) | orchestrator **stateless**; tiga tahap punya `*_jobs` / `*_results` + `pipeline_outbox`; keadaan akhir juga ditulis ke tabel outcome milik Orkestrasi pusat ([2.6](#26-tabel-outcome-dan-cara-hasil-sampai-ke-pemanggil)) | **ya** |
| Audit PII (Fernet + blind index, fail-closed) | di orchestrator | pindah ke **scoring**, tetap fail-closed | tidak (perilaku dipertahankan) |
| Rate limit, CORS, Elastic APM | di orchestrator | dipertahankan apa adanya | tidak |

**Yang perlu dibuat baru:** service `scoring` (8044), pustaka bersama padanan `ocr_common` (envelope,
request_id, pipeline stage/outbox/repository, klien HTTP), dan tabel `ocr_jobs`/`ocr_results`,
`structuring_jobs`/`_results`, `scoring_jobs`/`_results`, `pipeline_outbox`. Tabel outcome **tidak** dibuat
di sini — ia milik Orkestrasi pusat; yang perlu disepakati dengan mereka adalah nama tabel dan kolomnya
([2.6](#26-tabel-outcome-dan-cara-hasil-sampai-ke-pemanggil)).

**Yang perlu diadaptasi, bukan ditulis ulang:**

- `K2Quality` — bungkus respons `is_bad`/`probability` menjadi `GuardrailsResult`.
- `K2Extractor` — keluarkan bentuk `texts` v6; jadikan tahap async. Buang gerbang blank dan crop quality.
- `K2Regex-v2` — `build_kk_response` memecah `conf` menjadi `ocr_conf` + `crf_conf` dari `_meta` yang sudah
  ada (parser tidak disentuh), dan menambahkan `reject_reason` dari aturan validitas; endpoint job baru
  menerima `texts` kosong; plus pembungkus tahap async dan hook `rejection`.
- `K2Orchestrator` — buang `src/vendor/k2_extractor/`, `src/services/crop_curator.py`, dan
  `src/services/qualitydl_client.py`.

**Yang tidak dipakai lagi:** `K2QualityDL` dan `K2DocRec`.

---

## 13. Environment variables

### 13.1 Orchestrator

| Variabel | Wajib? | Default | Keterangan |
|---|---|---|---|
| `API_KEY` | **ya** | – | kunci yang diterima dari pemanggil, sekaligus kunci keluar ke service lain |
| `API_KEYS` | tidak | kosong | kunci tambahan yang juga diterima (dipisah koma), untuk rotasi tanpa downtime |
| `PORT` | tidak | `8040` | |
| `ENVIRONMENT` | di laptop: `local` | `production` | di luar `local`, service menolak start kalau backend `mock` atau alamat service menunjuk localhost |
| `LOG_FORMAT` | tidak | `json`; `local`: `text` | |
| `LOG_LEVEL` | tidak | `INFO` | |
| `MAX_UPLOAD_BYTES` | tidak | `5242880` | 5 MB; lebih besar → 413 |
| `ALLOWED_CONTENT_TYPES` | tidak | `image/jpeg,image/jpg,image/png,application/pdf` | satu-satunya daftar yang dibaca intake (`PDF_ENABLED` dihapus di draf 12) |
| `MAX_DOCUMENT_PAGES` | tidak | `2` | PDF dengan halaman lebih banyak → 400; hanya halaman 1 yang dibaca |
| `FILE_URL_ALLOWED_HOSTS` | produksi: ya | kosong | host yang boleh diunduh; **kosong = tolak semua**, bukan "hanya alamat publik". **Harus diisi di orchestrator dan ekstraksi**, karena keduanya mengunduh, dan keduanya menolak start tanpa daftar ini di luar `ENVIRONMENT=local` |
| `GUARDRAILS_SERVICE_URL` | ya | – | mis. `http://nlm-k2-guardrails:8041` |
| `GUARDRAILS_API_KEY` | tidak | = `API_KEY` | |
| `GUARDRAILS_TIMEOUT_SECONDS` | tidak | `20` | tanpa retry |
| `EKSTRAKSI_SERVICE_URL` | ya | – | mis. `http://nlm-k2-ekstraksi:8042` |
| `STRUCTURING_SERVICE_URL` | ya | – | mis. `http://nlm-k2-structuring:8043`; dipolling untuk status |
| `SCORING_SERVICE_URL` | ya | – | mis. `http://nlm-k2-scoring:8044`; dipolling untuk status |
| `EKSTRAKSI_API_KEY` / `STRUCTURING_API_KEY` / `SCORING_API_KEY` | tidak | = `API_KEY` | |
| `EKSTRAKSI_TIMEOUT_SECONDS` / `STRUCTURING_...` / `SCORING_...` | tidak | `10` | timeout per panggilan, termasuk tiap GET polling |
| `PIPELINE_WAIT_SECONDS` | tidak | `30` | anggaran total sejak request diterima; `0` = selalu 202 |
| `PIPELINE_POLL_INTERVAL_SECONDS` | tidak | `0.5` | jeda antar polling |
| `PIPELINE_RETRY_ATTEMPTS` / `PIPELINE_RETRY_DELAY_SECONDS` | tidak | `3` / `0.5` | retry `POST /v1/ekstraksi/jobs`, hanya 5xx / tidak terjangkau, backoff ×2 |
| `FIELD_CONFIDENCE_THRESHOLD` | tidak | `0.5` | **cadangan** untuk `auto` di `data`, dipakai hanya bila field itu tidak punya ambang sendiri di hasil scoring. Harus sama dengan nilai di scoring ([8.5](#85-menutup-request)) |
| `DATABASE_URL` | tidak | kosong | hanya untuk `guardrails_results`. Kosong = putusan tidak dicatat, dan `GET` request yang tidak pernah sampai tahap menjadi 404 |
| `GUARDRAILS_LOG_TIMEOUT_SECONDS` | tidak | `2` | batas satu tulis/baca `guardrails_results`; lewat = dicatat di log, request tetap dijawab |
| `RATE_LIMIT_*`, `CORS_*`, `ELASTIC_APM_*` | tidak | – | dipertahankan dari `K2Orchestrator` |

Orchestrator **tidak** memerlukan `DATABASE_URL`: ia membaca status tahap lewat API, bukan lewat database.

### 13.2 Ketiga tahap pipeline (ekstraksi, structuring, scoring)

| Variabel | Wajib? | Default | Keterangan |
|---|---|---|---|
| `DATABASE_URL` | produksi: ya | – | PostgreSQL untuk `*_jobs` / `*_results` / `pipeline_outbox`. Kosong = in-memory: hanya dev satu proses, tidak idempoten antar replika |
| `PIPELINE_JOB_LEASE_SECONDS` | tidak | `300` | job `PROCESSING` yang lebih tua dari ini boleh diklaim ulang. Harus jauh di atas durasi job terlama |
| `PIPELINE_STALE_JOBS` | tidak | `true` | tiap proses mengklaim ulang job basi dan menjalankannya lagi dari `input` + `*_results`. Butuh `DATABASE_URL` |
| `PIPELINE_DRAIN_TIMEOUT_SECONDS` | tidak | `30` | saat shutdown, tunggu job yang masih jalan; sisanya `FAILED` |
| `PIPELINE_HANDOFF_BY_REFERENCE` | tidak | `false` | `true` = handoff tidak membawa blok `ocr` / `structuring`; penerima membacanya dari database. Butuh `DATABASE_URL` yang sama di ketiga service. **Disarankan `true`** untuk KK |
| `PIPELINE_OUTBOX` | tidak | `false` | `true` = handoff ditulis ke `pipeline_outbox` dalam transaksi job, lalu dikirim relay |
| `PIPELINE_OUTBOX_INTERVAL_SECONDS` / `_BATCH` / `_LEASE_SECONDS` | tidak | `1` / `20` / `30` | perilaku relay |
| `PIPELINE_OUTBOX_MAX_BACKOFF_SECONDS` / `_MAX_AGE_SECONDS` | tidak | `300` / `86400` | batas backoff, dan umur sebelum pesan jadi dead letter |
| `PIPELINE_OUTBOX_STALE_AFTER_SECONDS` | tidak | `300` | relay menulis `WARNING` selama pesan tertua lebih tua dari ini |
| `ORCHESTRATION_OUTCOME_TABLE` | **produksi: ya** | kosong | nama tabel outcome milik Orkestrasi pusat ([2.6](#26-tabel-outcome-dan-cara-hasil-sampai-ke-pemanggil)). Kosong = tidak menulis, dan keadaan akhir hanya terbaca lewat polling — hanya untuk dev |
| `FIELD_CONFIDENCE_THRESHOLD` | scoring saja | `0.5` | **cadangan**, dipakai hanya untuk field yang tidak punya ambang sendiri di hasil scoring ([8.3](#83-hasil-tahap-scoring)). Tetap harus sama dengan orchestrator; ambang per field tidak perlu disinkronkan karena ikut di dalam hasil |
| `SCORING_BACKEND` | scoring saja | `mock` | `mock` mengarang angka dan **ditolak di luar `ENVIRONMENT=local`**; `calibrated` memuat artefak terlatih |
| `SCORING_MODEL_PATH` | scoring, `calibrated` | `weights/kk_trust_model.joblib` | artefak joblib: dua keluarga model, masing-masing dengan urutan kolom, tepi bin, dan ambang per field-nya sendiri. Ketiganya bukan konfigurasi — semuanya berpindah bersama bobotnya |
| `PII_ENCRYPTION_KEY` | scoring, produksi: ya | – | Fernet, untuk enkripsi hasil sebelum audit ([8.5](#85-menutup-request)) |
| `TESTING_ENDPOINTS` | tidak | `false` | `true` = orchestrator dan ketiga tahap membuka kembaran `-test` dari endpoint pipeline, memakai tabel `testing_*`, tanpa menulis tabel outcome. Untuk load test tim ML di dev; `false` = route-nya 404 |
| `EKSTRAKSI_BACKEND` | ekstraksi saja | `mock` | `mock` \| `kk_ocr` \| `paddle` \| `remote`. `paddle` = server PaddleOCR tim ML di `POST /ocr` (jalur tetap), `poly` diteruskan utuh |
| `EKSTRAKSI_OCR_URL` / `_API_KEY` / `_TIMEOUT_SECONDS` / `_QUERY` | ekstraksi, `paddle`/`remote` | – / – / `30` / `{}` | alamat server, kunci opsional, timeout, dan parameter query (mis. `{"use_doc_orientation_classify": true}`) |
| `EKSTRAKSI_PDF_DPI` | ekstraksi, `kk_ocr` | `200` | DPI render halaman 1 PDF sebelum deteksi |

### 13.3 Guardrails

| Variabel | Wajib? | Default | Keterangan |
|---|---|---|---|
| `GUARDRAILS_THRESHOLD` | tidak | `0.5` | `is_bad` = `probability_bad >= threshold`. Satu-satunya tuas gerbang mutu gambar di pipeline ini |
| `GUARDRAILS_FETCH_URL` | tidak | `false` | `true` = guardrails mengunduh sendiri dari `file_url` alih-alih menerima byte dari orchestrator |
| `FILE_URL_ALLOWED_HOSTS` | kalau `GUARDRAILS_FETCH_URL=true` | kosong | sama artinya dengan di orchestrator |

---

## Catatan terbuka

1. **Nama dan kolom tabel outcome.** Pemiliknya sudah jelas — Orkestrasi pusat
   ([2.6](#26-tabel-outcome-dan-cara-hasil-sampai-ke-pemanggil)) — tapi nama tabelnya dan bentuk kolomnya
   perlu disepakati dengan tim mereka, termasuk apakah `result_data` cukup memuat sembilan field kontrak
   atau mereka menginginkan lebih.
2. **Ambang guardrails menanggung beban lebih besar.** Tanpa gerbang legibilitas per field,
   `GUARDRAILS_THRESHOLD` (default 0.5) adalah satu-satunya gerbang keras untuk mutu gambar. Ambang itu
   dikalibrasi ketika masih ada gerbang kedua di hilir, jadi perlu ditinjau ulang.
3. ~~**Ambang `FIELD_CONFIDENCE_THRESHOLD` per field.**~~ **Selesai di draf 11.** Dugaannya benar dan
   terukur lebih tajam dari perkiraan: satu ambang global menjatuhkan cakupan pada presisi 100% ke **nol**,
   sementara ambang per field mencapai 26.6%. Ambangnya ikut di dalam hasil scoring
   ([8.3](#83-hasil-tahap-scoring)), **bukan** ditambahkan di kedua service — itulah yang menghilangkan
   keharusan menjaga dua konfigurasi tetap sinkron.
4. **Apakah `ocr_conf` cukup sebagai sinyal legibilitas** ([1.1](#11-tidak-ada-penilaian-legibilitas-per-field)).
   Ini asumsi utama desain sekarang dan perlu divalidasi saat trust model dievaluasi.
5. **Padanan `flag` / `flag_reason`.** nilam memakai keduanya sebagai fitur lunak trust model.
   `K2Regex-v2` belum memproduksinya, jadi trust model KK kehilangan satu kelas sinyal. Layak ditinjau
   apakah aturan KK bisa memancarkan penanda serupa (mis. nama satu kata, NIK tidak lolos cek tanggal
   lahir) tanpa menolak dokumen.
6. **Ukuran payload handoff.** Satu KK bisa menghasilkan 200+ kotak teks.
   `PIPELINE_HANDOFF_BY_REFERENCE` sebaiknya dinyalakan sejak awal.
7. **Callback sebagai pelengkap polling.** nilam menyediakan mode callback (`ORCHESTRATION_URL`) lewat
   outbox yang sama, walau tidak dipakai di sana. Kalau Orkestrasi pusat lebih suka didorong daripada
   memoles tabel, polanya sudah ada dan tinggal ditambahkan.
8. **Nasib `GET /get_request_id`.** `request_id` kini dibuat Orkestrasi pusat, jadi endpoint itu tidak
   diperlukan. Kalau pemanggil lama belum bisa membuatnya sendiri, endpoint itu bisa dipertahankan sebagai
   pembantu (tanpa mekanisme sekali-pakai) selama masa transisi.

---

## Riwayat revisi

### draf 12 — 29 September 2026

Menyelaraskan lima perilaku dengan nilam-ocr-npwp. Alasannya dan keputusan yang dibalik ada di
[`decisions/2026-09-29-selaras-nilam.md`](decisions/2026-09-29-selaras-nilam.md).

1. **`pipeline_name_sequence`** (§3.1, §3.2, §4, §6.1, §9) menggantikan `skip_guardrails` dan gerbang 403
   `GUARDRAILS_SKIP_ALLOWED`. Nama tahap OCR tetap `ekstraksi`. Tambahan di respons: `pipeline_last_stage`;
   kode baru: 422 `INVALID_PIPELINE_SEQUENCE`. **Memecah** klien yang mengirim `skip_guardrails`: field itu
   kini diabaikan dan guardrails tetap berjalan.
2. **`guardrails_results`** (§2.6, §4, §13.1): orchestrator mencatat setiap putusan guardrails, dan `GET`
   menjawab request yang tidak pernah sampai tahap dari tabel itu.
3. **PDF diterima bawaan** (§1, §2.4, §3.1, §12, §13.1), halaman 1 saja; `PDF_ENABLED` dihapus (R34a dibalik).
4. **Backend ekstraksi `paddle`** (§6.3, §13.2): server PaddleOCR `POST /ocr`, `poly` utuh.
5. **`POST /v1/ekstraksi/extract`** (§11): endpoint sinkron yang menjawab `OcrPayload` 7.1 (R17 dibalik).

### draf 11 — 27 September 2026

Trust model yang sungguhan mendarat di tahap scoring, dan mengukur model itu mengubah tiga hal di
kontrak ini. Semuanya berakar pada satu temuan: **`ocr_conf` dan `crf_conf` saja tidak memisahkan nilai
benar dari nilai salah.** Diukur atas 1686 sel berlabel tangan dari 97 dokumen, `crf_conf` mencapai AUC
0.502 — tidak lebih baik dari koin.

1. **`confidence` di `data` jadi float, plus `bin` dan `auto`** (§3.3, §10). Bendera `0 | 1` membuang
   satu-satunya hal yang dibutuhkan untuk triase: field di 0.52 dan field di 0.99 bukan klaim yang sama.
   `auto` mengambil alih peran bendera itu, dan `bin` menempatkan angkanya di sepuluh bin yang teratasnya
   dipotong di titik presisi 100% terukur. **Ini perubahan yang memecah konsumen** yang membaca
   `confidence` sebagai integer.
2. **`StructuredField.features`** (§7.3, §7.3.1). Sinyal yang bekerja sudah dihitung parser lalu dibuang:
   emisi mentah, geometri sel, jarak ke kandidat vocab runner-up, cek silang NIK. Dengan vektor itu AUC
   naik ke 0.855. Vektornya lahir di structuring, bukan dirakit ulang di scoring — fitur yang dihitung di
   dua tempat adalah cara paling andal menghasilkan model yang bagus saat latih lalu buruk saat dipakai.
3. **`thresholds` dan `bin_edges` di hasil scoring** (§8.3), yang menyelesaikan catatan terbuka nomor 3.
   Satu ambang global menjatuhkan cakupan pada presisi 100% ke **nol**; ambang per field mencapai 26.6%.
   Keduanya ikut di dalam hasil, bukan di konfigurasi, karena keduanya properti model yang dilatih — dan
   karena itulah `auto` di respons `extract-ocr` identik dengan `auto` di baris outcome secara konstruksi,
   bukan dengan menjaga dua env var tetap sinkron.

Dua batas yang harus dibaca bersama angka-angka di atas. **Pertama, 100% itu diukur pada held-out, bukan
dijamin**: batas bawah Clopper-Pearson 95% untuk bin teratas adalah 98.51%, dan itu angka yang boleh
dijanjikan. **Kedua, structuring masih backend `mock`**: `features` yang dipancarkannya sintetis — lengkap
dan berbeda per sel, cukup untuk membuktikan vektornya sampai utuh, tapi confidence yang dihitung darinya
tidak mengatakan apa pun tentang sebuah dokumen sampai port K2Regex-v2 mendarat.

### draf 10 — 26 September 2026

Menyerap empat perilaku yang sudah diputuskan pelaksanaannya tetapi dibantah draf 9. Alasan
menyerapnya, bukan sekadar mencatatnya sebagai catatan terbuka: R23 mensyaratkan **satu** sumber
kebenaran kontrak. Dua di antaranya hampir menyebabkan kegagalan yang baru muncul saat integrasi —
agen yang patuh pada draf 9 akan menulis model respons yang menolak `unassessable`, sementara agen
lain mengirimkannya.

1. **Vonis ketiga `unassessable`** (§5.2, §10). Gambar yang tidak bisa dinilai adalah vonis, bukan
   galat, karena §5.2 mewajibkan selalu `200`. `confidence` dan `probability_bad` jadi nullable.
2. **`FILE_URL_ALLOWED_HOSTS` kosong menolak semua** (§3.1, §13.1), bukan "hanya alamat publik".
   Gagal tertutup, dan kedua service yang mengunduh menolak start tanpa daftar itu di luar `local`.
3. **`PDF_ENABLED`** (§3.1, §13.1). Jalur PDF dipertahankan tetapi mati bawaan, dan saklar itu
   satu-satunya yang memasukkan `application/pdf` — di intake, bukan lebih dalam.
4. **Contoh nomor.** Contoh di dokumen ini masih memakai `3273...` (Kota Bandung). Kodenya memakai
   provinsi `99`, yang tidak pernah diberikan Indonesia, karena nomor berbentuk sah dari wilayah nyata
   bisa jadi milik seseorang (`ocr_common/synthetic_kk.py`). **Contoh di sini belum diganti**: mengubah
   setiap nomor di satu dokumen panjang saat tiga agen sedang membacanya adalah diff yang salah waktu.
   Dicatat di sini supaya perbedaannya disengaja, bukan kejutan.

Koreksi kode terhadap kontrak yang ditemukan pada revisi ini, tanpa mengubah kontrak: default
`PIPELINE_WAIT_SECONDS` di kode adalah 15 (terbawa dari struktur yang disalin) sementara §13
mendaftarkan 30. Kodenya yang diperbaiki.

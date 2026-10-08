# Integrasi service OCR Kartu Keluarga (nlm-k2) di GKE

Dokumen untuk tim Orkestrasi/Gateway pusat. Isinya cara memanggil pipeline OCR Kartu Keluarga (KK), apa
yang dikirim balik, dan apa yang kami butuhkan dari kalian. Bentuknya sengaja sama dengan pipeline nilam
(`integration.md` di repo nilam): satu pintu masuk, kontrak `extract-ocr` yang sama,
callback hasil yang sama. Yang berbeda hanya isi `data`, port, dan beberapa aturan khusus KK; perbedaan
itu ditandai **(KK)** di bawah.

Kontrak lengkapnya ada di [`docs/api-contract.md`](docs/api-contract.md) (draf 15); dokumen ini
ringkasannya untuk integrasi.

## 1. Status

Yang sudah terbukti, dan di mana:

- Unit test pustaka dan kelima service, serta uji ujung ke ujung `make smoke` di Docker Compose lokal
  (lihat README). Migrasi database `0001`–`0004` diuji terhadap PostgreSQL (naik, `alembic check`,
  pindah schema beserta barisnya, turun lagi, naik lagi).
- **Belum** diverifikasi oleh dokumen ini: perilaku di cluster setelah perubahan 5 Oktober 2026 (bentuk
  jawaban baru, callback hasil baru, schema `nilam_ocr_kk`), dan callback ke service kalian. Bagian ini
  diperbarui setelah deploy berikutnya.
- **(KK)** Isi `data` belum bermakna untuk dokumen sungguhan sampai port K2Regex-v2 di structuring
  terbukti di cluster; lihat README, bagian Status.

## 2. Akses

| Item | Nilai |
|---|---|
| Namespace | `nlm-k2` |
| Service (ClusterIP) | `nlm-k2`: pintu masuk, hanya port **8040** (orchestrator) |

Satu-satunya alamat yang kalian pakai:

    http://nlm-k2.nlm-k2.svc.cluster.local:8040

Di belakangnya, tiap service punya Deployment + Service sendiri (`nlm-k2-<service>`): orchestrator
(8040), guardrails (8041), extraction (8042), structuring (8043), scoring (8044). Selain orchestrator
semuanya internal.

**Autentikasi.** Semua endpoint kecuali `/health`, `/ready`, `/metrics` memerlukan header `X-API-Key`.
Nilainya kami kirim terpisah, tidak lewat dokumen ini.

**Rate limit (KK).** Orchestrator membatasi `RATE_LIMIT_REQUESTS` (60) per `RATE_LIMIT_WINDOW_SECONDS`
(60) per API key per proses; lewat batas dijawab **429** `TOO_MANY_REQUESTS` dengan header `Retry-After`.

**Dari laptop**, cukup forward orchestrator:

    kubectl -n nlm-k2 port-forward svc/nlm-k2-orchestrator 8040:8040

## 3. Alur

Hanya **satu panggilan** dari sisi kalian. Orchestrator memeriksa file, meminta guardrails menilainya,
lalu menunggu pipeline sampai `PIPELINE_WAIT_SECONDS` (default **30 detik (KK)**, dihitung sejak request
diterima):

    POST :8040/v1/extract-ocr
      file > 5 MB                -> 413  message "Ukuran dokumen melebihi batas 5 MB, ..." (sebelum model)
      PDF > 2 halaman            -> 400  message "Jumlah halaman melebihi batas, ..."    (sebelum model)
      ditolak model guardrails   -> 400  errors = DOWNSTREAM_VALIDATION_ERROR, guardrails = 1
                                         tidak ada yang jalan, tidak ada callback
      ditolak gerbang validitas  -> 400  errors = DOWNSTREAM_VALIDATION_ERROR, guardrails = 1,
        KK (structuring)                 message = alasannya; scoring tidak jalan
      lolos, selesai tepat waktu -> 200  data = {no_kk, nama_kepala_keluarga, anggota_keluarga[]}, guardrails = 0
      lolos, gagal tepat waktu   -> 422  errors = OCR_FAILED | STRUCTURING_FAILED | SCORING_FAILED
      lolos, belum selesai       -> 202  data = null, guardrails = null; hasil menyusul lewat callback

Pasang HTTP timeout panggilan ini di atas `PIPELINE_WAIT_SECONDS`, mis. **45 detik** untuk default 30
detik. `request_id` dibuat oleh kalian dan menjadi kunci di semua tahap.

Hasil sampai ke kalian lewat tiga jalur (boleh lebih dari satu): jawaban sinkron di atas,
`GET :8040/v1/extract-ocr/{request_id}` (bagian 4), dan callback hasil (bagian 6) kalau
`ORCHESTRATION_URL` diisi dan `ORCHESTRATION_CALLBACK_ENABLED` tidak `false`.

## 4. Orchestrator (port 8040): pintu masuk

### POST /v1/extract-ocr

    curl -X POST http://nlm-k2.nlm-k2.svc.cluster.local:8040/v1/extract-ocr \
      -H "X-API-Key: $API_KEY" \
      -F "request_id=REQ_001" \
      -F "file=@kk.jpg"

Bentuk jawaban: envelope standar ditambah `pipeline_last_stage` dan `guardrails`. Keadaan request dibaca
dari kode HTTP (juga di `status_code`): 200 selesai, 202 masih berjalan, 4xx / 5xx gagal atau ditolak.
Sejak 5 Oktober 2026 (seperti nilam sejak 1 Oktober) jawaban tidak lagi membawa `document_type`,
`job_status`, dan `params`, dan field `params` di request dihapus (kalau masih dikirim, diabaikan).

**`pipeline_last_stage`** bernilai `null` pada jawaban sukses (200, 202) dan menyebut service asal
**error**: `guardrails`, `structuring` (penolakan), service yang gagal atau tidak terjangkau, atau
`orchestrator` bila ditolak di pintu masuk sebelum service pipeline mana pun dipanggil.

| Field | Wajib | Keterangan |
|---|---|---|
| `request_id` | ya | dibuat oleh kalian, maks. 100 karakter |
| `file` / `file_url` | salah satu | JPEG, PNG, PDF, maks. **5 MB** (413) dan maks. **2 halaman** (400). **(KK)** Dari PDF hanya halaman 1 yang dinilai dan dibaca. Tipe yang dideklarasikan tapi tidak didukung (`application/octet-stream`, kosong, `jpg`, `text/plain`, ...) dibaca dari tanda tangan berkasnya |
| `pipeline_name_sequence` | tidak | service yang dijalankan, berurutan, dari `guardrails`, `extraction`, `structuring`, `scoring`. Field berulang, string dipisah koma, atau satu string JSON array. Default (atau field kosong): keempatnya |
| `guardrails_confidence_threshold` | tidak | `{"acc_rej": 0.8}`, nilai di antara 0 dan 1. **(KK)** Berlaku pada sisi reject: dokumen ditolak bila `probability_bad` model kualitas ≥ nilainya. **Tidak dikirim: dokumen dianggap lolos** guardrails (tidak ada ambang bawaan), dan `probability_bad`-nya (float) tetap diteruskan ke scoring dan tercatat di `nilam_guardrails_results` |
| `column_confidence_threshold` | tidak | ambang per field kontrak, nilai 0–1, sisi accept. Key `all_field` untuk semua field; key per field (`no_kk`, `nama_kepala_keluarga`, `nama_lengkap`, `nik`, `pendidikan`, `jenis_pekerjaan`, `status_hubungan_dalam_rumah_tangga`, `ayah`, `ibu`) menang atas `all_field`. **Field tanpa ambang (tidak disebut, atau parameter tidak dikirim): `confidence`-nya probabilitas trust model apa adanya (float), bukan 0/1** |

Threshold yang tidak bisa dibaca dijawab **422 `INVALID_THRESHOLD`**; `pipeline_name_sequence` yang
melanggar aturan urutan dijawab **422 `INVALID_PIPELINE_SEQUENCE`**. Keduanya sebelum apa pun dijalankan.

Selesai dalam waktu tunggu, **200**:

    {
      "status_code": 200,
      "status_desc": "OK",
      "message": "OCR extraction completed successfully",
      "data": {
        "no_kk":                {"value": "3273012345678901", "confidence": 1},
        "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "confidence": 1},
        "anggota_keluarga": [
          {
            "nama_lengkap":                       {"value": "BUDI SANTOSO", "confidence": 1},
            "nik":                                {"value": "3273011203850001", "confidence": 1},
            "pendidikan":                         {"value": "S1", "confidence": 1},
            "jenis_pekerjaan":                    {"value": "KARYAWAN SWASTA", "confidence": 1},
            "status_hubungan_dalam_rumah_tangga": {"value": "KEPALA KELUARGA", "confidence": 1},
            "ayah":                               {"value": "SUTRISNO", "confidence": 1},
            "ibu":                                {"value": "SITI AMINAH", "confidence": 0}
          }
        ]
      },
      "errors": null,
      "request_id": "REQ_001",
      "pipeline_last_stage": null,
      "guardrails": 0
    }

- Tiap field selalu `{value, confidence}`; field yang tidak ditemukan `{"value": "", "confidence": 0}`, tidak
  pernah `null`. Kedua kunci dokumen dan ketujuh kunci tiap anggota selalu ada.
- Field yang diberi ambang lewat `column_confidence_threshold` (key-nya sendiri atau `all_field`):
  `confidence` = `1` bila probabilitas trust model bahwa nilainya persis benar mencapai ambang itu, selain itu
  `0`. Field **tanpa ambang**: `confidence` = probabilitas itu sendiri, float 0–1 (mis. `0.9731`); `0` bila
  nilainya kosong. Ambang milik trust model tidak lagi memutuskan.
- `anggota_keluarga` berurutan seperti baris pada kartu.

Belum selesai, **202**:

    {"status_code": 202, "status_desc": "Accepted", "message": "OCR job accepted; still processing",
     "data": null, "errors": null, "request_id": "REQ_001", "pipeline_last_stage": null,
     "guardrails": null}

Ditolak model guardrails atau gerbang validitas KK, **400** (`pipeline_last_stage` `guardrails` atau
`structuring`):

    {"status_code": 400, "status_desc": "Bad Request",
     "message": "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil extraction tidak lengkap",
     "data": null, "errors": "DOWNSTREAM_VALIDATION_ERROR", "request_id": "REQ_001",
     "pipeline_last_stage": "structuring", "guardrails": 1}

**(KK)** Gerbang validitas di structuring punya tiga aturan, yang pertama cocok yang dipakai: tidak ada
teks terbaca sama sekali; nomor KK tidak ada; atau tidak ada anggota dengan NIK dan nama. `message`-nya
berbahasa Indonesia dan boleh ditampilkan langsung ke pengguna.

Satu tahap gagal, **422** (`errors` = `<TAHAP>_FAILED`, `pipeline_last_stage` = tahap itu). Kode error lain
mengikuti tabel §2.4 kontrak.

**Idempoten.** `request_id` yang sama dikirim ulang: guardrails dinilai lagi, tetapi pipeline tidak
menjalankan apa pun dua kali, kecuali percobaan sebelumnya `FAILED` atau melewati lease job
(`PIPELINE_JOB_LEASE_SECONDS`, 5 menit). `request_id` yang sudah selesai dijawab dengan hasil tersimpan.

### GET /v1/extract-ocr/{request_id}

Kontrak yang sama dengan `POST`, tanpa menunggu. Request yang tidak punya job tahap dijawab dari putusan
guardrails terakhirnya (`nilam_guardrails_results`); **404** `REQUEST_ID_NOT_FOUND` kalau tidak ada keduanya.

## 5. Endpoint internal

Kalian tidak memanggilnya; dicantumkan sebagai latar.

| Service | Endpoint | Dipanggil oleh |
|---|---|---|
| guardrails | `POST /v1/guardrails/check` | orchestrator |
| extraction | `POST /v1/extraction/jobs`, `GET .../jobs/{request_id}` | orchestrator |
| extraction | `POST /v1/extraction/extract` | debug, sinkron |
| structuring | `POST /v1/structuring/jobs`, `GET .../jobs/{request_id}` | extraction; orchestrator (status) |
| structuring | `POST /v1/structuring-direct` | QC: structuring saja atas keluaran extraction, tidak dicatat |
| structuring | `POST /v1/ocr_postprocess` | tim ML (K2Regex-v2) |
| scoring | `POST /v1/scoring/jobs`, `GET .../jobs/{request_id}` | structuring; orchestrator (status) |
| scoring | `POST /v1/scoring-direct` | QC: scoring saja atas keluaran structuring, tidak dicatat |
| scoring | `POST /v1/scoring/confidence` | tim ML |

## 6. Callback hasil

Endpoint dari tim Orkestrasi, satu POST per request saat request selesai
(`ORCHESTRATION_CALLBACK_FORMAT=result`), dengan header `X-Callback-Key`. Body dan aturan kirim ulang
mengikuti kontrak kalian "Callback Hasil OCR" (2 Okt 2026) dan jawaban kalian tanggal 5 Okt 2026, sama
dengan nilam. Callback dikirim oleh tahap pipeline yang mengakhiri request, bukan oleh orchestrator, dan
selalu dikirim (tahap tidak tahu apakah orchestrator menjawab 200 atau 202).

Selesai: `result` **sama persis** dengan `data` jawaban 200 `extract-ocr` untuk request yang sama
(sembilan field, `confidence` 0/1 dari threshold request itu atau probabilitasnya bila tanpa threshold; `pipeline_name_sequence` yang berakhir
sebelum scoring: hasil service terakhirnya apa adanya):

    {"request_id": "REQ_001", "status": "completed",
     "result": {"no_kk": {...}, "nama_kepala_keluarga": {...}, "anggota_keluarga": [...]},
     "guardrails": 0}

Ditolak gerbang validitas KK setelah 202:

    {"request_id": "REQ_001", "status": "completed", "result": null, "guardrails": 1,
     "message": "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil extraction tidak lengkap",
     "error_code": "DOWNSTREAM_VALIDATION_ERROR"}

Gagal:

    {"request_id": "REQ_001", "status": "failed", "error_code": "STRUCTURING_FAILED",
     "message": "Internal error in STRUCTURING stage"}

Dokumen yang ditolak model guardrails dan request `[guardrails]` saja tidak mendapat callback: keduanya
dijawab langsung.

**Perubahan untuk kalian (KK):** sebelum 5 Oktober 2026, callback hasil KK membawa probabilitas mentah
trust model sebagai `confidence` dan laporan guardrails sebagai `guardrails`, dan kegagalan/penolakan
memakai `error_message`. Sekarang bentuknya sama dengan nilam seperti di atas.

**Kirim ulang.** 5xx, timeout, dan koneksi gagal dicoba ulang dengan backoff selama
`ORCHESTRATION_CALLBACK_MAX_AGE_SECONDS` (600 detik). `409 RESULT_NOT_READY` dicoba ulang tiap 1,5 detik,
maksimal 5 kali. 4xx lain tidak dikirim ulang.

**Saklar.** `ORCHESTRATION_CALLBACK_ENABLED=false` (Helm `orchestration.callbackEnabled: false`) mematikan
callback walau `ORCHESTRATION_URL` terisi; ikuti mode KK di sisi kalian (di mode poll kalian menjawab
`409 CALLBACK_NOT_EXPECTED` dan membaca `GET /v1/extract-ocr/{request_id}`).

Format lama per tahap (`ORCHESTRATION_CALLBACK_FORMAT=stage`, body `{request_id, stage, status, result,
error_message}`) masih ada dan dijelaskan di webhook `stageCallback` pada `api/gateway.openapi.yaml`.

## 7. Yang kami butuhkan dari kalian

1. **Konfirmasi mode KK** di sisi kalian (callback atau poll), supaya `orchestration.callbackEnabled` kami
   sama.
2. **Ambang guardrails per request.** **(KK)** Endpoint threshold guardrails (`GUARDRAILS_THRESHOLD_URL`)
   dan `GUARDRAILS_THRESHOLD` sudah dihapus: hanya `guardrails_confidence_threshold` di request yang bisa
   menolak dokumen. Kirimkan di setiap request bila dokumen buram harus ditolak; tanpa itu semua dokumen lolos.
3. **Tabel `ocr.orchestration_api_events`.** Tim nilam mematikan double write ke tabel itu pada 5 Oktober
   2026 karena tabelnya dihapus migrasi kalian. Konfirmasi apakah tabel itu masih ada di database KK; kalau
   tidak, kami matikan juga (`ORCHESTRATION_API_EVENTS_TABLE` kosong), karena tulisan itu berada di
   transaksi job dan tabel yang hilang menggagalkan setiap job.

## 8. Database

Pipeline menyimpan job dan hasil tiap tahap ke PostgreSQL `bribrain_ocr_kk` (instans tersendiri, bukan
instans nilam). Sejak migrasi `0003` (5 Oktober 2026) semua tabel kami ada di schema **`nilam_ocr_kk`**
dan namanya berawalan **`nilam_`**; sebelumnya di `public` tanpa awalan.

| Tabel | Isi |
|---|---|
| `nilam_ocr_kk.nilam_ocr_extraction_jobs`, `nilam_ocr_kk.nilam_ocr_extraction_results` | status dan hasil tahap OCR (sebelum migrasi `0004`, 7 Okt 2026: `nilam_ocr_jobs`/`nilam_ocr_results`) |
| `nilam_ocr_kk.nilam_ocr_results` | **hasil final OCR KK** dari orchestrator, satu baris per `request_id`: `status_code`, `status_desc`, `message`, `data`, `errors`, `guardrails`, `created_at`, `updated_at`; diperbarui setiap kali POST/GET `extract-ocr` menjawab (migrasi `0004`) |
| `nilam_ocr_kk.nilam_structuring_jobs`, `nilam_ocr_kk.nilam_structuring_results` | field hasil structuring (2 dokumen + 7 per anggota; baris sebelum 7 Okt 2026: 11 + 15) |
| `nilam_ocr_kk.nilam_scoring_jobs`, `nilam_ocr_kk.nilam_scoring_results` | probabilitas trust model dan keputusan per field (0/1 bila ada ambang, selain itu probabilitasnya) |
| `nilam_ocr_kk.nilam_pipeline_outbox` | callback dan handoff yang belum terkirim |
| `nilam_ocr_kk.nilam_guardrails_results` | setiap putusan guardrails, termasuk yang ditolak |

Integrasi normal tidak perlu menyentuh tabel ini. Query di luar repo ini yang memakai nama lama harus
diganti. Tabel outcome milik kalian (`orchestration_extract_ocr`) tidak berubah. Hak akses dan isi data
sensitifnya: [`db/README.md`](db/README.md).

## 9. Spesifikasi lengkap

`api/gateway.openapi.yaml` berisi hanya yang kalian pakai: `POST` dan `GET /v1/extract-ocr` di
orchestrator, plus webhook callback. Spec per service ada di `services/<nama>/openapi.yaml`, dan Swagger UI
orchestrator di `/docs`:

    kubectl -n nlm-k2 port-forward svc/nlm-k2-orchestrator 8040:8040
    # buka http://127.0.0.1:8040/docs

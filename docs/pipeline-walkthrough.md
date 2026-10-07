# Alur pipeline KK, langkah demi langkah (contoh)

Satu request KK dari ujung ke ujung: apa yang dikirim, apa yang diproses, dan apa yang dijawab tiap
service. Ditulis 7 Oktober 2026, untuk pasangan model `kk_model` (structuring m04) + `kk_field` (scoring s11).

**Sumber contoh.**

- **Structuring dan scoring** adalah **keluaran asli** model yang ter-deploy. Contohnya dijalankan lewat
  `/v1/structuring-direct` dan `/v1/scoring-direct` atas kartu sintetis dari
  `services/structuring/tests/kk_boxes.py` (106 kotak OCR, tanpa PII). Angkanya dibulatkan dan `features`
  dipersingkat.
- **Guardrails dan extraction** memakai format dari [`api-contract.md`](api-contract.md) (§5–§6). Model
  keduanya tidak dijalankan untuk dokumen ini.

Kontrak lengkap setiap langkah ada di [`api-contract.md`](api-contract.md). Dokumen ini hanya contoh yang
mengalir.

```
Client ─► Orchestrator ─► Guardrails (sinkron)
                     └─► Extraction ─► Structuring ─► Scoring   (async, job + hand-off)
Client ◄─ Orchestrator (polling GET /v1/<tahap>/jobs/{id}, maks. 30 dtk)
```

---

## 1. Client → Orchestrator: request

`POST /v1/extract-ocr`, `multipart/form-data`, header `X-API-Key`.

```bash
curl -X POST http://nlm-k2:8040/v1/extract-ocr \
  -H "X-API-Key: $API_KEY" \
  -F "request_id=REQ_DEMO_001" \
  -F "document_type=kk" \
  -F "file=@kk.jpg" \
  -F 'guardrails_confidence_threshold={"acc_rej": 0.5}' \   # opsional
  -F 'column_confidence_threshold={"all_field": 0.9592}'   # opsional
```

Kedua ambang opsional. Tanpa `guardrails_confidence_threshold`, guardrails **meloloskan** dokumen dan hanya
mengembalikan `probability_bad`-nya. Tanpa `column_confidence_threshold` (atau untuk field yang tidak
disebutnya), `confidence` setiap field adalah **probabilitas trust model apa adanya** (float), bukan 0/1.

Di orchestrator: tipe dan ukuran file dicek (JPEG/PNG/PDF, maks. 5 MB, PDF maks. 2 halaman), lalu
`pipeline_name_sequence` dan ambang divalidasi.

## 2. Orchestrator → Guardrails

**Request** `POST /v1/guardrails/check` (multipart): `request_id`, `file` (atau `file_url`), dan
`threshold` + `threshold_target` (opsional; dari `guardrails_confidence_threshold`, sisi `reject`).

**Yang diproses** (`services/guardrails/app/ml/kk_quality.py`):

1. Gambar didekode. Untuk PDF, hanya halaman 1 yang dirender. Gambar yang tidak bisa dibaca → vonis
   `unassessable`.
2. **23 fitur kualitas gambar** dihitung:
   - **19 fitur CV klasik:** `laplacian_var`, `tenengrad`, `rms_contrast`, `mean_brightness`,
     `text_density`, `edge_density`, `fft_moire_energy`, `fft_radial_kurtosis`, `immerkaer_noise`,
     `brightness_bimodality`, `file_kb`, `aspect_ratio`, `skew_angle`, `binarization_quality`,
     `stroke_width_consistency`, `dct_high_low_ratio`, `dct_mid_energy`, `sharpness_per_brightness`,
     `edge_per_contrast`;
   - **2 fitur dari CNN blur** (EfficientNet-B0 per patch): `blur_cnn_raw` dan `blur_cnn_calibrated`
     (isotonic);
   - **2 slot NR-IQA** (`nriqa_topiq`, `nriqa_qualiclip`) yang selalu NaN karena dimatikan.
3. XGBoost menghitung `probability_bad`. Kalau request membawa `threshold` dan nilainya ≥ `threshold`, vonisnya
   `reject`. **Tanpa `threshold`, vonisnya selalu `accepted`** (`threshold_used: null`), dan `probability_bad`
   dikembalikan sebagai float. Tidak ada ambang bawaan (env, endpoint pusat, atau bobot).

**Response** (selalu 200):

```json
{
  "status_code": 200, "status_desc": "OK", "message": "OK", "errors": null, "request_id": "REQ_DEMO_001",
  "data": {
    "passed": true, "reason": null,
    "document": {"verdict": "accepted", "confidence": 0.9713, "probability_bad": 0.0287, "threshold_used": 0.5}
  }
}
```

Kalau `passed: false`, orchestrator langsung menjawab **400** `DOWNSTREAM_VALIDATION_ERROR` dengan `reason`
sebagai `message`, dan pipeline berhenti di sini.

## 3. Orchestrator → Extraction

**Request** `POST /v1/extraction/jobs` (multipart):

```
request_id=REQ_DEMO_001
document_type=kk
guardrails={"passed":true,"reason":null,"document":{...}}   ← blok data guardrails, apa adanya
pipeline_name_sequence=["guardrails","extraction","structuring","scoring"]
file=@kk.jpg            (atau file_url=...)
```

**Response langsung** (202):

```json
{"status_code": 202, "status_desc": "Accepted", "message": "OCR job accepted", "errors": null,
 "request_id": "REQ_DEMO_001",
 "data": {"request_id": "REQ_DEMO_001", "stage": "OCR", "status": "PROCESSING", "duplicate": false}}
```

**Yang diproses di background:** PaddleOCR PP-OCRv6 (deteksi + rekognisi) dijalankan. Hasilnya disimpan
ke `nilam_ocr_extraction_results`, lalu job diteruskan ke structuring. Tahap ini **tidak pernah menolak dokumen**:
gambar kosong menghasilkan `texts: []`, dan penolakannya terjadi di structuring.

**Hasil** (`GET /v1/extraction/jobs/REQ_DEMO_001` → `data.result`):

```json
{
  "engine": "paddle", "model": "PP-OCRv6", "elapsed_ms": 1800.0,
  "text_regions_count": 106, "avg_doc_score": 0.99, "min_doc_score": 0.99,
  "texts": [
    {"text": "KARTU KELUARGA",       "score": 0.99, "poly": [[361,11],[682,11],[682,22],[361,22]]},
    {"text": "No.9924187486671285",  "score": 0.99, "poly": [[348,46],[690,46],[690,57],[348,57]]},
    {"text": "Nama Kepala Keluarga", "score": 0.99, "poly": [[136,71],[253,71],[253,82],[136,82]]},
    {"text": ": BUDI SANTOSO",       "score": 0.99, "poly": [[265,73],[343,73],[343,84],[265,84]]},
    "... 102 kotak lagi (header kolom, isi tabel anggota, dst.)"
  ]
}
```

## 4. Extraction → Structuring

**Request** `POST /v1/structuring/jobs` (JSON):

```json
{
  "request_id": "REQ_DEMO_001",
  "document_type": "kk",
  "guardrails": {"passed": true, "reason": null, "document": {"...": "..."}},
  "ocr": {"engine": "paddle", "text_regions_count": 106, "avg_doc_score": 0.99, "texts": ["...106 kotak..."]},
  "pipeline_name_sequence": ["guardrails", "extraction", "structuring", "scoring"]
}
```

Dengan `PIPELINE_HANDOFF_BY_REFERENCE=true`, blok `ocr` tidak dikirim. Structuring membacanya sendiri dari
`nilam_ocr_extraction_results`. Response langsungnya 202 (`"stage": "STRUCTURING"`).

### Fitur yang diambil model m04

**Tahap A: fitur per kotak OCR** (`services/structuring/app/vendor/kk_model/features.py`). Ukuran halaman
diperkirakan dari bentang poly, dan koordinat dikoreksi kemiringannya dulu.

| Kelompok | Fitur | Jumlah |
|---|---|---|
| Geometri (dinormalisasi W/H) | `x0, x1, y0, y1, cx, cy, w, h, h_rel, aspect` | 10 |
| Teks | `len, ntok, ndig, dig_frac, alpha_frac, upper_frac, colon0, slash, comma, is16, is_dash, starts_digit, score` | 13 |
| Baris | `row_n, row_left, row_right` | 3 |
| Posisi relatif ke header kolom | 9 jangkar (`KARTU KELUARGA`, `NAMA KEPALA KELUARGA`, `NAMA LENGKAP`, `NIK`, `PENDIDIKAN`, `JENIS PEKERJAAN`, `STATUS HUBUNGAN`, `AYAH`, `IBU`) × `found, dxc, dxl, dxr, dy` | 45 |
| Model teks n-gram karakter | probabilitas 10 kelas dari teks kotak (angka diganti `9`) | 10 |

**Tahap B: rantai model.**

- `stage1` (XGBoost) membaca 81 fitur di atas dan menghasilkan P(kelas) per kotak. Kelasnya 9 field
  ditambah `O` (bukan field).
- `ctx1` membaca 81 fitur yang sama ditambah **54 fitur konteks**: probabilitas tetangga
  kiri/kanan/atas/bawah (4 × (10+1)) dan rata-rata probabilitas satu baris (10). Hasilnya P akhir per kotak.

**Tahap C: perakitan field.**

- Kotak dikelompokkan ke anggota berdasarkan baris dan kolom.
- Nilai dibersihkan. Untuk `no_kk` dan `nik`, hasilnya 16 digit.
- Tiga field tertutup (`pendidikan`, `pekerjaan`, `status_hubungan_dalam_keluarga`) disesuaikan ke
  kosakata beku yang ada di artefak.

**Tahap D: 42 fitur per field** untuk s11 (`ocr_common.kk.FIELD_FEATURES`). Ini yang dikirim di
`features`:

| Kelompok | Fitur |
|---|---|
| Struktur (probabilitas kotak) | `lg_conf, lg_struct_min, lg_pk_min, lg_pk_mean, margin_min, n_boxes, filled` |
| OCR | `lg_ocr_min, lg_ocr_mean` |
| Kosakata tertutup | `snapped, sim, raw_in_vocab, sim_gap` |
| Bentuk nilai | `len_norm, digit_frac, alpha_frac, n_tok, fmt16, nik_date_ok, region_match` |
| Konteks dokumen | `n_members, key_coverage, member_rel, member_n_filled` |
| Cek silang | `xname_exact, xname_near, xname_any, nik_dup, nik_gender, nik_tail0` |
| Teks OCR mentah | `n_weird, has_lower, has_label, digit_in_name, last_tok_len` |
| Geometri | `cpw_rel, cpw_dev, cand_pk_max, cand_n, edge_gap` |
| Kualitas OCR dokumen | `doc_ocr_mean, doc_ocr_p10` |

**Tahap E: gerbang validitas KK.** `reject_reason` diisi kalau tidak ada teks sama sekali, kalau
`nomor_kk` kosong, atau kalau tidak ada anggota yang `nik` **dan** `nama_lengkap`-nya terisi.

### Hasil structuring

Diambil dari `GET /v1/structuring/jobs/REQ_DEMO_001` → `data.result`, atau dari `data` milik
`/v1/structuring-direct`. Ini **keluaran asli** model:

```json
{
  "nomor_kk": {
    "value": "9924187486671285", "ocr_conf": 0.99, "crf_conf": null,
    "features": {"lg_conf": 8.517, "lg_struct_min": 8.535, "margin_min": 0.9997, "n_boxes": 1.0,
                 "fmt16": 1.0, "digit_frac": 1.0, "n_members": 2.0, "doc_ocr_mean": 0.99, "...": "42 total"}
  },
  "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "ocr_conf": 0.99, "crf_conf": null, "features": {"...": "42"}},
  "anggota_keluarga": [
    {
      "nama_lengkap":   {"value": "BUDI SANTOSO",     "ocr_conf": 0.99, "crf_conf": 0.9999, "features": {"...": "42"}},
      "nik":            {"value": "9908680101601956", "ocr_conf": 0.99, "crf_conf": 0.9999, "features": {"...": "42"}},
      "pendidikan":     {"value": "SLTA/SEDERAJAT",   "ocr_conf": 0.99, "crf_conf": 0.9999,
                         "features": {"snapped": 1.0, "sim": 0.833, "raw_in_vocab": 0.0, "...": "42"}},
      "jenis_pekerjaan":                {"value": "KARYAWAN SWASTA", "ocr_conf": 0.99, "crf_conf": 1.0,    "features": {"...": "42"}},
      "status_hubungan_dalam_keluarga": {"value": "KEPALA KELUARGA", "ocr_conf": 0.99, "crf_conf": 0.9995, "features": {"...": "42"}},
      "ayah": {"value": "RIZKY SANTOSO", "ocr_conf": 0.99, "crf_conf": 0.9999, "features": {"...": "42"}},
      "ibu":  {"value": "NURUL PRATAMA", "ocr_conf": 0.99, "crf_conf": 0.9998, "features": {"...": "42"}}
    },
    {
      "nama_lengkap":   {"value": "SITI SANTOSO", "...": "..."},
      "nik":            {"value": "9908114806713444", "...": "..."},
      "pendidikan":     {"value": "", "ocr_conf": null, "crf_conf": null, "features": null},
      "jenis_pekerjaan": {"value": "PELAJAR/MAHASISWA", "...": "..."},
      "status_hubungan_dalam_keluarga": {"value": "ISTRI", "...": "..."},
      "ayah": {"value": "INDAH SANTOSO", "...": "..."},
      "ibu":  {"value": "HENDRA PRATAMA", "...": "..."}
    }
  ],
  "reject_reason": null
}
```

Kalau `reject_reason` terisi, pipeline berhenti di sini dan klien menerima 400.

## 5. Structuring → Scoring

**Request** `POST /v1/scoring/jobs` (JSON):

```json
{
  "request_id": "REQ_DEMO_001",
  "document_type": "kk",
  "guardrails":  {"passed": true, "document": {"probability_bad": 0.0287, "verdict": "accepted", "...": "..."}},
  "ocr":         {"text_regions_count": 106, "avg_doc_score": 0.99, "min_doc_score": 0.99, "...": "..."},
  "structuring": {"...objek hasil structuring di atas, apa adanya..."}
}
```

Dengan `PIPELINE_HANDOFF_BY_REFERENCE=true`, `ocr` dan `structuring` tidak dikirim dan scoring membacanya
dari database. Response langsungnya 202 (`"stage": "SCORING"`).

### Fitur yang dibutuhkan s11

Untuk setiap field yang terisi, s11 menyusun satu baris input (`services/scoring/app/ml/kk_field.py`):

| Masukan | Sumber |
|---|---|
| 42 fitur `FIELD_FEATURES` | `structuring.<field>.features` |
| One-hot nama field (9) + interaksi field × fitur | dari nama field |
| Logit model teks n-gram atas `"<field[:4]>\|<value>"` | `structuring.<field>.value` |

Dua blend (Logistic Regression dan HistGradientBoosting) dirata-ratakan, hasilnya P(field benar).

Yang **tidak** dipakai model: `ocr_conf`, `crf_conf`, `guardrails`, dan agregat `ocr`. Keduanya yang
terakhir hanya disimpan di `payload` sebagai jejak audit. Field dengan `value` kosong, atau `features` yang
tidak lengkap, diberi skor `null`.

### Hasil scoring (keluaran asli)

```json
{
  "document_type": "kk",
  "model": "kk-trust-s11_blend_lr_hgb",
  "fields": {"nomor_kk": 0.9866, "nama_kepala_keluarga": 0.99},
  "anggota_keluarga": [
    {"nama_lengkap": 0.9658, "nik": 0.9049, "pendidikan": 0.1085, "jenis_pekerjaan": 0.9731,
     "status_hubungan_dalam_keluarga": 0.9812, "ayah": 0.9377, "ibu": 0.8796},
    {"nama_lengkap": 0.9395, "nik": 0.9423, "pendidikan": null, "jenis_pekerjaan": 0.9662,
     "status_hubungan_dalam_keluarga": 0.9904, "ayah": 0.9134, "ibu": 0.3465}
  ],
  "thresholds": {"nomor_kk": 0.9592, "nik": 0.9592, "...": "0.9592 untuk kesembilan field"},
  "bin_edges": null,
  "decisions": {
    "no_kk":                {"value": "9924187486671285", "confidence": 1, "threshold": 0.9592},
    "nama_kepala_keluarga": {"value": "BUDI SANTOSO",     "confidence": 1, "threshold": 0.9592},
    "anggota_keluarga": [
      {"nama_lengkap": {"value": "BUDI SANTOSO", "confidence": 1, "threshold": 0.9592},
       "nik":          {"value": "9908680101601956", "confidence": 0, "threshold": 0.9592},
       "pendidikan":   {"value": "SLTA/SEDERAJAT", "confidence": 0, "threshold": 0.9592},
       "...": "..."}
    ]
  },
  "payload": {"structuring": {"...": "..."}, "guardrail_probability": 0.0287, "guardrail_verdict": "accepted",
              "avg_doc_score": 0.99, "min_doc_score": 0.99, "text_regions_count": 106}
}
```

`decisions` berisi keputusan per field. Ambangnya hanya dari `column_confidence_threshold` di request (di sini
`all_field` 0,9592): `confidence` = 1 kalau P ≥ ambang, selain itu 0. Field tanpa ambang mendapat
`"threshold": null` dan `confidence` = P itu sendiri (mis. `"nik": {"confidence": 0.9049, "threshold": null}`).
`thresholds` milik model tetap disimpan sebagai rujukan, tetapi tidak memutuskan. Nama field di `decisions`
sudah memakai **nama kontrak**.

## 6. Orchestrator → Client: response akhir

Orchestrator memproyeksikan `decisions` ke 9 field kontrak, lalu menjawab 200:

```json
{
  "status_code": 200, "status_desc": "OK", "message": "OCR extraction completed successfully",
  "data": {
    "no_kk":                {"value": "9924187486671285", "confidence": 1},
    "nama_kepala_keluarga": {"value": "BUDI SANTOSO",     "confidence": 1},
    "anggota_keluarga": [
      {"nama_lengkap": {"value": "BUDI SANTOSO", "confidence": 1},
       "nik":          {"value": "9908680101601956", "confidence": 0},
       "pendidikan":   {"value": "SLTA/SEDERAJAT", "confidence": 0},
       "jenis_pekerjaan": {"value": "KARYAWAN SWASTA", "confidence": 1},
       "status_hubungan_dalam_rumah_tangga": {"value": "KEPALA KELUARGA", "confidence": 1},
       "ayah": {"value": "RIZKY SANTOSO", "confidence": 0},
       "ibu":  {"value": "NURUL PRATAMA", "confidence": 0}},
      {"nama_lengkap": {"value": "SITI SANTOSO", "confidence": 0},
       "pendidikan":   {"value": "", "confidence": 0},
       "...": "..."}
    ]
  },
  "errors": null, "request_id": "REQ_DEMO_001", "pipeline_last_stage": null, "guardrails": 0
}
```

Dua nama berganti di sini: `nomor_kk` menjadi `no_kk`, dan `status_hubungan_dalam_keluarga` menjadi
`status_hubungan_dalam_rumah_tangga`.

Setiap envelope yang dijawab orchestrator (202, lalu 200/400/422) juga di-upsert ke
**`nilam_ocr_kk.nilam_ocr_results`**: satu baris per `request_id` dengan `status_code`, `status_desc`,
`message`, `data`, `errors`, `guardrails`, `created_at`, `updated_at`.

---

## Yang terlihat dari contoh nyata ini

- **Model menyesuaikan `pendidikan` ke nilai yang salah.** Kartu mencetak `SD/SEDERAJAT`, tapi structuring
  mengeluarkan `SLTA/SEDERAJAT`. Ini terjadi karena `SD/SEDERAJAT` tidak ada di kosakata beku (yang ada
  `TAMAT SD/SEDERAJAT`), dan ambang kemiripan 0,5 terlalu longgar. **s11 menangkapnya**: skornya 0,1085,
  sehingga `confidence: 0`. Pada kartu asli, yang mencetak `TAMAT SD/SEDERAJAT`, kasus ini kemungkinan tidak
  terjadi. Kartu sintetisnya memang memakai ejaan pendek.
- **Kosakata membawa salah ketik dari GT.** Isinya antara lain `SLTA/SEDERAAT`, `SLTASEDERAAT` dan
  `DIPLOMA IV/STRARA I`. Akibatnya output bisa berupa nilai baku yang salah eja.
- **`D-III` di anggota 2 tidak terbaca sama sekali.** `pendidikan` anggota 2 kosong, sehingga skornya `null`
  dan `confidence`-nya 0.

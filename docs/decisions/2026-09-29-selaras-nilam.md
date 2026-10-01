# Menyelaraskan lima perilaku dengan nilam-ocr-npwp

Tanggal: 29 September 2026 · kontrak draf 12

nlm-k2 disalin dari nilam-ocr-npwp lalu menyimpang. Lima kemampuan nilam hilang atau dimatikan di
tengah jalan, sebagian dengan alasan tertulis (R17, R34a). Keputusan ini membawa kelimanya kembali,
supaya Orkestrasi pusat bisa memanggil kedua pipeline dengan cara yang sama. Dokumen ini mencatat apa
yang dibalik, apa yang **sengaja tidak** disalin dari nilam, dan kenapa.

Ada juga satu bug yang ikut ditutup: `deploy/helm/nlm-k2/values.yaml` sudah memakai
`EXTRACTION_BACKEND: paddle`, padahal backend itu tidak terdaftar. Pod extraction menolak start dengan
nilai bawaan chart-nya sendiri.

## Yang dibalik

| Perilaku | Sebelumnya | Sekarang | Keputusan yang dibalik |
|---|---|---|---|
| Memilih service per request | `skip_guardrails` + gerbang 403 `GUARDRAILS_SKIP_ALLOWED` | `pipeline_name_sequence`, tanpa gerbang (nilam `7c69a19`) | — |
| Jejak putusan guardrails | tidak ada; `GET` request yang ditolak = 404 | `guardrails_results`; `GET` menjawab dari putusan terakhir | "orchestrator stateless tanpa tabel" (§2.6) |
| PDF | 400 kecuali `PDF_ENABLED` | diterima bawaan, **halaman 1 saja** | R34a |
| Backend OCR `paddle` | tidak terdaftar | server PaddleOCR `POST /ocr` | "`kk_ocr` menggantikan `paddle.py`" (plan, `2b2c2be`) |
| OCR sinkron | tidak ada | `POST /v1/extraction/extract` | R17 |

## Yang sengaja tidak disalin dari nilam

- **Nama tahap OCR di sequence kini `extraction`** (sebelumnya `extraction`), sama dengan nilam. Nama service
  repo ini (URL, env, helm) tetap `extraction`; hanya nilai di `pipeline_name_sequence` dan
  `pipeline_last_stage` yang `extraction`. Konsekuensinya:
  **Orkestrasi pusat mengirim nilai berbeda** untuk KK dan NPWP.
- **`poly` tidak dijadikan `bbox` tegak.** `paddle.py` nilam membuang kuadrilateralnya; parser tata letak
  KK mengukur kemiringan dari sana (`estimate_shear`). Backend `paddle` di sini adalah `remote` dengan
  jalur `/ocr` tetap, dan memakai parser §7.1 yang sudah ada.
- **Respons endpoint sinkron adalah `OcrPayload` §7.1**, bukan `blocks` / `full_text` nilam. Satu bentuk
  OCR untuk seluruh repo, dan `data`-nya bisa dikirim langsung ke `/v1/ocr_postprocess`.
- **PDF multi-halaman tidak dibaca per halaman.** nilam menjalankan aturan per halaman. Di sini parser
  membaca satu bingkai halaman, dan kotak dari halaman 2 akan jatuh ke koordinat halaman 1 lalu merusak
  zonasi. Guardrails dan extraction sama-sama membaca halaman 1. `MAX_DOCUMENT_PAGES=2` tetap seperti
  nilam, jadi halaman 2 (mis. legalisir) diterima tetapi diabaikan.
- **Ambang guardrails per request** (`guardrails_confidence_threshold`, `threshold_source=request`) dan
  `column_confidence_threshold` belum dibawa. Kolomnya ada di `guardrails_results`, tetapi
  `threshold_source` selalu `service` dan `threshold_target` selalu null.
- **Migrasi 0002 memakai `CREATE ... IF NOT EXISTS`**, seperti baseline dan tidak seperti 0008 nilam, supaya
  langkah CI "adopt a database that already has the tables" tetap lolos.

## Konsekuensi yang harus diketahui

- **`skip_guardrails` kini diabaikan diam-diam.** Klien yang mengirimnya akan mendapati guardrails tetap
  berjalan. Ganti dengan `pipeline_name_sequence` tanpa `guardrails`.
- **Tidak ada lagi saklar yang bisa menolak request yang melewati guardrails.** Itu keputusan Orkestrasi
  pusat, seperti di nilam. Cek file dan aturan structuring tetap berjalan.
- **Orchestrator kini butuh `DATABASE_URL`** untuk mencatat putusan (helm `database: true`, compose).
  Tanpa itu ia tetap berjalan; putusan tidak dicatat dan `GET` request yang tidak pernah sampai tahap
  menjadi 404 seperti sebelumnya. Hibah R28-nya: `SELECT, INSERT` pada `guardrails_results` saja.
- **DPI render PDF untuk `kk_ocr` (`EXTRACTION_PDF_DPI=200`) belum diukur** terhadap baseline korpus. 150
  milik guardrails adalah DPI pelatihannya dan tidak dipakai ulang di sini.
- **Lock extraction hanya ditambah entri `pymupdf`.** Menjalankan `make lock-extraction` penuh di mesin ini
  juga menurunkan `certifi` dan `idna` ke build lama dari indeks PyTorch, **bahkan tanpa perubahan apa
  pun** — lock yang ada tidak bisa direproduksi di sini. Hash `pymupdf` identik dengan lock guardrails.

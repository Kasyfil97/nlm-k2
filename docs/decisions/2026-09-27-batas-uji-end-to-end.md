# Sejauh mana "end-to-end" yang sudah diuji batch ini

Tanggal: 27 September 2026 · setelah Unit 10 dan uji terhadap VM PP-OCRv6

`make smoke` hijau pada delapan jalur, dan pipeline lima service berjalan di compose dengan
PostgreSQL sungguhan. Dokumen ini ada supaya angka itu tidak dibaca lebih besar dari artinya.

## Yang terbukti, dan yang tidak

| Lapisan | Terbukti? | Dengan apa |
|---|---|---|
| Intake orchestrator, §3.2 matriks, 200/202/400/422 | **ya** | delapan jalur `make smoke` |
| Panggilan guardrails sinkron, penerusan laporannya | **ya** | jalur penolakan, dan `guardrails` diteruskan utuh ke ekstraksi |
| Siklus job: klaim, lease, handoff, outbox, dead letter, release | **ya** | dijalankan sungguhan: dead letter → `failed`/`STRUCTURING_FAILED` → release → `completed` |
| Tabel outcome sebagai kanal ke Orkestrasi pusat | **ya** | 7 dari 8 request menulis baris; yang ke-8 penolakan guardrails, yang memang tidak menulis (§2.6) |
| Korelasi `request_id`, amplop bersama, metrik, log tanpa PII | **ya** | uji per service (R30) + `X-Request-ID` lintas tahap |
| **Model OCR sungguhan** | **ya** | VM PP-OCRv6, bentuk §7.1, `poly` 4 titik, agregat dihitung `ocr_aggregates` |
| **Penilaian mutu gambar** | **tidak** | guardrails `mock` menolak berdasarkan NAMA BERKAS, bukan isi gambar |
| **Ekstraksi field dari teks** | **tidak** | structuring `mock` **mengarang** field; ia tidak membaca teks OCR sama sekali |
| **Trust model per field** | **tidak** | scoring `mock` menurunkan skor dari skor structuring, bukan dari model terkalibrasi |

## Buktinya, bukan klaimnya

Tiga gambar berbeda dikirim lewat `POST /v1/extract-ocr` dengan ekstraksi memakai model sungguhan:

| Gambar | Yang dibaca model OCR | Yang dikembalikan `data.no_kk` |
|---|---|---|
| kartu seed 0 | `No.9924187486671285` · `BUDI SANTOSO` | `9924187486671285` |
| kartu seed 7 | `No.9911487799978005` · `NURUL KUSUMA` | `9924187486671285` |
| **Surat Keterangan Domisili Usaha** | `SURAT KETERANGAN DOMISILI USAHA` | `9924187486671285` |

`data` ketiganya **identik byte demi byte**. Model OCR membacanya dengan benar dan berbeda-beda;
structuring mengabaikan seluruh teks itu dan mengeluarkan satu keluarga sintetis yang sama.

Dua konsekuensi yang harus dibaca terang-terangan:

1. **Tidak ada satu pun field di `data` yang pernah diekstraksi dari sebuah gambar.** Kecocokan pada
   baris pertama tabel di atas adalah **kebetulan yang terbangun sendiri**: gambar ujinya digambar
   dari `ocr_common.synthetic_kk` dengan seed bawaan, dan structuring mock memanggil generator yang
   sama dengan seed bawaan yang sama. Bukan hasil parsing.
2. **Dokumen yang bukan Kartu Keluarga dijawab `200` dengan KK yang sempurna.** Gerbang keabsahan
   §7.4 tidak menyala, karena ia memeriksa field yang dikarang mock itu sendiri — bukan teks OCR. Jadi
   gerbangnya **ada secara struktural tetapi mati secara semantik** terhadap masukan sungguhan.

Butir kedua tidak terlihat dari `make smoke` yang hijau, dan itu justru intinya: setiap jalur smoke
menggerakkan mock lewat tuasnya sendiri (`blank`, `MOCK:members=0`, `servererror`), jadi yang teruji
adalah bahwa **pipa meneruskan penolakan dengan benar**, bukan bahwa **ada yang bisa mengenali KK**.

## Kenapa begini, dan kenapa itu tidak salah

Ini pilihan yang diambil sadar di awal batch: structuring dan scoring dikerjakan sebagai **stub
tipis** supaya siklus 202→200 bisa diuji ujung ke ujung lebih dulu, sebelum port K2Regex-v2 dan trust
model terkalibrasi ada. R20a menyatakan batch selesai dengan backend `mock` dan backend asli tidak
memblokir.

Yang salah hanyalah kalau hijaunya smoke test dibaca sebagai "pipeline sudah bisa membaca KK". Ia
membuktikan hal lain, dan hal lain itu memang yang paling mahal diperbaiki nanti kalau salah:
transaksi, idempotensi, lease, outbox, dead letter, dan bentuk kontrak.

## Apa yang akan menutup celah ini

1. **Port K2Regex-v2 ke `services/structuring`** — ini satu-satunya yang membuat `data` berasal dari
   gambar. Sampai itu ada, §7.4 tidak bisa diuji terhadap dokumen sungguhan.

   **Yang dibutuhkan bukan service-nya, melainkan satu berkas di dalamnya.** Kelima service nlm-k2
   sudah lengkap dan jalan; yang kosong adalah *backend* di salah satunya. Bandingkan isi `app/ml/`:

   | Service | soket (`base.py`) | `mock.py` | backend asli |
   |---|---|---|---|
   | guardrails | 78 | 33 | `kk_quality.py` **761** (inti K2Quality; bobotnya belum ada) |
   | ekstraksi | 53 | 131 | `kk_ocr.py` **215** + `remote.py` **129** — `remote` sudah jalan ke VM |
   | structuring | 22 | 117 | **tidak ada** |
   | scoring | 16 | 62 | **tidak ada** |

   Otak yang hilang itu adalah `K2Regex-v2/src/services/kk_layout_parser.py`, 1.638 baris: zoning
   lewat marker `(1)..(17)`, deteksi batas kolom dengan pencocokan header fuzzy lalu snap ke koridor
   kosong, anchoring baris lewat nomor urut, normalisasi kosakata tertutup ke nilai kanonik Dukcapil,
   dan validasi silang NIK terhadap tanggal lahir serta jenis kelamin. Tidak ada satu pun dari itu
   yang bisa disimpulkan dari bentuk kontrak — ia harus diambil.

   **Bentuk beku §7.3 di repo ini memang dipotong menurut keluaran parser itu**, dan itu bisa
   diperiksa: **11 dari 11** `DOC_FIELDS` dan **15 dari 15** `MEMBER_FIELDS` ada dengan nama yang
   sama persis di parser (yang punya dua lebih, `no_paspor` dan `no_kitap`, sengaja tidak diambil),
   dan `crf_conf` — skor kedua di `StructuredField` — berasal dari sana. Soketnya sudah dibentuk
   untuk plug ini; yang belum ada hanya plug-nya.

   Preseden persisnya guardrails sebelum Unit 8: service-nya sudah ada, agen B memasukkan inti
   K2Quality ke dalamnya sebagai satu berkas dan membuang routes, database, dan config-nya. Tidak ada
   service yang ditambah. `structuring` hari ini berada di posisi guardrails saat itu.

   Catatan: berkas parser di K2Regex-v2 sendiri adalah *vendored snapshot* dari sebuah prototipe
   (`docs/brainstorms/2026-09-22-kk-full-schema-parser.py`, dipaku lewat SHA-256), jadi sumber
   sebenarnya adalah prototipe itu — yang justru memperkuat bahwa yang diambil adalah logikanya.
2. **Inti K2Quality dengan enam artefak bobotnya** di guardrails, supaya penolakan mutu berdasar
   gambar, bukan nama berkas. Kodenya sudah ada (`app/ml/kk_quality.py`); bobotnya belum.
3. **Trust model terkalibrasi** di scoring.
4. Satu uji yang **hanya bisa ditulis setelah (1)**: dokumen bukan-KK harus dijawab `400`, dan kartu
   yang berbeda harus menghasilkan `data` yang berbeda. Keduanya akan gagal hari ini, dan itu sebabnya
   keduanya belum ditulis sebagai uji — bukan karena terlupa.

# Keputusan fase 0 yang dibekukan gerbang R6

Tanggal: 26 September 2026 · Unit 4

Gerbang R6 butir ketiga menuntut keputusan ini **tertulis** sebelum `db/**` dan `libs/ocr_common/**`
dibekukan, karena R5 (isi baseline) dan R22 (pembekuan) mengunci hasilnya. Setiap keputusan di sini
menyebutkan apa yang dipilih, apa yang ditolak, dan apa konsekuensinya kalau ternyata keliru.

---

## R34 — subsistem warisan tanpa padanan di kontrak

### 1. Tabel `testing_*` dan kembaran endpoint `-test` — **dipertahankan**

Kontrak mendaftarkannya (§11, §13), dan R23 menjadikan kontrak satu-satunya sumber kebenaran, jadi
membuangnya adalah perubahan kontrak, bukan pilihan implementasi. Biayanya juga nol: `repo_metadata()`
sudah membangun kedua jalur, dan baseline hanya mengulang templat yang sama dengan prefiks.

Konsekuensi yang dicatat: tabel ini **salinan persis** tabel tahap, jadi ia mewarisi setiap butir
matriks hak R28 — termasuk yang memuat NIK. R8a sudah menolak `TESTING_ENDPOINTS=true` di luar
`ENVIRONMENT=local`, jadi permukaan rutenya tidak pernah ada di produksi; tabelnya tetap ada.

### 2. Jalur callback (`ORCHESTRATION_URL`, `ResultCallback`) — **dipertahankan, mati bawaan**

Kontrak §2.6 hanya menjelaskan polling dan tabel outcome, jadi callback bukan kanal yang dijanjikan.
Tetapi tanpa `ORCHESTRATION_URL`, `build_callback` mengembalikan klien `None` dan setiap panggilan
dilewati — ia sudah inert. Membuangnya berarti menyunting kode beku tanpa manfaat.

Kalau Orkestrasi pusat nanti lebih suka didorong daripada memoles tabel, saklarnya sudah ada.

### 3. `ocr.orchestration_api_events` — **dipertahankan, mati bawaan**

Sama seperti di atas: `ORCHESTRATION_API_EVENTS_TABLE` kosong berarti tidak ada yang ditulis. Tabelnya
milik Orkestrasi pusat dan **tidak** dibuat migrasi kita; tiruannya ada di `db/external/` supaya
`make db-external` tetap menyiapkan alurnya secara lokal.

### 4. Rate limit, CORS, dan Elastic APM — **TIDAK ADA, dan itu janji kontrak yang belum dipenuhi**

Ini bukan "pertahankan apa adanya", karena tidak ada yang bisa dipertahankan. Diperiksa:

| Di mana | Rate limit | CORS | Elastic APM |
|---|---|---|---|
| `K2Orchestrator` | ada — `src/middleware/rate_limiter.py`, sliding window, 230+ baris | ada — `allow_origins` di `src/core/config.py` | ada — `ElasticApmConfig` |
| `nilam-ocr-npwp`, karenanya salinan kita | **tidak ada** | **tidak ada** | **tidak ada** |

Kontrak §12 mendaftarkan ketiganya sebagai "di orchestrator · dipertahankan apa adanya · tidak
[berubah]". Artinya kontrak mengharapkannya ada. Batch ini, sebagaimana direncanakan, tidak
mengirimkannya — dan itu akan muncul saat deploy, bukan saat pengembangan.

Keputusan, terbelah karena risikonya berbeda:

- **Rate limit dan CORS: masuk lingkup agen A (Unit 7).** Keduanya kecil, keduanya proteksi nyata,
  dan orchestrator adalah satu-satunya service yang terekspos ke luar.
- **Elastic APM: ditunda, dengan syarat.** Agen APM menangkap badan request dan konteks span secara
  bawaan — di orchestrator itu berarti unggahan kartu dan JSON hilir berisi 26 field, terkirim ke
  kolektor pihak ketiga yang retensi dan daftar pembacanya berbeda dari database maupun log sink.
  Ia baru boleh masuk setelah penangkapan badan dimatikan dan retensi kolektornya disepakati.
  Mengirimnya salah lebih buruk daripada mengirimnya belakangan.

---

## R27 — audit PII §8.5: bentuk diputuskan, tabelnya **tidak** dibuat sekarang

Dua reviewer bertentangan di sini, dan keduanya benar sebagian.

- Security-lens: putuskan sekarang, karena `db/**` beku dan menambahkannya belakangan mahal.
- Scope-guardian: kolom yang ditebak sebelum kode auditnya ada lebih mungkin salah daripada tidak
  ada, dan tabel beku yang salah lebih mahal daripada tabel yang belum ada.

Sintesisnya: **memutuskan bentuknya di atas kertas tidak berbiaya dan berguna; membuat tabelnya di
baseline tidak menghasilkan apa pun** selain tabel kosong di produksi. Kalau bentuknya ternyata
keliru, mengubahnya butuh migrasi — persis biaya yang sama dengan menambahkannya belakangan. Jadi
tebakannya tidak dibekukan.

Bentuk yang dicatat sebagai titik awal batch yang mengimplementasikannya:

```
kk_pii_audit
  id                    BIGSERIAL PRIMARY KEY
  request_id            TEXT NOT NULL
  nomor_kk_blind_index  TEXT NOT NULL   -- HMAC atas nomor_kk yang sudah dinormalkan; memungkinkan
                                        -- "cari semua request untuk KK ini" tanpa kolom plaintext
  payload_encrypted     BYTEA NOT NULL  -- Fernet atas result_data kontrak
  key_version           TEXT NOT NULL   -- kunci mana yang mengenkripsi, supaya rotasi mungkin
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
  ds                    TEXT NOT NULL
  INDEX (nomor_kk_blind_index), INDEX (ds)
```

**Akibatnya R5 dipersempit**: "keadaan akhir skema" berarti keadaan akhir **tabel pipeline**, bukan
seluruh skema yang akan pernah ada. Penundaan ini tetap tidak bisa diam-diam sampai ke produksi:
`PII_AUDIT_IMPLEMENTED` bawaannya `false` dan scoring menolak start di luar `ENVIRONMENT=local`
selama itu.

---

## R28 — matriks hak dua sisi dan retensi

Ditulis lengkap di [`db/README.md`](../../db/README.md). Intinya: hibah dibatasi **di kedua arah**,
bukan hanya untuk service nlm-k2. Peran Orkestrasi pusat tidak mendapat `SELECT` pada tabel tahap —
satu-satunya yang mereka butuhkan dari sini adalah tabel outcome, sementara `ocr_results` memuat
teks OCR seluruh kartu dan `structuring_results` memuat 26 field internal.

Satu kolom yang mudah terlewat: `ocr_jobs.input` memuat presigned URL, yang setara kredensial
pembawa ke gambar KK itu sendiri. Ia dikosongkan di transaksi `complete()`/`fail()` milik job,
bukan lewat sapuan retensi `ds` — §3.1 menjanjikan job terlantar bisa dijalankan ulang dari URL itu,
dan janji itu hanya berlaku selagi job `PROCESSING`.

**Terbuka, dan tidak bisa diputuskan sepihak:** apakah Orkestrasi pusat terhubung dengan peran yang
bisa dibatasi nlm-k2, atau dengan peran pemilik yang tidak bisa. Kalau yang kedua, matriks ini jadi
kesepakatan lintas tim, bukan sesuatu yang bisa ditegakkan baseline.

---

## R14a — guardrails untuk gambar yang tidak bisa dinilai

`verdict` memperoleh nilai ketiga `"unassessable"`, dengan `probability_bad: null` dan
`passed: false`. Ini memperbaiki perilaku yang diwarisi, bukan menambah hiasan: hari ini
`app/services/pages.py` melempar `BadRequest` (400) untuk gambar yang tidak terbaca, sementara §5.2
mewajibkan guardrails **selalu** menjawab 200 dengan vonis di `data.passed`.

Dibedakan dari `reject` karena keduanya butuh alert dan perbaikan yang berbeda: "kami menilai ini
buruk" versus "kami tidak bisa menilai". Inti model K2Quality melempar
`ImageValidationError`/`ImageLoadError` di belasan tempat, termasuk untuk dimensi di luar rentang —
jadi foto KK yang sangat jelas dari kamera baru pun bisa mendarat di sini kalau batas dimensinya
diwarisi dari korpus lain. Teks `reason`-nya harus ditulis agen B dan ditambahkan ke tabel pesan
§3.5; jangan memakai ulang pesan "kualitas gambar terlalu rendah", karena itu diagnosis yang salah.

**Konsekuensi kontrak:** §10 saat ini hanya mengenal `accepted | reject`. Sudah masuk daftar
perubahan menuju draf 10.

---

## Mode serah-terima — `PIPELINE_HANDOFF_BY_REFERENCE=true`

Dianjurkan §13.2 dan catatan terbuka #6, dan spike §7.1 mengukurnya: **177 kotak untuk satu kartu**.
Mode ini menjauhkan muatan sebesar itu dari baris outbox — termasuk dead letter, yang bertahan 24 jam
secara bawaan dan karenanya juga persoalan R30.

Syaratnya `DATABASE_URL` ada di ketiga tahap, yang memang sudah divalidasi `config.py`.

---

## Penyelarasan larik anggota

Larik `anggota_keluarga` structuring dan scoring **selaras posisional**, dan structuring otoritatif
atas panjangnya. Panjang yang tidak cocok adalah cacat pipeline ini, bukan sifat dokumen, jadi
`kk.contract_fields()` melempar alih-alih memotong atau menambal: salah geser satu indeks akan
menghasilkan 200 yang tampak benar sambil membawa confidence milik orang lain.

**Terbuka:** apakah produksi gagal-tertutup (422) atau turun ke `confidence: 0` untuk anggota yang
tidak terskor. §2.4 mengiklankan `SCORING_FAILED` sebagai "aman diulang", padahal pengulangan
menghasilkan ketidakcocokan yang sama — jadi kalau gagal-tertutup dipertahankan, kode galatnya perlu
yang tidak dianjurkan untuk diulang. Diputuskan bersama tahap scoring sungguhan.

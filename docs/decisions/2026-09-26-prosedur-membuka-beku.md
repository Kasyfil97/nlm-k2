# Prosedur membuka beku (R22b)

Tanggal: 26 September 2026 · Unit 6

Gerbang R6 membekukan `libs/ocr_common/**` (termasuk `tests/`), `Makefile`, `docker-compose.yml`,
`docker-compose.db.yml`, `pyproject.toml`, dan `db/**`. Pembekuan tanpa cara membukanya bukan
pembekuan, melainkan kebuntuan — dan cara membuka yang tidak tertulis akan berubah jadi integrator
sebagai antrean, yaitu biaya yang justru dihindari dengan membuat fase 0.

## Tiga kategori berkas

| Kategori | Isi | Siapa menyunting |
|---|---|---|
| Milik agen | `services/orchestrator/**`, `services/guardrails/**`, `services/extraction/**` | agennya sendiri, bebas |
| **Beku** | `libs/ocr_common/**`, `Makefile`, `docker-compose*.yml`, `pyproject.toml`, `db/**` | hanya lewat prosedur di bawah |
| Milik integrator | `scripts/**`, `.github/**`, `api/**`, `services/structuring/**`, `services/scoring/**`, `.env.example` (akar dan per service), dokumen akar | integrator, atas permintaan |

Kategori ketiga ada karena berkas yang tidak dimiliki siapa pun **dan** tidak beku adalah berkas
yang disunting dua agen di hari yang sama. Kedua service stub masuk ke sini: mereka dibangun di
fase 0 tetapi dipakai ketiga agen di fase 1.

## Bentuk permintaan

Satu isu atau pesan, memuat empat hal dan tidak lebih:

1. **Berkas dan baris** yang perlu berubah.
2. **Kenapa tidak bisa diselesaikan di dalam direktori agen sendiri.** Ini penyaring utamanya —
   sebagian besar permintaan ternyata bisa.
3. **Siapa lagi yang terpengaruh.** Perubahan bentuk di `ocr_common` menyentuh ketiganya; sebuah
   kunci `lock-<service>` hanya menyentuh satu.
4. **Apakah menunggu sampai titik sinkronisasi berikutnya cukup**, atau memblokir hari ini.

## Siapa memutuskan

Integrator — aktor yang sama yang menjalankan fase 0 dan fase 2 (R33). Perannya berlanjut
sepanjang fase 1; ia tidak berhenti setelah gerbang R6.

## Irama

Permintaan **dikumpulkan dan digerbangi paling sering sekali sehari**, tidak satu per satu.
Alasannya aritmetika: menjalankan ulang gerbang R6 berarti fixture bentuk, periksa semantik,
`make lint`, `make test-lib`, dan `make up-db` + `db-upgrade` + `db-external` + `db-check` terhadap
Postgres sungguhan. Membayar itu per permintaan akan membuat serialisasi lebih mahal daripada
konflik yang dihindarinya.

Pengecualian: sebuah perubahan yang **memblokir seluruh agen** digerbangi begitu siap.

## Setelah perubahan mendarat

1. Gerbang R6 dijalankan ulang **penuh**. Bukan sebagiannya — yang paling mungkin rusak justru
   fixture bentuk, dan itu butir yang paling mudah dilewati.
2. Revisi beku naik satu. Nomornya adalah tag git sederhana (`freeze-2`, `freeze-3`).
3. Ketiga agen menarik perubahan itu di titik sinkronisasi yang sama, bukan kapan-kapan. Dua agen
   yang bekerja di atas revisi beku berbeda adalah persis penyimpangan yang R22 cegah.
4. **Agen yang sudah hijau menjalankan ulang R24**, bukan R6. Definisi selesai mereka tercapai
   terhadap revisi beku tertentu, dan perubahan `ocr_common` menurut definisinya membuat hijau itu
   basi. R24 murah; R6 tidak.

## Sementara gerbang berjalan

Agen yang **tidak** meminta terus bekerja di revisi beku yang lama. Mereka tidak menarik perubahan
setengah jadi, dan tidak menunggu. Yang meminta boleh bekerja di atas perubahannya secara lokal,
dengan risiko bahwa gerbang bisa menolaknya.

## Berapa banyak yang wajar

Nol adalah target yang salah — pembekuan yang tidak pernah dibuka biasanya berarti agennya
mengakalinya. Tetapi permintaan yang **berulang pada berkas yang sama** adalah sinyal bahwa fase 0
melewatkan sesuatu, dan itu dicatat, bukan sekadar dilayani. Dua kandidat yang sudah diketahui:

- **Bentuk di `ocr_common`.** Inventaris R2 dibangun dengan memburu simbol bernama lama lalu
  diperluas dua kali saat pelaksanaan. Perluasan ketiga di fase 1 masuk akal.
- **Kunci env di compose.** Sudah disediakan lebih dulu di Unit 6 justru untuk ini, tetapi
  daftarnya diturunkan dari §13 kontrak ditambah delta yang diketahui — bukan dari kode yang
  belum ditulis.

## Catatan revisi beku

| Tag | Isi | Kenapa tidak bisa menunggu |
|---|---|---|
| `freeze-1` | Gerbang R6, akhir fase 0 | — |
| `freeze-2` | `openapi.main` menulis LF eksplisit; `TEST_API_KEY` ganda dibuang; contoh NIK di `OcrBoxPayload` pakai provinsi 99 | Butir pertama memblokir ketiga agen: setiap `make openapi-<service>` di Windows menulis CRLF ke pohon kerja, dan pohon kerja itu build context Docker |

| `freeze-3` | `require_pii_audit()` pindah ke kelas yang punya fieldnya; tiga `# type: ignore` mypy diganti `cast`; `sorted(route.methods or ())`; penekanan `ty` di tiga pabrik settings uji | Tidak memblokir siapa pun: digerbangi setelah kedua agen fase 1 selesai, justru supaya tidak ada yang bekerja di atas revisi lama |

Ditemukan saat Unit 7, bukan saat gerbang R6 — karena R6 memeriksa isi berkas lewat git, yang sudah
menormalkannya. Pemeriksaan yang ditambahkan: tidak ada berkas terlacak yang memuat CRLF di pohon
kerja. Itu masuk daftar gerbang, bukan hanya diperbaiki sekali.

Gerbang R6 sebagaimana dijalankan di fase 0 **tidak memuat `make typecheck`**. Itu ketahuan di
`freeze-3`: `ty` melaporkan 161 diagnostik di pustaka, tiga di antaranya di kode yang dikirim dan
satu di antaranya cacat sungguhan (`require_pii_audit()` membaca field yang tidak ada di kelasnya,
jadi service pertama di luar scoring yang memanggilnya akan dapat `AttributeError`, bukan
`ValueError` yang dijanjikan). Dua hal masuk daftar gerbang karenanya: `make typecheck` penuh, dan
kesadaran bahwa `# type: ignore` adalah sintaks mypy yang `ty` tidak baca sama sekali.

# R18b: orientasi dan pelurusan, diukur terhadap VM PP-OCRv6

Tanggal: 27 September 2026 · setelah Unit 10

R18b dibiarkan terbuka oleh Unit 9, dan agen C **benar** menolak menutupnya dengan hook `deskew()`
kosong: hook seperti itu akan terbaca "sudah ditangani" di setiap tinjauan berikutnya. Sekarang
pertanyaannya bisa dijawab dengan angka, bukan dengan penalaran, karena model OCR yang sesungguhnya
dipakai sudah tersedia: `http://10.213.191.203:8040` (`/health` melaporkan `PP-OCRv6_medium_det` +
`PP-OCRv6_medium_rec`, PaddleOCR, gpu).

Pertanyaannya: §7.1 menjanjikan `poly` pada gambar yang **sudah diluruskan**, sementara jalur torch
K2Extractor menyetel `use_angle_cls = False`. Siapa yang meluruskan, dan dalam kerangka gambar mana
`poly` dilaporkan?

## Cara mengukurnya

Satu kartu sintetis (`ocr_common.synthetic_kk`, provinsi 99) dalam empat bentuk: tegak, miring 13°,
diputar 90°, dan terbalik 180°. Masing-masing dikirim dua kali — dengan
`use_doc_orientation_classify=true` dan dengan `false` — lalu dibandingkan kotak pembatas seluruh
`poly` terhadap `page.width`/`page.height` yang dilaporkan VM.

## Hasilnya

| Gambar dikirim | orientasi NYALA | orientasi MATI |
|---|---|---|
| tegak 1000x620 | bbox (37,43)-(814,545), muat | sama |
| **diputar 90° 620x1000** | bbox (38,43)-(812,545), **TIDAK muat di page yang dilaporkan** | bbox (40,184)-(548,962), muat, **8 kotak bukan 9** |
| terbalik 180° | bbox (38,44)-(812,546), muat | bbox (184,75)-(961,578), muat |
| miring 13° 1114x830 | bbox (52,46)-(832,652), muat, **kotak tetap miring** | sama |

Tiga kesimpulan, masing-masing punya konsekuensi berbeda.

### 1. Rotasi kasar 90°/180° memang dikoreksi

Dengan `use_doc_orientation_classify=true`, kartu yang diputar 90° mengembalikan `poly` di kerangka
**tegak**: header `KARTU KELUARGA` datang sebagai `[[39,43],[354,43],[354,73],[39,73]]`, kotak
horizontal, persis seperti pada kartu tegak. Dengan saklar itu mati, kotaknya ada di kerangka yang
dikirim **dan satu kotak hilang** (8 dari 9) — jadi saklarnya bukan hanya soal geometri, ia juga
memperbaiki pengenalan. **Nyalakan.**

### 2. Kemiringan halus TIDAK dikoreksi — §7.1 tetap tidak terpenuhi

Kartu miring 13° mengembalikan segi empat yang betul-betul miring: sisi atas header turun 72 px dari
kiri ke kanan. `use_doc_unwarping` mati di VM (`/health` → `doc_unwarping: false`), dan
menyalakannya bukan wewenang repo ini.

Jadi **janji §7.1 bahwa `poly` ada pada gambar yang sudah diluruskan hanya separuh benar**: tegaknya
ya, lurusnya tidak. Dua jalan keluar, dan pilihannya bukan milik unit ini sendiri:

- **Luruskan di tahap extraction**, dengan biaya satu putaran deteksi tambahan; atau
- **ubah §7.1** supaya ia menyatakan kerangkanya adalah gambar yang dikirim setelah koreksi
  orientasi kasar, dan pindahkan masalahnya ke penetapan kolom di structuring.

Yang tidak boleh: membiarkan §7.1 berbunyi seperti sekarang sementara kotaknya miring. Structuring
menetapkan kolom dari geometri ini, dan kegagalannya sistematis — ia tidak muncul sebagai galat, ia
muncul sebagai field yang tertukar kolom di sebagian dokumen.

Untuk sekarang jalur mock `extraction` sengaja mengeluarkan poly miring (`_TILT = 0.014`), sehingga
tidak ada yang di hilir bisa diam-diam mulai mengandaikan kotak tegak.

### 3. `page.width`/`page.height` tidak boleh dipercaya — ini yang paling mudah menjebak

Dengan koreksi orientasi menyala, VM mengembalikan `poly` di kerangka **yang sudah dikoreksi**
tetapi tetap melaporkan dimensi **yang dikirim**. Untuk kartu 620x1000 yang diputar, ia menjawab
`page=620x1000` sementara kotak pembatas `poly` mencapai x=812 — polinya **tidak muat di halaman yang
ia sebutkan sendiri**.

Siapa pun yang menskalakan atau menumpangkan `poly` memakai dimensi itu akan salah, dan salahnya
hanya pada dokumen yang difoto menyamping. Repo ini lolos karena §7.1 tidak punya dimensi halaman
sama sekali dan tahapnya tidak menyimpannya; `app/ml/remote.py` mengabaikan kedua kolom itu secara
eksplisit, dengan uji yang memakunya (`test_page_dimensions_are_ignored`).

**Ini perlu disampaikan ke tim ML**: entah `page` ikut dikoreksi, atau responsnya menyatakan kerangka
mana yang berlaku. Sekarang responsnya tidak konsisten dengan dirinya sendiri.

## Versi model

§7.1 mencontohkan PP-OCRv6 sementara bobot di K2Extractor adalah PP-OCRv5, dan agen C mencatatnya
sebagai butir rekonsiliasi. VM ini **memang PP-OCRv6** (`medium_det` + `medium_rec`), jadi contoh
kontraknya benar dan yang tertinggal adalah jalur dalam-proses. `remote` melaporkan versi yang
benar-benar berjalan, karena itulah gunanya field `model`.

## Yang berubah di kode karena pengukuran ini

Tidak ada yang meluruskan gambar — itu keputusan yang masih terbuka. Yang berubah hanyalah backend
`remote` jadi benar-benar bisa memanggil VM ini: `EXTRACTION_OCR_PATH` (`/ocr`, bukan
`/v1/predict/json`), `EXTRACTION_OCR_QUERY` (parameternya di query string, bukan field form), dan
`parse_pages` menerima bentuk `texts: [{text, score, poly}]` di samping tiga daftar sejajar
`rec_*`. Bentuk per kotak VM **sudah** bentuk §7.1, jadi tidak ada yang dikonversi — hanya divalidasi.

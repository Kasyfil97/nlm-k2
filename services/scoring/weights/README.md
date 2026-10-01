# `kk_trust_model.joblib`

Artefak trust model KK yang dimuat `SCORING_BACKEND=calibrated`. Bukan dilatih di repo ini — repo ini
hanya menyajikannya.

## Isinya

```
{"versi": "kk-trust-hgb-isotonic-v2",
 "member": {"model", "isotonic", "kolom", "fields", "ambang", "tepi_bin", "estimator", "metrik"},
 "doc":    {...idem...}}
```

Dua keluarga model, bukan satu dengan flag: sel anggota punya `crf_conf` dan seluruh internal CRF,
field dokumen tidak punya `crf_conf` sama sekali. Satu model atas keduanya harus belajar mengabaikan
separuh masukannya tergantung sebuah flag, dan diukur atas campuran dua base rate yang jauh berbeda.

**`kolom` adalah satu-satunya sumber urutan vektor.** Estimatornya dilatih di atas array numpy, bukan
DataFrame, justru supaya tidak ada nama kolom tersimpan yang memberi kesan sklearn memeriksa sesuatu
yang sebenarnya tidak diperiksa. `CalibratedTrustModel` menelusuri `kolom` dan menolak memuat artefak
yang panjang kolomnya tidak cocok dengan `n_features_in_` estimatornya.

**`ambang` dan `tepi_bin` bukan konfigurasi.** Keduanya properti model yang dilatih dan berpindah
bersama bobotnya; keduanya juga ikut keluar di hasil scoring, yang membuat `auto` di respons
`extract-ocr` identik dengan `auto` di baris outcome secara konstruksi.

## Angka yang terukur (1686 sel anggota + 162 sel dokumen, 97 dokumen)

Semua angka diukur pada dokumen UJI: GroupKFold per dokumen, dan di tiap fold seluruh prosedur latih
(model, isotonic, ambang) diulang hanya di dokumen latih. Jadi angka ini adalah perilaku artefak yang
dikirim pada dokumen yang belum pernah dilihatnya.

| | anggota | dokumen |
|---|---|---|
| akurasi exact parser | 86.89% | 83.95% |
| AUC | 0.847 | 0.814 |
| AUC sinyal mentah kontrak | 0.502 (`crf_conf`) | 0.619 (`ocr_conf`) |
| cakupan `auto` | 16.4% | 28.4% |
| presisi `auto` | 98.55% (4 salah dari 276) | 93.48% (3 salah dari 46) |
| batas bawah Clopper-Pearson 95% presisi `auto` | 96.71% | 84.00% |

**Presisi `auto` belum 100%.** Ambang dipilih di titik tertinggi tempat sel salah muncul di data latih,
dan dokumen baru tetap bisa menghasilkan sel salah di atasnya. Yang boleh dijanjikan adalah batas bawah
Clopper-Pearson, bukan angka presisinya.

`nomor_kk` dan `nama_lengkap` tidak punya ambang: di data latih tidak ada titik yang di atasnya semua
sel benar. Keduanya **tidak pernah `auto`** (`ocr_common.kk.contract_fields`). Mereka tidak jatuh ke
`FIELD_CONFIDENCE_THRESHOLD`: di dokumen uji, `nomor_kk` pada >= 0.5 hanya 83% benar, dan `nama_lengkap`
83%. Ketiadaan ambang adalah informasi, bukan cacat.

### v1 -> v2

v1 memasang isotonic di atas probabilitas OOF yang SUDAH dikalibrasi, lalu menerapkannya ke probabilitas
mentah model final; skala latih dan skala pakai tidak sama. Laporannya menyebut presisi `auto` 100%
(CP95 99.34%), padahal pada dokumen uji hasilnya 97.77% dengan 12 sel salah lolos. v2 memasang isotonic
langsung di atas probabilitas mentah OOF dan memilih ambang di skala yang sama dengan keluaran service:
presisi 98.55%, 4 salah, cakupan turun dari 31.9% ke 16.4%.

Keluaran isotonic berupa anak tangga: dua masukan yang cukup berbeda bisa mendapat skor yang sama persis.
Tepi bin karena itu dihitung atas nilai skor unik supaya tetap sepuluh bin.

## Melatih ulang

Pelatihannya ada di luar repo ini (`kk_trust_model/` di ruang kerja OCR KK: `src/build_dataset.py`,
`src/evaluate.py`, lalu `src/train.py`; laporannya `REPORT.md`), dan membaca ground truth level sel plus
keluaran parser K2Regex-v2. **Latih dengan versi scikit-learn, numpy, dan joblib yang sama persis dengan
`requirements.txt` service ini** (sekarang scikit-learn 1.9.0): pickle dari versi lain dimuat dengan
`InconsistentVersionWarning` dan tidak dijamin menghasilkan angka yang sama. Setelah melatih ulang, salin artefaknya ke sini dan jalankan `pytest tests/test_calibrated_model.py` — di situ ada uji
yang membandingkan kolom artefak dengan `ocr_common.kk.MEMBER_CELL_FEATURES` / `DOC_CELL_FEATURES`,
sehingga penyimpangan antara apa yang structuring kirim dan apa yang model baca gagal pada hari ia
mendarat, bukan pada hari angkanya ternyata aneh.

Dua hal yang **mengharuskan** pelatihan ulang, bukan sekadar menambah kolom:

* `guardrail_probability` masuk ground truth. Sekarang kolomnya selalu kosong saat latih dan dibuang.
* Fitur baru di `MEMBER_CELL_FEATURES` / `DOC_CELL_FEATURES`. Nama yang tidak dikenal artefak diabaikan;
  nama yang dikenal tapi tidak dikirim menjadi NaN.

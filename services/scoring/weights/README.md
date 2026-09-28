# `kk_trust_model.joblib`

Artefak trust model KK yang dimuat `SCORING_BACKEND=calibrated`. Bukan dilatih di repo ini — repo ini
hanya menyajikannya.

## Isinya

```
{"versi": "kk-trust-hgb-isotonic-v1",
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

| | anggota | dokumen |
|---|---|---|
| akurasi exact | 86.89% | 83.95% |
| AUC out-of-fold | 0.855 | 0.815 |
| AUC sinyal mentah kontrak | 0.502 (`crf_conf`) | 0.619 (`ocr_conf`) |
| cakupan pada presisi 100%, ambang per field | 26.6% | 30.9% |
| batas bawah Clopper-Pearson 95% | 99.34% | 94.18% |

GroupKFold per dokumen, isotonic bersarang di dalam tiap fold latih. Angka 100% itu **diukur pada
held-out, bukan dijamin** — yang boleh dijanjikan adalah batas bawah Clopper-Pearson.

`nomor_kk` tidak punya ambang presisi-100% (hanya 5 sel bersih dari 97), jadi ia tidak ada di
`ambang` dan jatuh ke `FIELD_CONFIDENCE_THRESHOLD`. Ketiadaannya adalah informasi, bukan cacat.

## Melatih ulang

Pelatihannya ada di luar repo ini (`conf_model/` di ruang kerja OCR KK: `build_dataset.py` lalu
`train_conf.py`), dan membaca ground truth level sel plus keluaran parser K2Regex-v2. Setelah melatih
ulang, salin artefaknya ke sini dan jalankan `pytest tests/test_calibrated_model.py` — di situ ada uji
yang membandingkan kolom artefak dengan `ocr_common.kk.MEMBER_CELL_FEATURES` / `DOC_CELL_FEATURES`,
sehingga penyimpangan antara apa yang structuring kirim dan apa yang model baca gagal pada hari ia
mendarat, bukan pada hari angkanya ternyata aneh.

Dua hal yang **mengharuskan** pelatihan ulang, bukan sekadar menambah kolom:

* `guardrail_probability` masuk ground truth. Sekarang kolomnya selalu kosong saat latih dan dibuang.
* Fitur baru di `MEMBER_CELL_FEATURES` / `DOC_CELL_FEATURES`. Nama yang tidak dikenal artefak diabaikan;
  nama yang dikenal tapi tidak dikirim menjadi NaN.

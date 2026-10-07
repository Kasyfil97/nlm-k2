# `kk_trust_model.joblib`

Trust model KK yang dimuat `SCORING_BACKEND=kk_field`: **s11_blend_lr_hgb** (rata-rata probabilitas
Logistic Regression dan HistGradientBoosting), dilatih di atas keluaran structuring **m04_06102026**.
Bukan dilatih di repo ini — repo ini hanya menyajikannya. Pelatihannya di ruang kerja OCR KK,
`scoring/training/` (laporan lengkap: `MODEL.md` di sana).

**Ia berpasangan.** Angkanya hanya sah di atas structuring yang melatihnya, yaitu
`STRUCTURING_BACKEND=kk_model` dengan `services/structuring/weights/kk_structuring_model.joblib`. Keduanya
diekspor bersama dan diganti bersama:

```bash
# dari root ruang kerja (ocr-kk/), python dengan scikit-learn 1.7.2 / xgboost 3.2.0 / numpy 2.2.6
python3 scoring/training/export_nlm_k2.py s11_blend_lr_hgb \
    --baseline nlm-k2/services/structuring/tests/fixtures/kk_model_baseline.json
cd nlm-k2 && make kk-model-parity      # keluaran service == keluaran training, per dokumen
```

Field dengan vektor fitur dari structuring lain (`kk_regex`) dinilai `null`, bukan dibaca keliru.

## Apa yang dinilai

`final_conf` = P(field **benar**), dengan benar berarti keduanya: kotak OCR penyusun field = kotak GT pada
anggota yang sama (**struktur**), dan teks akhir = nilai GT setelah normalisasi A-Z0-9 (**nilai**). Satu model
untuk sembilan field kontrak, field sebagai one-hot; 42 fitur per field (`ocr_common.kk.FIELD_FEATURES`)
yang dihitung di structuring, ditambah model teks n-gram karakter atas `"<field>|<nilai>"`.

## Isinya

```
{"versi": "kk-trust-s11_blend_lr_hgb", "structuring_versi": "kk-structuring-m04_06102026",
 "dataset", "fields", "ambang": {nama internal: ambang},
 "spec": {"model": "blend", ...}, "est": {"parts": [{"spec", "est": {"main", "text"}, "names"}, ...]}, "names"}
```

`names` tiap part = kolom yang dipakai saat fit; service membangunnya ulang dari `spec` dan menolak start
kalau berbeda. **`ambang` bukan konfigurasi**: ambang CV untuk presisi 98% (0,9592, sama untuk sembilan
field), berpindah bersama bobotnya dan ikut keluar di hasil scoring sebagai `thresholds`.

## Angka yang terukur (dataset 06102026)

Train 318 dokumen (out-of-fold, GroupKFold 5 per dokumen), test 100 dokumen (2.428 field terisi).

| | CV | test |
|---|---|---|
| AUROC | 0,892 | 0,937 |
| ECE | 0,008 | 0,018 |
| cakupan @ presisi 98% | 58,4% | 59,9% (presisi 99,4%) pada ambang CV 0,9592 |

Per field (test, ambang 0,9592): cakupan `no_kk` 89%, `nama_kepala_keluarga` 87%, `status_hubungan` 89%,
`nik` 62%, `pekerjaan` 54%, `ayah` 54%, `nama_lengkap` 52%, `pendidikan` 46%, `ibu` 44%.

Di service, ukuran halaman diambil dari jangkauan poligon (§7.1 tidak membawanya): pada 100 dokumen test,
4 dari 2.428 field berubah dan AUROC 0,9374 → 0,9360. `make kk-model-parity` memeriksa 418 dokumen
train+test; semuanya identik dengan rantai training pada ukuran halaman yang sama.

## Versi pustaka

Dilatih dengan scikit-learn 1.7.2 dan numpy 2.2.6; `requirements.txt` service ini memin versi yang sama,
karena pickle sklearn tidak dijamin menghasilkan angka yang sama di versi lain. Melatih ulang dengan versi
lain berarti menaikkan pin di sini dan di structuring bersamaan.

## Riwayat

Artefak sebelumnya (`kk-trust-hgb-isotonic-v2`, backend `calibrated`, pasangan `kk_regex`) ada di riwayat
git (commit `38598f2`); `tests/test_calibrated_model.py` menjalankannya lewat `CALIBRATED_TRUST_MODEL`.

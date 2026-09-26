# Spike §7.1 — bentuk muatan OCR, hasil pengamatan

Tanggal: 26 September 2026 · Fase 0, sebelum gerbang beku R6
Alasan: §7.1 adalah satu-satunya bentuk yang dibekukan fase 0 yang **belum pernah dikeluarkan
sistem mana pun**. §7.3 ditranskripsi dari keluaran K2Regex-v2 yang nyata dan §8.3 belum ada
apa-apa, tetapi §7.1 ditulis pada versi yang belum dijalankan. Membekukannya dari contoh kontrak
saja berarti memvalidasi aspirasi dengan mock yang ditulis dari aspirasi yang sama.

## Cara

Jalur `fullpytorch` `K2Extractor/src/services/ocr_backends.py` dijalankan sekali, read-only,
lewat `.venv` K2Extractor (torch 2.14.0+cpu), atas satu gambar KK nyata
(`raw_ocr/raw_image/PH2403SHU6.jpg`, 955×663 RGB). Bobot
`models_extractor_pytorch_server_{det,rec}_v1.0.pth`.

## Yang teramati

**Bentuk keluaran backend** adalah `list` berisi **satu** `dict`:

```
[{"rec_texts": [str], "rec_scores": [float], "rec_polys": [ndarray]}]
```

dan `ocr_service.transform_ocr_result` mengubahnya lagi jadi `[(poly, (text, score)), …]` —
persis bentuk lama yang dicatat §12. Jadi `{text, score, poly}` di §7.1 memang **transformasi yang
harus dilakukan nlm-k2**, bukan sesuatu yang diwarisi.

| Yang diperiksa | Hasil |
|---|---|
| `poly` 4×2 | ya, seluruh 177 kotak |
| tipe `poly` | `float32`, bukan integer |
| bentuk `poly` | segiempat miring sungguhan, **bukan** kotak tegak |
| jumlah kotak, satu KK | 177 |
| skor | min 0,2822 · rata-rata 0,8140 · maks 0,9999 |

Contoh yang teramati, sudah dalam bentuk §7.1:

```json
{
  "texts": [
    {"text": "5", "score": 0.5591,
     "poly": [[291.0, 7.0], [306.0, 6.0], [309.0, 26.0], [294.0, 28.0]]}
  ],
  "text_regions_count": 177,
  "avg_doc_score": 0.814,
  "min_doc_score": 0.2822
}
```

## Akibat

**Membuang `BoundingBox` benar dan perlu.** `poly[0]` di atas miring — `y` berjalan 7 → 6 → 26 → 28.
Tipe `BoundingBox` nilam (`x1,y1,x2,y2`, kotak tegak) tidak bisa mewakilinya tanpa kehilangan
informasi. Ini mengunci keputusan R2.

**R18b terkonfirmasi, bukan lagi kekhawatiran.** Kemiringan itu sendiri buktinya: jalur
det+rec ini mengembalikan koordinat dalam **bingkai gambar asli**, tidak diluruskan, sejalan dengan
`use_angle_cls = False`. Janji §7.1 — `poly` "dalam piksel gambar yang **sudah** diluruskan
PaddleOCR" — **tidak dipenuhi backend ini**. Penanggung jawab pelurusan harus ditunjuk sebelum
backend asli ditulis, atau §7.1 diubah.

**`PIPELINE_HANDOFF_BY_REFERENCE=true` terbukti tepat.** 177 kotak untuk satu kartu, dan itu baru
satu contoh; menjauhkan muatan sebesar itu dari baris outbox (termasuk dead letter yang bertahan
24 jam) adalah pilihan yang benar.

**Agregatnya bisa dihitung langsung** dari `rec_scores`, tanpa tambahan apa pun dari model.

## Yang spike ini TIDAK buktikan

Kualitas rekognisinya buruk — 177 kotak yang sebagian besar satu karakter (`'5'`, `'S'`, `'IK'`).
Backend dipanggil dengan **nilai bawaan konstruktor**, bukan konfigurasi K2Extractor
(`rec_image_shape`, `det_limit_side_len`, dan `PaddleOCR_server.yaml`). Jadi spike ini memvalidasi
**bentuk**, bukan **mutu**. Mutu diukur di unit yang menulis backend asli, dan angka baseline
korpus 1184 dokumen sudah ada di tempat lain.

## Untuk Unit 2

Fixture emas §7.1 dibangun dari pengamatan di atas, bukan dari contoh kontrak. Yang wajib ditegaskan
fixture: `poly` 4×2 bertipe float, `texts[]` menggantikan `blocks[]`, tidak ada `full_text`, dan
ketiga agregat (`text_regions_count`, `avg_doc_score`, `min_doc_score`) ada.

"""Kirim gambar KK lewat extraction -> structuring `kk_model` -> scoring `kk_field`, tampilkan hasil per field.

    scripts/try_kk_model.sh up
    python scripts/try_kk_model.py test/data/kk_true.jpg [lain.jpg ...] [--json hasil.json]

Untuk setiap field kontrak: nilai, P(benar) dari trust model, ambang model, dan `confidence` 0/1 yang akan
keluar di `extract-ocr` (P >= ambang). `--threshold 0.9` mencoba ambang lain (`column_confidence_threshold`
all_field), seperti yang bisa dikirim Orkestrasi pusat per request. `--json` menyimpan respons mentah ketiga
tahap untuk diperiksa lebih jauh. Hanya pustaka standar, jadi jalan dengan python apa pun.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.request
import uuid
from pathlib import Path

INTERNAL = {"status_hubungan_dalam_rumah_tangga": "status_hubungan_dalam_keluarga", "no_kk": "nomor_kk"}


def _post(url: str, api_key: str, *, body: dict | None = None, file: Path | None = None) -> dict:
    headers = {"X-API-Key": api_key}
    if file is not None:
        boundary = uuid.uuid4().hex
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        data = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
                f"Content-Type: {kind}\r\n\r\n"
            ).encode()
            + file.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{url}: HTTP {exc.code}: {exc.read().decode()[:500]}") from None


def run(image: Path, a: argparse.Namespace) -> dict:
    ocr = _post(f"{a.extraction}/v1/extraction/extract", a.api_key, file=image)["data"]
    structuring = _post(f"{a.structuring}/v1/structuring-direct", a.api_key, body={"ocr": ocr})["data"]
    body: dict = {"structuring": structuring, "ocr": ocr}
    if a.threshold is not None:
        body["column_confidence_threshold"] = {"all_field": a.threshold}
    scoring = _post(f"{a.scoring}/v1/scoring-direct", a.api_key, body=body)["data"]
    return {"image": str(image), "ocr": ocr, "structuring": structuring, "scoring": scoring}


def show(result: dict) -> None:
    structuring, scoring = result["structuring"], result["scoring"]
    print(f"\n=== {result['image']}  ({result['ocr'].get('text_regions_count')} kotak OCR)")
    if structuring.get("reject_reason"):
        print(f"DITOLAK structuring (400 di extract-ocr): {structuring['reject_reason']}")
        return
    decisions = scoring["decisions"]
    rows = [
        ("", name, decisions[name], scoring["fields"].get(INTERNAL.get(name, name)))
        for name in ("no_kk", "nama_kepala_keluarga")
    ]
    for index, member in enumerate(decisions["anggota_keluarga"]):
        scores = scoring["anggota_keluarga"][index]
        rows += [(str(index), name, cell, scores.get(INTERNAL.get(name, name))) for name, cell in member.items()]
    print(f"{'#':>2}  {'field':<36} {'P(benar)':>8} {'ambang':>7} {'conf':>4}  nilai")
    for member, name, cell, p in rows:
        prob = "-" if p is None else f"{p:.4f}"
        limit = "-" if cell["threshold"] is None else f"{cell['threshold']:.4f}"
        print(f"{member:>2}  {name:<36} {prob:>8} {limit:>7} {cell['confidence']:>4}  {cell['value']}")
    filled = [r for r in rows if r[2]["value"]]
    lolos = sum(r[2]["confidence"] for r in filled)
    print(f"model {scoring['model']}: {lolos}/{len(filled)} field terisi lolos tanpa review (confidence 1)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--extraction", default="http://127.0.0.1:18042")
    ap.add_argument("--structuring", default="http://127.0.0.1:18043")
    ap.add_argument("--scoring", default="http://127.0.0.1:18044")
    ap.add_argument("--api-key", default="try-kk-model")
    ap.add_argument("--threshold", type=float, default=None, help="ambang all_field per request (bawaan: ambang model)")
    ap.add_argument("--json", type=Path, default=None, help="simpan respons mentah ketiga tahap")
    a = ap.parse_args()
    results = []
    for image in a.images:
        result = run(image, a)
        show(result)
        results.append(result)
    if a.json:
        a.json.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nrespons mentah -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

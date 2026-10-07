"""Kesetaraan pasangan `kk_model` (structuring) + `kk_field` (scoring) dengan rantai training.

    python scripts/check_kk_model_parity.py [--corpus ../raw_ocr_v6] [--limit N]

Setiap dokumen di `services/structuring/tests/fixtures/kk_model_baseline.json` dijalankan lewat jalur
service yang sesungguhnya -- `StructuringService` + `KKModelStructurer`, lalu `ConfidenceService` +
`FieldTrustModel` -- dan keluarannya di-hash dengan cara yang sama seperti `export_nlm_k2.baseline_rows`
di ruang kerja training. `values` = nilai + conf structuring per field terisi, `scores` = final_conf.
Keduanya harus sama persis: angka confidence hanya bermakna kalau service menghitung yang sama dengan
yang diukur di laporan training.

Korpusnya OCR KK sungguhan dan tidak ada di repo (sama dengan uji baseline `kk_regex`); fixture-nya
hanya hash. Kedua service bernama paket `app`, jadi masing-masing dijalankan di prosesnya sendiri.
Keluar 1 bila ada yang berbeda, 2 bila korpus tidak ada.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "services" / "structuring" / "tests" / "fixtures" / "kk_model_baseline.json"
CORPUS = ROOT.parent / "raw_ocr_v6"


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _rows(structuring: dict, scoring: dict | None) -> list[dict]:
    """Field terisi dalam bentuk baris training: member "" untuk field dokumen, nama field training."""
    from ocr_common.kk import MODEL_FIELD_NAMES, SCORED_DOC_FIELDS, SCORED_MEMBER_FIELDS

    out = []
    for name in SCORED_DOC_FIELDS:
        cell = structuring[name]
        if cell["value"]:
            p = scoring["fields"][name] if scoring else None
            out.append({"member": "", "field": MODEL_FIELD_NAMES[name], "cell": cell, "p": p})
    for index, member in enumerate(structuring["anggota_keluarga"]):
        for name in SCORED_MEMBER_FIELDS:
            cell = member[name]
            if cell["value"]:
                p = scoring["anggota_keluarga"][index][name] if scoring else None
                out.append({"member": str(index), "field": MODEL_FIELD_NAMES[name], "cell": cell, "p": p})
    return sorted(out, key=lambda r: (r["member"], r["field"]))


def values_hash(structuring: dict) -> str:
    """Field dokumen tidak membawa `crf_conf`; padanannya di training (structuring_conf) diambil dari fiturnya."""
    import math

    rows = []
    for r in _rows(structuring, None):
        cell = r["cell"]
        conf = cell["crf_conf"]
        if conf is None:  # field dokumen: structuring_conf = sigmoid(lg_struct_min)
            conf = round(1 / (1 + math.exp(-cell["features"]["lg_struct_min"])), 4)
        rows.append([r["member"], r["field"], cell["value"], cell["ocr_conf"], conf])
    return _h(rows)


def scores_hash(structuring: dict, scoring: dict) -> str:
    return _h([[r["member"], r["field"], r["p"]] for r in _rows(structuring, scoring)])


def _stage_structuring(ids: list[str], corpus: Path, out: Path) -> None:
    from ocr_common.kk import ocr_aggregates

    from app.config import get_settings
    from app.ml.kk_model import KKModelStructurer
    from app.services.structuring_service import StructuringService

    service = StructuringService(KKModelStructurer(get_settings().structuring_model_path))
    with out.open("w", encoding="utf-8") as handle:
        for doc_id in ids:
            page = json.loads((corpus / f"{doc_id}.json").read_text(encoding="utf-8"))["pages"][0]
            texts = service.boxes_from_ocr(page)
            record = {"id": doc_id, "structuring": service.structure(texts), "ocr": ocr_aggregates(texts)}
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _stage_scoring(source: Path, out: Path) -> None:
    from app.config import get_settings
    from app.ml.kk_field import FieldTrustModel
    from app.services.confidence_service import ConfidenceService

    service = ConfidenceService(FieldTrustModel(get_settings().scoring_model_path))
    with source.open(encoding="utf-8") as handle, out.open("w", encoding="utf-8") as sink:
        for line in handle:
            record = json.loads(line)
            scored = service.score("kk", None, record["ocr"], record["structuring"], 0.5)
            sink.write(json.dumps({"id": record["id"], "scoring": scored}, ensure_ascii=False) + "\n")


def _run(stage: str, service: str, *args: str) -> None:
    env = {**os.environ, "API_KEY": os.environ.get("API_KEY", "parity"), "ENVIRONMENT": "local"}
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--stage", stage, *args],
        cwd=ROOT / "services" / service,
        env=env,
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--stage", choices=("structuring", "scoring"), help=argparse.SUPPRESS)
    ap.add_argument("--ids", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--src", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.stage:
        sys.path.insert(0, os.getcwd())
        if a.stage == "structuring":
            _stage_structuring(json.loads(a.ids.read_text()), a.corpus, a.out)
        else:
            _stage_scoring(a.src, a.out)
        return 0

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))["hashes"]
    ids = [i for i in baseline if (a.corpus / f"{i}.json").is_file()][: a.limit]
    if not ids:
        print(f"korpus tidak ada di {a.corpus} (OCR KK sungguhan, di luar repo)", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "ids.json").write_text(json.dumps(ids))
        _run(
            "structuring",
            "structuring",
            "--ids",
            str(work / "ids.json"),
            "--corpus",
            str(a.corpus.resolve()),
            "--out",
            str(work / "s.jsonl"),
        )
        _run("scoring", "scoring", "--src", str(work / "s.jsonl"), "--out", str(work / "c.jsonl"))
        structured = {r["id"]: r for r in map(json.loads, (work / "s.jsonl").read_text().splitlines())}
        scored = {r["id"]: r for r in map(json.loads, (work / "c.jsonl").read_text().splitlines())}

    bad_values = [i for i in ids if values_hash(structured[i]["structuring"]) != baseline[i]["values"]]
    bad_scores = [
        i for i in ids if scores_hash(structured[i]["structuring"], scored[i]["scoring"]) != baseline[i]["scores"]
    ]
    print(f"{len(ids)} dokumen: nilai berbeda {len(bad_values)}, skor berbeda {len(bad_scores)}")
    for i in (bad_values + bad_scores)[:10]:
        print(f"  {i}", file=sys.stderr)
    return 1 if bad_values or bad_scores else 0


if __name__ == "__main__":
    sys.exit(main())

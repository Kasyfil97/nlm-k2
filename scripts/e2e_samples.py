"""Uji ujung ke ujung dengan dokumen KK sungguhan dari `test/data/`, terhadap stack compose lokal.

Berbeda dengan `smoke_e2e.py`, yang menguji **pipa** lewat backend `mock` dan nama file pemicu, skrip
ini menguji **model**: guardrails, OCR, structuring, dan scoring yang sungguhan harus memberi putusan
yang benar atas gambar yang nyata. Jadi stack-nya harus jalan dengan backend sungguhan
(`GUARDRAILS_BACKEND=kk_quality`, `EXTRACTION_BACKEND=paddle`, `SCORING_BACKEND=calibrated`).

| Berkas | Kasus | Harapan |
|---|---|---|
| `kk_true.jpg` | KK benar | 200, sembilan field `{value, confidence}` (probabilitas), nilai cocok, `GET` identik |
| `kk_pdf.pdf` | KK benar, PDF | sama dengan di atas |
| `kk_cut.jpg` | KK terpotong | 400 dari guardrails; tanpa guardrails ditolak structuring (§7.4) |
| `kk_no_nokk.jpg` | nomor KK tidak ada | 400 dari guardrails; tanpa guardrails ditolak structuring (§7.4) |
| `kk_bad.jpg` | buram / pudar | 400 dari guardrails |

Nilai yang diharapkan pada kasus positif dibaca dari gambarnya, bukan disalin dari keluaran OCR.

Tanpa `column_confidence_threshold`, `confidence` adalah probabilitas trust model (float 0..1). Untuk setiap
kasus positif, satu request lagi dikirim dengan `column_confidence_threshold` bernilai 0 untuk
kesembilan field. Hasilnya, setiap field yang terisi harus ber-confidence 1. Ini membuktikan ambang per
request sampai ke scoring pada data sungguhan.

Guardrails hanya menolak bila request membawa ambang, jadi setiap request di sini mengirim
`guardrails_confidence_threshold` `{"acc_rej": 0.5}` (`GUARDRAILS_THRESHOLD`).

Untuk kasus negatif, dokumen yang sama dikirim sekali lagi dengan
`pipeline_name_sequence=["extraction","structuring","scoring"]`, yaitu tanpa guardrails. Ini melihat
apakah gerbang keabsahan di structuring juga menangkapnya. `kk_bad.jpg` saat ini **lolos** di jalur itu:
teksnya terbaca sebagian, jadi structuring tidak punya alasan menolak, dan guardrails-lah satu-satunya
penjaganya. Karena itu hasilnya dicetak sebagai CATATAN, bukan dihitung gagal.

`probability_bad` guardrails dicetak bersama selisihnya terhadap ambang. `kk_no_nokk.jpg` dan
`kk_pdf.pdf` sama-sama dekat dengan 0.5, jadi pergeseran bobot guardrails paling dulu terlihat di sana.

Variabel lingkungan dan pagar lokalnya sama dengan `smoke_e2e.py`: `SMOKE_DATABASE_URL` untuk
membersihkan baris, `ENVIRONMENT=local`, alamat 127.0.0.1. `E2E_SAMPLES_DIR` menunjuk folder data
(bawaan `test/data`).
"""

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
import smoke_e2e as smoke

from ocr_common.kk import CONTRACT_FIELDS

SAMPLES_DIR = os.environ.get("E2E_SAMPLES_DIR") or os.path.join(os.path.dirname(__file__), "..", "test", "data")
NO_GUARDRAILS = json.dumps(["extraction", "structuring", "scoring"])
DOC_KEYS = {"no_kk", "nama_kepala_keluarga", "anggota_keluarga"}
MEMBER_KEYS = set(CONTRACT_FIELDS) - DOC_KEYS


@dataclass(frozen=True)
class Positive:
    """Nilai yang dibaca dari gambar. `first_nik` hanya diisi bila digitnya terbaca jelas di gambar."""

    no_kk: str
    kepala: str
    members: int
    first_nik: str | None = None


@dataclass(frozen=True)
class Case:
    file: str
    label: str
    positive: Positive | None = None
    # Kasus negatif: apakah structuring diharapkan juga menolaknya bila guardrails dilewati.
    # None = celah yang sudah diketahui, dicetak sebagai catatan.
    structuring_rejects: bool | None = None


CASES = [
    Case("kk_true.jpg", "KK benar", Positive("3206321811130002", "JOJOH", 1, "3206324107600205")),
    Case("kk_pdf.pdf", "KK benar (PDF)", Positive("3376011502220002", "ROMZI FUAD BARABA", 3)),
    Case("kk_cut.jpg", "KK terpotong", structuring_rejects=True),
    Case("kk_no_nokk.jpg", "tanpa nomor KK", structuring_rejects=True),
    Case("kk_bad.jpg", "buram / pudar", structuring_rejects=None),
]

notes: list[str] = []


def _content_type(filename: str) -> str:
    return "application/pdf" if filename.lower().endswith(".pdf") else "image/jpeg"


def _read(filename: str) -> bytes:
    with open(os.path.join(SAMPLES_DIR, filename), "rb") as handle:
        return handle.read()


#: Ambang guardrails yang dikirim setiap request: tanpa ambang, guardrails meloloskan semua dokumen.
GUARDRAILS_THRESHOLD = 0.5


def _submit(client: httpx.Client, request_id: str, case: Case, content: bytes, **fields: str) -> dict[str, Any]:
    """POST /v1/extract-ocr; kalau jawabannya 202, tunggu lewat GET sampai selesai."""
    threshold = json.dumps({"acc_rej": GUARDRAILS_THRESHOLD})
    response = client.post(
        f"{smoke.URLS['orchestrator']}/v1/extract-ocr",
        headers=smoke.HEADERS,
        data={"request_id": request_id, "document_type": "kk", "guardrails_confidence_threshold": threshold, **fields},
        files={"file": (case.file, content, _content_type(case.file))},
    )
    body = response.json()
    deadline = time.monotonic() + smoke.TIMEOUT_SECONDS
    while response.status_code == 202 and time.monotonic() < deadline:
        time.sleep(1.0)
        response = smoke._status(client, request_id)
        body = response.json()
    return body


def _guardrails_score(client: httpx.Client, case: Case, content: bytes) -> tuple[bool, float, float] | None:
    """Laporan guardrails langsung dari service-nya: (passed, probability_bad, ambang)."""
    response = client.post(
        f"{smoke.URLS['guardrails']}/v1/guardrails/check",
        headers=smoke.HEADERS,
        data={
            "request_id": f"E2E_GR_{case.file}",
            "threshold": str(GUARDRAILS_THRESHOLD),
            "threshold_target": "reject",
        },
        files={"file": (case.file, content, _content_type(case.file))},
    )
    if response.status_code != 200:
        return None
    data = response.json()["data"]
    document = data.get("document") or {}
    return bool(data.get("passed")), float(document["probability_bad"]), float(document["threshold_used"])


def _check(results: list[tuple[str, bool]], label: str, passed: bool, detail: str = "") -> bool:
    print(f"    {'ok   ' if passed else 'GAGAL'} {label}{f'  ({detail})' if detail else ''}")
    results.append((label, passed))
    return passed


def _fields(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Setiap field kontrak di `data`, dengan nama berindeks untuk field anggota."""
    fields = [(name, data.get(name) or {}) for name in ("no_kk", "nama_kepala_keluarga")]
    for index, row in enumerate(data.get("anggota_keluarga") or []):
        fields += [(f"anggota[{index}].{name}", row.get(name) or {}) for name in sorted(row)]
    return fields


def _shape_ok(data: dict[str, Any]) -> tuple[bool, str]:
    """Sembilan field kontrak, dan setiap field tepat `{value: str, confidence: 0..1}`; bukan `bin`/`auto`."""
    if set(data) != DOC_KEYS:
        return False, f"key data {sorted(data)}"
    for index, row in enumerate(data["anggota_keluarga"]):
        if set(row) != MEMBER_KEYS:
            return False, f"anggota[{index}] key {sorted(row)}"
    for name, field in _fields(data):
        if set(field) != {"value", "confidence"}:
            return False, f"{name} punya key {sorted(field)}"
        confidence = field["confidence"]
        if not isinstance(field["value"], str) or not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
            return False, f"{name} = {field}"
    return True, ""


def _print_data(data: dict[str, Any]) -> None:
    for name, field in _fields(data):
        print(f"      {name:<48} {field.get('value')!r:<40} confidence={field.get('confidence')}")


def positive(client: httpx.Client, case: Case, content: bytes, results: list[tuple[str, bool]]) -> None:
    expected = case.positive
    assert expected is not None
    request_id = smoke._rid()
    body = _submit(client, request_id, case, content)
    print(
        f"  POST -> {body.get('status_code')} errors={body.get('errors')} "
        f"stage={body.get('pipeline_last_stage')} guardrails={body.get('guardrails')}"
    )
    completed = (
        body.get("status_code") == 200
        and body.get("errors") is None
        and body.get("pipeline_last_stage") is None
        and body.get("guardrails") == 0
    )
    if not _check(results, f"{case.file}: 200 selesai sampai scoring", completed, body.get("message") or ""):
        return
    data = body["data"]
    _print_data(data)

    shape, why = _shape_ok(data)
    _check(results, f"{case.file}: bentuk data {{value, confidence 0..1}}", shape, why)
    members = data.get("anggota_keluarga") or []
    values = {
        "no_kk": (data["no_kk"]["value"], expected.no_kk),
        "nama_kepala_keluarga": (data["nama_kepala_keluarga"]["value"], expected.kepala),
        "jumlah anggota": (len(members), expected.members),
    }
    if expected.first_nik:
        values["nik anggota pertama"] = ((members[0]["nik"]["value"] if members else None), expected.first_nik)
    for name, (got, want) in values.items():
        _check(results, f"{case.file}: {name}", got == want, f"dapat {got!r}, harap {want!r}")

    status = smoke._status(client, request_id)
    _check(
        results,
        f"{case.file}: GET menjawab data yang sama dengan POST",
        status.status_code == 200 and status.json().get("data") == data,
        f"GET {status.status_code}",
    )

    # Ambang 0 untuk kesembilan field: setiap field yang terisi harus ber-confidence 1.
    zero = json.dumps(dict.fromkeys(CONTRACT_FIELDS, 0))
    relaxed = _submit(client, smoke._rid(), case, content, column_confidence_threshold=zero)
    wrong = [
        name
        for name, field in _fields(relaxed.get("data") or {})
        if field.get("confidence") != (1 if field.get("value") else 0)
    ]
    _check(
        results,
        f"{case.file}: column_confidence_threshold=0 -> setiap field terisi confidence 1",
        relaxed.get("status_code") == 200 and not wrong,
        f"status {relaxed.get('status_code')}, tidak sesuai: {wrong[:4]}" if wrong else "",
    )


def negative(client: httpx.Client, case: Case, content: bytes, results: list[tuple[str, bool]]) -> None:
    request_id = smoke._rid()
    body = _submit(client, request_id, case, content)
    print(
        f"  POST -> {body.get('status_code')} errors={body.get('errors')} stage={body.get('pipeline_last_stage')} "
        f"guardrails={body.get('guardrails')} message={body.get('message')!r}"
    )
    _check(
        results,
        f"{case.file}: 400 ditolak guardrails",
        body.get("status_code") == 400
        and body.get("errors") == "DOWNSTREAM_VALIDATION_ERROR"
        and body.get("pipeline_last_stage") == "guardrails"
        and body.get("guardrails") == 1
        and body.get("data") is None,
    )
    # GET membaca putusan yang tersimpan di nilam_guardrails_results, jadi jawabannya sama dengan POST-nya.
    status = smoke._status(client, request_id)
    read_back = status.json()
    same = ("status_code", "errors", "pipeline_last_stage", "guardrails", "data")
    _check(
        results,
        f"{case.file}: GET menjawab penolakan yang sama dari nilam_guardrails_results",
        status.status_code == 400 and all(read_back.get(key) == body.get(key) for key in same),
        f"GET {status.status_code} {read_back.get('errors')} stage={read_back.get('pipeline_last_stage')}",
    )

    bypass = _submit(client, smoke._rid(), case, content, pipeline_name_sequence=NO_GUARDRAILS)
    outcome = f"{bypass.get('status_code')} {bypass.get('errors')} stage={bypass.get('pipeline_last_stage')}"
    print(f"  tanpa guardrails -> {outcome}")
    rejected = (
        bypass.get("status_code") == 400
        and bypass.get("errors") == "DOWNSTREAM_VALIDATION_ERROR"
        and bypass.get("pipeline_last_stage") == "structuring"
    )
    if case.structuring_rejects is None:
        verdict = "ditolak structuring" if rejected else f"LOLOS ({outcome})"
        note = f"{case.file} tanpa guardrails: {verdict}; hanya guardrails yang menjaga kasus {case.label}"
        print(f"    CATATAN {note}")
        notes.append(note)
    else:
        _check(results, f"{case.file}: tanpa guardrails ditolak structuring (§7.4)", rejected, outcome)


def main() -> int:
    missing = [case.file for case in CASES if not os.path.isfile(os.path.join(SAMPLES_DIR, case.file))]
    if missing:
        print(f"berkas uji tidak ada di {os.path.abspath(SAMPLES_DIR)}: {', '.join(missing)}")
        return 2

    results: list[tuple[str, bool]] = []
    with httpx.Client(timeout=180.0) as client:
        for name, url in smoke.URLS.items():
            print(f"health {name}:", client.get(f"{url}/health").json()["backends"])
        if not smoke._all_local(client):
            return 2

        for case in CASES:
            content = _read(case.file)
            print(f"\n== {case.file}: {case.label} ==")
            score = _guardrails_score(client, case, content)
            if score:
                passed, probability_bad, threshold = score
                print(
                    f"  guardrails: passed={passed} probability_bad={probability_bad:.4f} "
                    f"ambang={threshold} selisih={probability_bad - threshold:+.4f}"
                )
            try:
                (positive if case.positive else negative)(client, case, content, results)
            except Exception as exc:  # satu kasus yang meledak tidak boleh menyembunyikan kasus lain
                _check(results, f"{case.file}: EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    smoke.cleanup()
    failed = [label for label, passed in results if not passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} pemeriksaan lulus")
    for label in failed:
        print(f"  GAGAL {label}")
    for note in notes:
        print(f"  CATATAN {note}")
    print("HASIL:", "GAGAL" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

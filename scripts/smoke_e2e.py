import io
import json
import math
import os
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from ocr_common.kk import DOC_FIELDS
from ocr_common.synthetic_kk import member, nomor_kk


def _key_from_env_file() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "services", "ekstraksi", ".env")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "changeme"


URLS = {
    "orchestrator": os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8040"),
    "guardrails": os.environ.get("GUARDRAILS_URL", "http://127.0.0.1:8041"),
    "ekstraksi": os.environ.get("EKSTRAKSI_URL", "http://127.0.0.1:8042"),
    "structuring": os.environ.get("STRUCTURING_URL", "http://127.0.0.1:8043"),
    "scoring": os.environ.get("SCORING_URL", "http://127.0.0.1:8044"),
}
API_KEY = os.environ.get("API_KEY") or _key_from_env_file()
HEADERS = {"X-API-Key": API_KEY}
CALLBACK_PORT = int(os.environ.get("SMOKE_CALLBACK_PORT") or 0)
TIMEOUT_SECONDS = float(os.environ.get("SMOKE_TIMEOUT_SECONDS") or 60)
LATENCY_RUNS = int(os.environ.get("SMOKE_LATENCY_RUNS") or 0)
STAGES = ("ekstraksi", "structuring", "scoring")

callbacks: list[dict] = []


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (nama method ditentukan http.server)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        callbacks.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _image() -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return b"\xff\xd8fake-jpeg-bytes"
    head = member(0)
    image = Image.new("RGB", (1000, 620), "white")
    draw = ImageDraw.Draw(image)
    try:
        big, small = ImageFont.truetype("arial.ttf", 34), ImageFont.truetype("arial.ttf", 28)
    except OSError:
        big = small = ImageFont.load_default()
    lines = [
        ("KARTU KELUARGA", big),
        (f"No. {nomor_kk()}", big),
        (f"Nama Kepala Keluarga : {head.nama_lengkap}", small),
        ("Alamat : JL. MERDEKA NO. 12", small),
        ("Desa/Kelurahan : CIHAPIT      RT/RW : 003/007", small),
        ("Kecamatan : BANDUNG WETAN    Kode Pos : 40114", small),
        (f"{head.nama_lengkap}  {head.nik}  {head.status_hubungan_dalam_keluarga}", small),
        (f"{head.ayah} / {head.ibu}", small),
    ]
    for i, (text, font) in enumerate(lines):
        draw.text((40, 40 + i * 68), text, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _submit(client: httpx.Client, request_id: str, filename: str, content: bytes) -> httpx.Response:
    return client.post(
        f"{URLS['orchestrator']}/v1/extract-ocr",
        headers=HEADERS,
        data={"request_id": request_id, "document_type": "kk"},
        files={"file": (filename, content, "image/jpeg")},
    )


def _status(client: httpx.Client, request_id: str) -> httpx.Response:
    return client.get(f"{URLS['orchestrator']}/v1/extract-ocr/{request_id}", headers=HEADERS)


def _poll(client: httpx.Client, request_id: str) -> dict[str, dict]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    jobs: dict[str, dict] = {}
    while time.monotonic() < deadline:
        for stage in ("ekstraksi", "structuring", "scoring"):
            response = client.get(f"{URLS[stage]}/v1/{stage}/jobs/{request_id}", headers=HEADERS)
            if response.status_code == 200:
                jobs[stage] = response.json()["data"]
        statuses = {stage: job["status"] for stage, job in jobs.items()}
        if statuses.get("scoring") in {"DONE", "FAILED"} or "FAILED" in statuses.values():
            break
        time.sleep(0.3)
    return jobs


def async_pipeline(client: httpx.Client) -> bool:
    print("== pipeline async ==")
    request_id = f"REQ_{uuid.uuid4()}"
    image = _image()
    print("request_id:", request_id)

    started = time.monotonic()
    submitted = _submit(client, request_id, "kk.jpg", image)
    elapsed = time.monotonic() - started
    body = submitted.json()
    print(
        f"orchestrator extract-ocr: {submitted.status_code} dalam {elapsed:.2f}s job_status={body.get('job_status')} "
        f"guardrails={body.get('guardrails')} errors={body.get('errors')} message={body.get('message')!r}"
    )
    if submitted.status_code not in (200, 202):
        print("  pipeline tidak dimulai atau gagal")
        return False
    finished_in_time = submitted.status_code == 200 and body.get("job_status") == "completed"
    if finished_in_time:
        print("  selesai dalam waktu tunggu; data di respons 200:")
        for name, field in body["data"].items():
            print(f"    {name:<12} {field['value']!r:<35} confidence={field['confidence']}")
    else:
        print("  belum selesai saat waktu tunggu habis (202); lanjut polling")

    jobs = _poll(client, request_id)
    for stage in ("ekstraksi", "structuring", "scoring"):
        job = jobs.get(stage)
        print(f"  {stage:<12} {job['status'] if job else '(belum ada job)'} {(job or {}).get('error_message') or ''}")
    ok = all(jobs.get(stage, {}).get("status") == "DONE" for stage in STAGES)
    if finished_in_time:
        # nomor_kk di dalam, no_kk di luar: itu satu dari dua penggantian nama §3.3.1, dan
        # memeriksanya di sini adalah satu-satunya tempat kedua sisi proyeksi dilihat bersama.
        read = jobs["structuring"]["result"]["nomor_kk"]["value"]
        ok = ok and body["data"]["no_kk"]["value"] == read
    if ok:
        structured = jobs["structuring"]["result"]
        for name in DOC_FIELDS:
            field = structured.get(name) or {}
            print(f"  {name:<24} {field.get('value')!r:<32} ocr={field.get('ocr_conf')} crf={field.get('crf_conf')}")
        members = structured.get("anggota_keluarga") or []
        print(f"  anggota_keluarga: {len(members)} orang")
        score = jobs["scoring"]["result"]
        print(f"  skor dokumen: {score.get('fields')}")
        print(f"  skor anggota: {len(score.get('anggota_keluarga') or [])} baris")
        ok = ok and len(members) == len(score.get("anggota_keluarga") or [])

    status = _status(client, request_id)
    read_back = status.json()
    print(
        f"GET status -> {status.status_code} job_status={read_back.get('job_status')} params={read_back.get('params')}"
    )
    ok = ok and status.status_code == 200 and read_back.get("job_status") == "completed"
    if finished_in_time:
        ok = ok and read_back["data"] == body["data"]

    again = _submit(client, request_id, "kk.jpg", image)
    repeated = again.json()
    print(f"kirim ulang request_id yang sama -> {again.status_code} job_status={repeated.get('job_status')}")
    ok = ok and again.status_code == 200 and repeated.get("job_status") == "completed"
    if finished_in_time:
        ok = ok and repeated["data"] == body["data"]

    if CALLBACK_PORT:
        time.sleep(1.0)
        mine = [(c["stage"], c["status"]) for c in callbacks if c.get("request_id") == request_id]
        print("callback diterima:", mine)
        expected = [("OCR", "DONE"), ("STRUCTURING", "DONE"), ("SCORING", "DONE")]
        ok = ok and mine == expected
        final = next((c for c in callbacks if c.get("request_id") == request_id and c["stage"] == "SCORING"), None)
        if final:
            print("hasil akhir (callback SCORING):", json.dumps(final["result"])[:300])
    else:
        print("callback tidak diperiksa (SMOKE_CALLBACK_PORT tidak di-set)")
    return ok


def _stage_seconds(job: dict) -> float:
    return (datetime.fromisoformat(job["updated_at"]) - datetime.fromisoformat(job["created_at"])).total_seconds()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def latency(client: httpx.Client, runs: int) -> bool:
    print(f"== latensi end-to-end, {runs} request ==")
    image = _image()
    totals: list[float] = []
    codes: Counter[int] = Counter()
    per_stage: dict[str, list[float]] = {stage: [] for stage in STAGES}
    failed = 0
    for _ in range(runs):
        request_id = f"REQ_{uuid.uuid4()}"
        started = time.monotonic()
        response = _submit(client, request_id, "kk.jpg", image)
        answered = time.monotonic() - started
        codes[response.status_code] += 1
        jobs = _poll(client, request_id)
        if not all(jobs.get(stage, {}).get("status") == "DONE" for stage in STAGES):
            failed += 1
            continue
        totals.append(answered if response.status_code == 200 else time.monotonic() - started)
        for stage in STAGES:
            per_stage[stage].append(_stage_seconds(jobs[stage]))
    print("  HTTP orchestrator:", ", ".join(f"{code} x{count}" for code, count in sorted(codes.items())))
    if totals:
        print(
            f"  end-to-end (klien): p50={_percentile(totals, 0.5):.2f}s  p95={_percentile(totals, 0.95):.2f}s  "
            f"max={max(totals):.2f}s"
        )
        print(
            "  per tahap (server, p50): "
            + ", ".join(f"{stage} {_percentile(values, 0.5):.2f}s" for stage, values in per_stage.items())
        )
    print(f"  gagal: {failed}")
    return failed == 0


def guardrails_reject(client: httpx.Client) -> bool:
    """Butuh GUARDRAILS_BACKEND=mock: model mock menolak nama file yang mengandung `notkk`."""
    print("== guardrails menolak ==")
    request_id = f"REQ_{uuid.uuid4()}"
    response = _submit(client, request_id, "notkk.jpg", _image())
    body = response.json()
    print(f"notkk.jpg -> {response.status_code} errors={body.get('errors')} guardrails={body.get('guardrails')}")
    print(f"  message={body.get('message')!r}")
    status = _status(client, request_id)
    print(f"  GET status -> {status.status_code} (tidak ada tahap yang jalan)")
    return (
        response.status_code == 400
        and body.get("errors") == "DOWNSTREAM_VALIDATION_ERROR"
        and status.status_code == 404
    )


def main() -> int:
    if CALLBACK_PORT:
        server = ThreadingHTTPServer(("0.0.0.0", CALLBACK_PORT), _CallbackHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"menerima callback di port {CALLBACK_PORT}")

    with httpx.Client(timeout=60.0) as client:
        for name, url in URLS.items():
            print(f"health {name}:", client.get(f"{url}/health").json()["backends"])
        results = [async_pipeline(client), guardrails_reject(client)]
        if LATENCY_RUNS:
            results.append(latency(client, LATENCY_RUNS))
    print("HASIL:", "OK" if all(results) else "GAGAL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

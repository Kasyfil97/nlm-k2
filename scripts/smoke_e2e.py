"""Smoke test ujung ke ujung terhadap stack compose lokal.

Ia menjalankan **delapan jalur**, bukan tujuh. §7.4 punya tiga aturan penolakan, dan aturan 2 dan 3
mengembalikan `reject_reason` yang sama -- jadi respons 400-nya tidak bisa membedakan keduanya, dan
satu-satunya tempat bedanya terlihat adalah isi hasil di `GET /v1/structuring/jobs/{request_id}`.
Menguji dua dari tiga akan meninggalkan aturan yang mana pun yang rusak tidak terdeteksi.

Semua pemicu lewat nama berkas, dan sampai ke tahap yang dituju lewat backend `mock`:

| Jalur | Pemicu | Harapan |
|---|---|---|
| lengkap | `kk.jpg` | 200, sembilan field §3.3, `GET` identik |
| jumlah anggota | `kk-MOCK:members=4.jpg` | 200 dengan 4 anggota, confidence milik masing-masing |
| menunggu habis | `kk-delay20s.jpg` | 202, lalu `GET` 200 dengan sembilan field yang sama |
| guardrails | `notkk.jpg` | 400 §3.5, dan `GET` berikutnya 400 yang sama (dari `guardrails_results`) |
| §7.4 aturan 1 | `kk-blank.jpg` | 400, `texts` kosong |
| §7.4 aturan 2 | `kk-MOCK:blank_kk=1.jpg` | 400, `nomor_kk` kosong di hasil |
| §7.4 aturan 3 | `kk-MOCK:members=0.jpg` | 400, `nomor_kk` terisi tetapi nol anggota |
| tahap gagal | `kk-servererror.jpg` | 422 `OCR_FAILED` |

Ditambah satu jalur integrasi: sebuah handoff yang jadi dead letter menandai tabel outcome `failed`
dengan `<TAHAP BERIKUTNYA>_FAILED`, dan barisnya bisa dilepas lewat `POST /v1/<tahap>/outbox/release`.

R25a: menolak jalan di luar `ENVIRONMENT=local`, dan membersihkan baris yang ditinggalkannya --
**termasuk baris tabel outcome**, karena di produksi tabel itu milik Orkestrasi pusat dan dibagi
dengan mereka.
"""

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

#: Nama tahap sebagaimana tertulis di baris job dan baris outcome, per nama service.
STAGE_NAMES = {"ekstraksi": "OCR", "structuring": "STRUCTURING", "scoring": "SCORING"}

def _psycopg_url(url: str) -> str:
    """Buang sufiks driver SQLAlchemy supaya psycopg bisa membacanya.

    Seluruh repo ini menulis `DATABASE_URL` dalam bentuk SQLAlchemy
    (`postgresql+asyncpg://...`) -- itu yang ada di setiap `.env` dan di compose -- jadi menyalin
    nilainya ke `SMOKE_DATABASE_URL` adalah hal yang paling wajar dilakukan. psycopg memakai libpq
    dan menolak bentuk itu dengan `missing "=" after ...`.

    Kenapa ini bukan sekadar kenyamanan: galatnya muncul di `cleanup()`, **setelah** semua jalur
    uji selesai dan baris-barisnya dibuat. Jadi kegagalannya persis kebalikan dari maksud skrip ini
    -- ia meninggalkan baris uji di tabel outcome, yang di produksi milik Orkestrasi pusat.
    """
    prefix, sep, rest = url.partition("://")
    return f"{prefix.split('+', 1)[0]}{sep}{rest}" if sep else url


#: Dipakai jalur dead letter dan pembersihan. Kosong = keduanya dilewati, bukan gagal: keduanya
#: butuh akses langsung ke database compose, yang tidak selalu ada dari tempat skrip ini jalan.
DATABASE_URL = _psycopg_url(os.environ.get("SMOKE_DATABASE_URL", ""))
OUTCOME_TABLE = os.environ.get("ORCHESTRATION_OUTCOME_TABLE", "orchestration_extract_ocr")

#: Setiap request_id yang dibuat skrip ini, supaya barisnya bisa dihapus lagi di akhir.
minted: list[str] = []

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


def _print_contract_data(data: dict[str, Any]) -> None:
    """Sembilan field §3.3.1. `anggota_keluarga` adalah LIST, bukan objek `{value, confidence}` --
    memperlakukannya seperti dua field dokumen di atasnya adalah cara membaca bentuk dokumen lama ke
    dalam bentuk KK, dan itulah persis yang dilakukan versi pertama skrip ini sampai ia dijalankan."""
    for name in ("no_kk", "nama_kepala_keluarga"):
        field = data.get(name) or {}
        print(f"    {name:<22} {field.get('value')!r:<34} confidence={field.get('confidence')}")
    for index, row in enumerate(data.get("anggota_keluarga") or []):
        nama, nik = row["nama_lengkap"], row["nik"]
        print(f"    [{index}] {nama['value']!r:<22} nik={nik['value']} conf={nik['confidence']}")


def _rid() -> str:
    """Satu request_id, dicatat supaya pembersihan tahu baris mana miliknya."""
    request_id = f"REQ_{uuid.uuid4()}"
    minted.append(request_id)
    return request_id


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
    request_id = _rid()
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
        _print_contract_data(body["data"])
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
        request_id = _rid()
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
    request_id = _rid()
    response = _submit(client, request_id, "notkk.jpg", _image())
    body = response.json()
    print(f"notkk.jpg -> {response.status_code} errors={body.get('errors')} guardrails={body.get('guardrails')}")
    print(f"  message={body.get('message')!r}")
    # Tidak ada tahap yang jalan, tapi putusannya tersimpan di guardrails_results: GET menjawab 400 yang sama.
    status = _status(client, request_id)
    print(f"  GET status -> {status.status_code} errors={status.json().get('errors')}")
    return (
        response.status_code == 400
        and body.get("errors") == "DOWNSTREAM_VALIDATION_ERROR"
        and status.status_code == 400
        and status.json().get("pipeline_last_stage") == "guardrails"
    )


# --- §7.4: tiga aturan, tiga jalur --------------------------------------------------------


def _structuring_result(client: httpx.Client, request_id: str) -> dict[str, Any] | None:
    """Hasil structuring yang tersimpan. Penolakan adalah job `DONE` dengan `reject_reason` di dalam
    hasilnya -- bukan status job tersendiri -- jadi hasilnya tetap bisa dibaca setelah 400."""
    response = client.get(f"{URLS['structuring']}/v1/structuring/jobs/{request_id}", headers=HEADERS)
    if response.status_code != 200:
        return None
    return response.json()["data"].get("result")


def _rejected(client: httpx.Client, label: str, filename: str) -> tuple[bool, dict[str, Any] | None]:
    """Kirim satu dokumen yang seharusnya ditolak gerbang keabsahan, dan kembalikan hasilnya."""
    request_id = _rid()
    response = _submit(client, request_id, filename, _image())
    body = response.json()
    print(
        f"  {label}: {filename} -> {response.status_code} "
        f"errors={body.get('errors')} guardrails={body.get('guardrails')}"
    )
    print(f"    message={body.get('message')!r}")
    ok = (
        response.status_code == 400
        and body.get("errors") == "DOWNSTREAM_VALIDATION_ERROR"
        and body.get("guardrails") == 1
        and body.get("data") is None
    )
    result = _structuring_result(client, request_id)
    if result is None:
        print("    hasil structuring tidak terbaca -- penolakan seharusnya tetap job DONE (§7.4)")
        return False, None
    return ok, result


def validity_gate(client: httpx.Client) -> bool:
    """Ketiga aturan §7.4, bukan dua.

    Aturan 2 dan 3 memakai `reject_reason` yang sama persis, jadi respons 400-nya tidak membedakan
    keduanya. Yang membedakan adalah isi hasil structuring: aturan 2 berarti `nomor_kk` kosong,
    aturan 3 berarti `nomor_kk` terisi tetapi tidak ada satu pun anggota dengan NIK dan nama. Kalau
    jalur ini hanya memeriksa status dan pesan, salah satu dari kedua aturan bisa mati tanpa
    ketahuan -- dan yang mati akan meloloskan dokumen ke tahap berikutnya.
    """
    print("== gerbang keabsahan KK (§7.4) ==")
    results: list[bool] = []

    ok, result = _rejected(client, "aturan 1, tanpa teks terbaca", "kk-blank.jpg")
    if result is not None:
        empty_doc = all(not (result.get(name) or {}).get("value") for name in DOC_FIELDS)
        no_members = not (result.get("anggota_keluarga") or [])
        print(f"    aturan 1: semua field kosong={empty_doc} anggota={len(result.get('anggota_keluarga') or [])}")
        ok = ok and empty_doc and no_members
    results.append(ok)

    ok, result = _rejected(client, "aturan 2, nomor KK hilang", "kk-MOCK:blank_kk=1.jpg")
    if result is not None:
        blank_kk = not (result.get("nomor_kk") or {}).get("value")
        has_members = bool(result.get("anggota_keluarga"))
        print(f"    aturan 2: nomor_kk kosong={blank_kk} anggota={len(result.get('anggota_keluarga') or [])}")
        # Anggota HARUS ada di sini, kalau tidak aturan 3 yang menyala dan jalur ini diam-diam
        # menguji aturan yang sama dua kali.
        ok = ok and blank_kk and has_members
    results.append(ok)

    ok, result = _rejected(client, "aturan 3, nol anggota", "kk-MOCK:members=0.jpg")
    if result is not None:
        filled_kk = bool((result.get("nomor_kk") or {}).get("value"))
        no_members = not (result.get("anggota_keluarga") or [])
        print(f"    aturan 3: nomor_kk terisi={filled_kk} anggota={len(result.get('anggota_keluarga') or [])}")
        ok = ok and filled_kk and no_members
    results.append(ok)

    return all(results)


# --- jumlah anggota yang bukan bawaan -----------------------------------------------------


def member_count(client: httpx.Client, members: int = 4) -> bool:
    """Sebuah KK dengan jumlah anggota berbeda dari fiksi bawaan tetap menghasilkan
    `data.anggota_keluarga` sepanjang itu, dengan confidence yang benar-benar milik tiap anggota.

    Array anggota berukuran variabel adalah perbedaan struktural terbesar antara KK dan dokumen
    bernilai tunggal, dan pergeseran indeks satu langkah akan menghasilkan 200 yang tampak benar
    sambil membawa confidence milik orang lain. Karena itu yang diperiksa bukan hanya panjangnya,
    tetapi bahwa NIK setiap anggota di `data` sama dengan NIK anggota pada posisi yang sama di hasil
    structuring.
    """
    print(f"== anggota keluarga: {members} orang ==")
    request_id = _rid()
    response = _submit(client, request_id, f"kk-MOCK:members={members}.jpg", _image())
    body = response.json()
    if response.status_code != 200:
        _poll(client, request_id)
        response = _status(client, request_id)
        body = response.json()
    data = body.get("data") or {}
    rows = data.get("anggota_keluarga") or []
    print(f"  {response.status_code} anggota={len(rows)} (diminta {members})")
    if len(rows) != members:
        return False

    result = _structuring_result(client, request_id) or {}
    source = result.get("anggota_keluarga") or []
    aligned = len(source) == members and all(
        rows[i]["nik"]["value"] == (source[i].get("nik") or {}).get("value") for i in range(members)
    )
    print(f"  urutan cocok dengan hasil structuring: {aligned}")
    for i, row in enumerate(rows):
        nik = row["nik"]
        print(f"    [{i}] {row['nama_lengkap']['value']!r:<22} nik={nik['value']} conf={nik['confidence']}")
    return aligned


# --- menunggu habis: 202, lalu GET yang sama -----------------------------------------------


def wait_timeout(client: httpx.Client) -> bool:
    """Pipeline melampaui `PIPELINE_WAIT_SECONDS` -> 202, dan `GET` setelah selesai -> 200 dengan
    sembilan field yang sama. Dua endpoint, satu kontrak: kalau bentuknya berbeda, pemanggil yang
    menempuh jalur 202 mendapat sesuatu yang tidak pernah didapat pemanggil jalur langsung."""
    print("== waktu tunggu habis -> 202 ==")
    request_id = _rid()
    # Harus DI ATAS PIPELINE_WAIT_SECONDS, yang bawaannya 30 (kontrak §13). Nilai 20 di sini pernah
    # membuat jalur ini diam-diam menguji jalur 200: pipeline selesai dalam anggaran, jadi yang
    # diperiksa bukan 202 melainkan pipeline yang lambat sedikit.
    delay = int(os.environ.get("SMOKE_DELAY_SECONDS") or 40)
    started = time.monotonic()
    response = _submit(client, request_id, f"kk-delay{delay}s.jpg", _image())
    body = response.json()
    print(f"  {response.status_code} dalam {time.monotonic() - started:.1f}s job_status={body.get('job_status')}")
    if response.status_code != 202 or body.get("job_status") != "processing":
        print("    bukan 202: naikkan SMOKE_DELAY_SECONDS di atas PIPELINE_WAIT_SECONDS")
        return False
    if body.get("data") is not None:
        return False

    _poll(client, request_id)
    after = _status(client, request_id)
    finished = after.json()
    print(f"  GET setelah selesai -> {after.status_code} job_status={finished.get('job_status')}")
    if after.status_code != 200 or finished.get("job_status") != "completed":
        return False
    data = finished.get("data") or {}
    shape = set(data) == {"no_kk", "nama_kepala_keluarga", "anggota_keluarga"}
    print(f"  sembilan field §3.3 hadir: {shape}")
    return shape


# --- tahap gagal: 422 ----------------------------------------------------------------------


def stage_failure(client: httpx.Client) -> bool:
    """Model yang meledak adalah job `FAILED` dan 422, bukan penolakan. Bedanya penting bagi
    pemanggil: 400 berarti jangan kirim ulang dokumen ini, 422 dari tahap berarti coba lagi."""
    print("== tahap gagal -> 422 ==")
    request_id = _rid()
    response = _submit(client, request_id, "kk-servererror.jpg", _image())
    body = response.json()
    if response.status_code == 202:
        _poll(client, request_id)
        response = _status(client, request_id)
        body = response.json()
    print(f"  {response.status_code} errors={body.get('errors')} job_status={body.get('job_status')}")
    print(f"    message={body.get('message')!r}")
    return (
        response.status_code == 422
        and str(body.get("errors", "")).endswith("_FAILED")
        and body.get("job_status") == "failed"
        and body.get("data") is None
    )


# --- handoff yang jadi dead letter ---------------------------------------------------------


def dead_letter(client: httpx.Client) -> bool:
    """Handoff yang mati menandai baris outcome `failed` + `<TAHAP BERIKUTNYA>_FAILED`, dan
    barisnya bisa dilepas lewat `POST /v1/<tahap>/outbox/release`.

    Pemicunya **hanya lewat env**: jalankan stack dengan `PIPELINE_OUTBOX_MAX_AGE_SECONDS` kecil dan
    tahap penerima dimatikan. Tidak ada hook baru di `ocr_common` untuk ini -- R8 melarang mekanisme
    baru mendarat di sana, dan outbox tinggal di sana. Konsekuensinya jalur ini tidak bisa memicu
    keadaannya sendiri: ia memeriksa keadaan yang sudah ada, dan melewati diri sendiri kalau tidak ada.
    """
    print("== handoff dead letter ==")
    found = False
    for stage in STAGES:
        response = client.get(f"{URLS[stage]}/v1/{stage}/outbox", headers=HEADERS)
        if response.status_code != 200:
            print(f"  {stage}: outbox tidak terbaca ({response.status_code})")
            continue
        data = response.json()["data"]
        if not data.get("enabled"):
            print(f"  {stage}: PIPELINE_OUTBOX mati")
            continue
        dead = data.get("dead_letters") or 0
        print(f"  {stage}: pending={data.get('pending')} retrying={data.get('retrying')} dead_letters={dead}")
        if not dead:
            continue
        found = True
        released = client.post(f"{URLS[stage]}/v1/{stage}/outbox/release", headers=HEADERS)
        print(f"    release -> {released.status_code} {released.json().get('data')}")
        if released.status_code != 200:
            return False

    if not found:
        print(
            "  tidak ada dead letter: jalur ini butuh stack yang dijalankan dengan "
            "PIPELINE_OUTBOX_MAX_AGE_SECONDS kecil dan tahap penerima dimatikan (R8: tanpa hook baru). "
            "DILEWATI, bukan lulus."
        )
    return True


# --- tabel outcome -------------------------------------------------------------------------


def _outcome_rows(request_ids: list[str]) -> list[dict[str, Any]]:
    """Baris tabel outcome untuk request_id yang diberikan; daftar kosong tanpa SMOKE_DATABASE_URL."""
    if not DATABASE_URL or not request_ids:
        return []
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"SELECT request_id, downstream_status, downstream_stage, error_code FROM {OUTCOME_TABLE} "
            "WHERE request_id = ANY(%s) ORDER BY request_id",
            (request_ids,),
        )
        columns = [column.name for column in cursor.description or []]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def outcome_table(client: httpx.Client) -> bool:
    """Keadaan akhir setiap request muncul di tabel outcome. Itu satu-satunya kanal yang dibaca
    Orkestrasi pusat, jadi sebuah request yang selesai bersih tetapi tidak menulis barisnya adalah
    request yang, dari sisi mereka, tidak pernah selesai."""
    print("== tabel outcome ==")
    if not DATABASE_URL:
        print("  SMOKE_DATABASE_URL tidak di-set; DILEWATI, bukan lulus")
        return True
    rows = _outcome_rows(minted)
    seen = {row["request_id"]: row for row in rows}
    missing = [rid for rid in minted if rid not in seen]
    for row in rows:
        print(
            f"  {row['request_id'][:20]}… {row['downstream_status']:<10} "
            f"{row['downstream_stage'] or '-':<12} {row['error_code'] or ''}"
        )
    print(f"  baris: {len(rows)} dari {len(minted)} request; tanpa baris: {len(missing)}")
    states = {row["downstream_status"] for row in rows}
    known = states <= {"completed", "failed", "processing"}
    # Tepat SATU request tidak boleh punya baris: yang ditolak guardrails. §2.6 -- pipeline tidak
    # pernah dimulai, jadi tidak ada tahap yang menulis. Diperiksa sebagai angka, bukan sekadar
    # dicetak: tanpa ini, tahap yang berhenti menulis barisnya sama sekali akan lolos di sini.
    only_guardrails = len(missing) == 1
    print(f"  keadaan: {sorted(states)}; tepat satu tanpa baris (penolakan guardrails): {only_guardrails}")
    return known and only_guardrails


def cleanup() -> None:
    """Hapus baris yang dibuat jalannya skrip ini, termasuk baris tabel outcome.

    Tabel outcome adalah satu-satunya di sini yang **tidak** dimiliki repo ini: di produksi ia
    dibagi dengan Orkestrasi pusat. Meninggalkan baris tes di sana berarti menaruh sampah di tabel
    tim lain, dan itu sebabnya `TESTING_ENDPOINTS` sengaja tidak menulisnya sama sekali.
    """
    if not DATABASE_URL or not minted:
        return
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {OUTCOME_TABLE} WHERE request_id = ANY(%s)", (minted,))
        outcome_rows = cursor.rowcount
        # Urutannya wajib: `*_results.request_id` punya foreign key ke `*_jobs`, jadi hasil dulu,
        # baru jobnya. `pipeline_outbox` berdiri sendiri. Baris job ikut dihapus -- versi pertama
        # hanya menghapus hasilnya, dan meninggalkan 21/18/9 baris job setelah satu kali jalan.
        stage_rows = 0
        for table in (
            "ocr_results",
            "structuring_results",
            "scoring_results",
            "ocr_jobs",
            "structuring_jobs",
            "scoring_jobs",
            "pipeline_outbox",
            # Ditulis orchestrator untuk setiap putusan guardrails, termasuk yang menolak.
            "guardrails_results",
        ):
            try:
                cursor.execute(f"DELETE FROM {table} WHERE request_id = ANY(%s)", (minted,))
                stage_rows += cursor.rowcount
            except psycopg.errors.UndefinedTable:
                connection.rollback()
        connection.commit()
    print(
        f"bersih-bersih: {outcome_rows} baris tabel outcome dan {stage_rows} baris tahap dihapus "
        f"untuk {len(minted)} request"
    )


def main() -> int:
    if CALLBACK_PORT:
        server = ThreadingHTTPServer(("0.0.0.0", CALLBACK_PORT), _CallbackHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"menerima callback di port {CALLBACK_PORT}")

    with httpx.Client(timeout=90.0) as client:
        for name, url in URLS.items():
            health = client.get(f"{url}/health")
            print(f"health {name}:", health.json()["backends"])
        # R25a: menolak jalan di luar local, dan membacanya dari service yang berjalan, bukan dari
        # environment skrip ini -- yang penting adalah ENVIRONMENT service, bukan milik shell.
        if not _all_local(client):
            return 2

        checks = [
            ("pipeline lengkap", async_pipeline),
            ("jumlah anggota", member_count),
            ("waktu tunggu habis", wait_timeout),
            ("guardrails menolak", guardrails_reject),
            ("gerbang keabsahan §7.4", validity_gate),
            ("tahap gagal", stage_failure),
            ("dead letter", dead_letter),
            ("tabel outcome", outcome_table),
        ]
        results = []
        for label, check in checks:
            try:
                results.append((label, bool(check(client))))
            except Exception as exc:  # satu jalur yang meledak tidak boleh menyembunyikan tujuh lainnya
                print(f"  {label}: EXCEPTION {type(exc).__name__}: {exc}")
                results.append((label, False))
        if LATENCY_RUNS:
            results.append(("latensi", latency(client, LATENCY_RUNS)))

    cleanup()
    print()
    for label, passed in results:
        print(f"  {'OK  ' if passed else 'GAGAL'} {label}")
    ok = all(passed for _, passed in results)
    print("HASIL:", "OK" if ok else "GAGAL")
    return 0 if ok else 1


def _all_local(client: httpx.Client) -> bool:
    """R25a. Skrip ini membuat baris sampah dan menghapusnya lagi, termasuk di tabel yang dimiliki
    tim lain. Menjalankannya terhadap apa pun selain compose lokal adalah cara menghapus baris
    orang lain, jadi pagarnya diperiksa sebelum request pertama, bukan diserahkan ke kehati-hatian."""
    environment = os.environ.get("ENVIRONMENT", "local")
    if environment != "local":
        print(f"MENOLAK JALAN: ENVIRONMENT={environment}. Smoke test ini hanya untuk compose lokal (R25a).")
        return False
    remote = [name for name, url in URLS.items() if "127.0.0.1" not in url and "localhost" not in url]
    if remote:
        print(f"MENOLAK JALAN: {', '.join(remote)} bukan alamat lokal. Hanya untuk compose lokal (R25a).")
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())

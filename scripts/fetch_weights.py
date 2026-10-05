"""Ambil artefak model ke `services/<nama>/weights/`.

Sebuah entri boleh punya **satu berkas** (`target`) atau **beberapa** (`files` + `target_dir`).
K2Quality memaksa bentuk kedua: intinya butuh enam artefak yang harus datang bersama-sama, dan
mengambil lima dari enam menghasilkan service yang menolak start dengan pesan yang benar tetapi
setelah mengunduh 19 MB percuma.

Sumbernya bisa `gs://`, `s3://`, `https://`, **atau sebuah direktori/berkas lokal**. Yang terakhir
bukan kemudahan: hari ini artefak K2Quality memang ada di checkout sebelah, jadi jalur lokal adalah
satu-satunya cara membuat langkah ini bisa diulang sekarang alih-alih setelah seseorang mengunggahnya.

Untuk entri banyak-berkas, URI diperlakukan sebagai **awalan** (K2Quality sendiri memakai
`GCS_MODEL_PREFIX` bawaan `quality_models/kk/`), dan tiap berkas diverifikasi terhadap
`model_hashes.json` yang ikut diambil -- bukan terhadap satu sha di env, yang untuk enam berkas
berarti enam variabel atau tidak ada verifikasi sama sekali.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Jika WEIGHTS_DIR di-set (dalam container), semua artefak mendarat di sana.
# Tanpa WEIGHTS_DIR (lokal), tiap service pakai {ROOT}/services/{service}/weights/.
_WEIGHTS_DIR = os.environ.get("WEIGHTS_DIR")


def _service_weights(service: str, filename: str | None = None) -> Path:
    base = Path(_WEIGHTS_DIR) if _WEIGHTS_DIR else ROOT / "services" / service / "weights"
    return (base / filename) if filename else base

#: Berkas yang dihasilkan ekspor K2Quality. `model_hashes.json` ikut diambil dan dipakai untuk
#: memverifikasi lima lainnya; ia sendiri tidak memuat hash dirinya, jadi tidak diverifikasi.
K2QUALITY_FILES = (
    "blur_cnn_weights.pt",
    "blur_cnn_meta.json",
    "xgb_model.json",
    "calibration.json",
    "feature_names.json",
    "model_hashes.json",
)
MANIFEST = "model_hashes.json"

MODELS: dict[str, dict[str, Any]] = {
    "guardrails": {
        "uri_env": "GUARDRAILS_MODEL_URI",
        "target_dir": _service_weights("guardrails"),
        "files": K2QUALITY_FILES,
        "manifest": MANIFEST,
    },
    "scoring": {
        "uri_env": "SCORING_MODEL_URI",
        "target": _service_weights("scoring", "kk_trust_model.joblib"),
    },
    "extraction": {
        "uri_env": "EXTRACTION_WEIGHTS_URI",
        "target_dir": _service_weights("extraction"),
        "files": ("ppocrv5_server_det.pth", "ppocrv5_server_rec.pth"),
    },
}


def _download(uri: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    local = _local_path(uri)
    if local is not None:
        if not local.is_file():
            raise SystemExit(f"tidak ada: {local}")
        shutil.copyfile(local, target)
        return
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        tmp = Path(handle.name)
    try:
        if uri.startswith("gs://"):
            _download_gcs(uri, tmp)
        elif uri.startswith("s3://"):
            subprocess.run(["mc", "cp", uri[len("s3://") :], str(tmp)], check=True)
        elif uri.startswith(("http://", "https://")):
            with urllib.request.urlopen(uri, timeout=120) as response, open(tmp, "wb") as out:
                shutil.copyfileobj(response, out)
        else:
            raise SystemExit(f"skema URI tidak dikenal: {uri!r} (pakai gs://, s3://, https://, atau path lokal)")
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _download_gcs(uri: str, target: Path) -> None:
    """Unduh satu objek GCS via Python client library; fallback ke subprocess gsutil jika library tidak ada."""
    try:
        from google.cloud import storage as gcs
    except ImportError:
        subprocess.run(["gsutil", "cp", uri, str(target)], check=True)
        return
    without_scheme = uri[len("gs://"):]
    bucket_name, _, blob_name = without_scheme.partition("/")
    client = gcs.Client()
    blob = client.bucket(bucket_name).blob(blob_name)
    blob.download_to_filename(str(target))


def _local_path(uri: str) -> Path | None:
    """`uri` sebagai path lokal, atau None kalau ia URI jarak jauh.

    `file://` ditangani eksplisit; selain itu apa pun yang bukan skema yang dikenal dianggap path.
    Pemeriksaannya pada skema, bukan pada keberadaan berkas, supaya salah ketik pada path lokal
    dilaporkan sebagai berkas yang tidak ada dan bukan sebagai "skema tidak dikenal".
    """
    if uri.startswith("file://"):
        return Path(urllib.request.url2pathname(uri[len("file://") :]))
    if uri.startswith(("gs://", "s3://", "http://", "https://")):
        return None
    return Path(uri)


def _join(uri: str, name: str) -> str:
    """Satu berkas di bawah awalan `uri`, untuk URI jarak jauh maupun direktori lokal."""
    local = _local_path(uri)
    if local is not None:
        return str(local / name)
    return f"{uri.rstrip('/')}/{name}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_one(name: str, spec: dict[str, Any], uri: str, *, force: bool) -> int:
    target: Path = spec["target"]
    if target.is_file() and not force:
        print(f"{name}: {target} sudah ada, lewati (pakai --force untuk unduh ulang)", flush=True)
    else:
        print(f"{name}: mengambil {uri} -> {target}", flush=True)
        _download(uri, target)

    expected = os.environ.get(spec["sha_env"]) if spec.get("sha_env") else None
    if expected:
        actual = _sha256(target)
        if actual != expected.lower():
            target.unlink()
            print(f"{name}: sha256 tidak cocok (dapat {actual}, harap {expected}); berkas dihapus", file=sys.stderr)
            return 1
        print(f"{name}: sha256 cocok", flush=True)
    print(f"{name}: siap ({target.stat().st_size // 1024} KB)", flush=True)
    return 0


def _fetch_many(name: str, spec: dict[str, Any], uri: str, *, force: bool) -> int:
    """Semua berkas entri ini, lalu verifikasi terhadap manifesnya.

    Diambil dulu semuanya baru diverifikasi, bukan satu per satu, karena manifesnya sendiri salah
    satu berkas yang diambil -- dan karena artefak yang tidak lengkap tidak berguna, jadi tidak ada
    gunanya berhenti di tengah dengan tiga dari enam sudah mendarat.
    """
    directory: Path = spec["target_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    files: tuple[str, ...] = spec["files"]

    present = [item for item in files if (directory / item).is_file()]
    if len(present) == len(files) and not force:
        print(
            f"{name}: {len(files)} artefak sudah ada di {directory}, lewati (pakai --force untuk ambil ulang)",
            flush=True,
        )
    else:
        if present and not force:
            # Sebagian ada: itu keadaan yang lebih buruk daripada kosong, karena service-nya akan
            # start-gagal dengan menyebut satu berkas dan menyembunyikan lima lainnya.
            print(f"{name}: {len(present)}/{len(files)} artefak ada; melengkapi sisanya", flush=True)
        for item in files:
            destination = directory / item
            if destination.is_file() and not force:
                continue
            print(f"{name}: mengambil {_join(uri, item)} -> {destination.name}", flush=True)
            _download(_join(uri, item), destination)

    manifest_name = spec.get("manifest")
    if not manifest_name:
        print(f"{name}: siap ({len(files)} artefak)", flush=True)
        return 0

    manifest_path = directory / manifest_name
    if not manifest_path.is_file():
        print(f"{name}: {manifest_name} tidak ada; tidak bisa memverifikasi artefak", file=sys.stderr)
        return 1
    hashes: dict[str, str] = json.loads(manifest_path.read_text(encoding="utf-8"))

    bad: list[str] = []
    for item, expected in hashes.items():
        path = directory / item
        if not path.is_file():
            bad.append(f"{item}: tidak ada")
        elif _sha256(path) != expected.lower():
            bad.append(f"{item}: sha256 tidak cocok")
    if bad:
        for line in bad:
            print(f"{name}: {line}", file=sys.stderr)
        print(f"{name}: artefak TIDAK dipakai; perbaiki sumbernya lalu ulangi dengan --force", file=sys.stderr)
        return 1

    total = sum((directory / item).stat().st_size for item in files) // 1024
    print(f"{name}: siap -- {len(hashes)} artefak terverifikasi terhadap {manifest_name} ({total} KB)", flush=True)
    return 0


def fetch(name: str, *, force: bool) -> int:
    spec = MODELS[name]
    uri = os.environ.get(spec["uri_env"])
    multi = "files" in spec
    if not uri:
        if multi:
            directory: Path = spec["target_dir"]
            missing = [item for item in spec["files"] if not (directory / item).is_file()]
            state = "lengkap" if not missing else f"KURANG {len(missing)}: {', '.join(missing)}"
            print(f"{name}: {spec['uri_env']} tidak di-set; lewati pengambilan ({directory} {state})", flush=True)
        else:
            target: Path = spec["target"]
            print(
                f"{name}: {spec['uri_env']} tidak di-set; lewati pengambilan ({target} "
                f"{'sudah ada' if target.is_file() else 'TIDAK ADA'})"
            )
        return 2
    return _fetch_many(name, spec, uri, force=force) if multi else _fetch_one(name, spec, uri, force=force)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="*", default=list(MODELS), help="nama model (default: semua)")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return max(fetch(name, force=args.force) for name in args.models)


if __name__ == "__main__":
    sys.exit(main())

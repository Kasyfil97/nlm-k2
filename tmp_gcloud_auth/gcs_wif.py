#!/usr/bin/env python3
"""
GCS access via Workload Identity Federation (Entra ID -> GCP), no SA key file.

Credentials come from an env file (default: wif.gcs.env next to this script)
or from the real environment, which takes precedence. Nothing is hardcoded.

Library use:
    from gcs_wif import GCS
    gcs = GCS()
    gcs.upload("../gt_guardrail_per_halaman.csv", "ocr-nilam/bank-statement/guardrail/")
    gcs.list_objects(prefix="ocr-nilam/bank-statement/")
    gcs.download("ocr-nilam/.../file.csv", "./file.csv")

CLI use:
    python gcs_wif.py check
    python gcs_wif.py ls
    python gcs_wif.py ls ocr-nilam/bank-statement/
    python gcs_wif.py upload <local-path> <gcs-prefix-or-key>
    python gcs_wif.py download <gcs-key> <local-path>

The SA (gc-bribrain-dev-sac-gcs-01) can read/write OBJECTS and list buckets.
It cannot create or delete buckets.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

ENV_FILE = Path(__file__).resolve().parent / "wif.gcs.env"

STS_URL = "https://sts.googleapis.com/v1/token"
IAMC_URL = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateAccessToken"
STORAGE = "https://storage.googleapis.com/storage/v1"
UPLOAD = "https://storage.googleapis.com/upload/storage/v1"
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"

# Simple upload below this; resumable above. GCS requires resumable chunks to
# be a multiple of 256 KiB.
SIMPLE_UPLOAD_MAX = 8 * 1024 * 1024
CHUNK = 8 * 1024 * 1024

REQUIRED = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "GCP_PROJECT_ID",
    "GCP_PROJECT_NUMBER",
    "GCP_POOL_ID",
    "GCP_PROVIDER_ID",
    "GCP_SERVICE_ACCOUNT_EMAIL",
)


class WIFError(RuntimeError):
    """Auth or API failure. Never carries a token or secret in its message."""


def load_env(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines. Real environment variables win over the file."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise WIFError(
            f"Missing {name}. Set it in {ENV_FILE.name} or export it. "
            f"See wif.gcs.env.example."
        )
    return val


class GCS:
    """Thin GCS client backed by a WIF-federated, impersonated access token."""

    def __init__(self, bucket: str | None = None, env_file: Path = ENV_FILE):
        load_env(env_file)
        missing = [k for k in REQUIRED if not os.environ.get(k)]
        if missing:
            raise WIFError(
                "Missing config: " + ", ".join(missing) +
                f"\nExpected in {env_file} (see wif.gcs.env.example)."
            )
        self.project = _require("GCP_PROJECT_ID")
        self.sa = _require("GCP_SERVICE_ACCOUNT_EMAIL")
        self.bucket = bucket or os.environ.get("GCS_DEFAULT_BUCKET") or ""
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._session = requests.Session()

    # ---------------- auth ----------------

    def _entra_token(self) -> str:
        """Step 1: OIDC token from Entra ID via client credentials."""
        cid = _require("AZURE_CLIENT_ID")
        tenant = _require("AZURE_TENANT_ID")
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

        # This app registration exposes no Application ID URI, so the bare
        # client-id scope is the one that works; api:// is tried first only
        # for portability across the other SAs in the spreadsheet.
        last = ""
        for scope in (f"{cid}/.default", f"api://{cid}/.default"):
            resp = self._session.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": cid,
                    "client_secret": _require("AZURE_CLIENT_SECRET"),
                    "scope": scope,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()["access_token"]
            last = f"{resp.status_code} {resp.json().get('error_description', resp.text)[:200]}"
            if not ("invalid_resource" in resp.text or "AADSTS500011" in resp.text):
                break
        raise WIFError(f"Entra ID token request failed: {last}")

    def _sts_token(self, entra_token: str) -> str:
        """Step 2: exchange the Entra JWT for a GCP federated token."""
        audience = (
            f"//iam.googleapis.com/projects/{_require('GCP_PROJECT_NUMBER')}"
            f"/locations/global/workloadIdentityPools/{_require('GCP_POOL_ID')}"
            f"/providers/{_require('GCP_PROVIDER_ID')}"
        )
        resp = self._session.post(
            STS_URL,
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "audience": audience,
                "scope": CLOUD_PLATFORM,
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "subject_token": entra_token,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise WIFError(f"STS exchange failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()["access_token"]

    def token(self) -> str:
        """Cached GCP access token. Refreshed 5 min before expiry."""
        if self._token and time.time() < self._expires_at - 300:
            return self._token

        resp = self._session.post(
            IAMC_URL.format(sa=self.sa),
            headers={"Authorization": f"Bearer {self._sts_token(self._entra_token())}"},
            json={"scope": [CLOUD_PLATFORM], "lifetime": "3600s"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise WIFError(
                f"Impersonation of {self.sa} failed: {resp.status_code} {resp.text[:300]}"
            )
        self._token = resp.json()["accessToken"]
        self._expires_at = time.time() + 3600
        return self._token

    def _headers(self, **extra: str) -> dict:
        return {"Authorization": f"Bearer {self.token()}", **extra}

    def _bucket(self, bucket: str | None) -> str:
        name = bucket or self.bucket
        if not name:
            raise WIFError("No bucket given and GCS_DEFAULT_BUCKET is unset.")
        return name

    # ---------------- operations ----------------

    def list_buckets(self) -> list[str]:
        resp = self._session.get(
            f"{STORAGE}/b", params={"project": self.project},
            headers=self._headers(), timeout=30,
        )
        if resp.status_code != 200:
            raise WIFError(f"list_buckets failed: {resp.status_code} {resp.text[:300]}")
        return [b["name"] for b in resp.json().get("items", [])]

    def list_objects(self, prefix: str = "", bucket: str | None = None) -> list[dict]:
        """All objects under prefix, following pagination."""
        name, out, page = self._bucket(bucket), [], None
        while True:
            params = {"prefix": prefix, "maxResults": 1000}
            if page:
                params["pageToken"] = page
            resp = self._session.get(
                f"{STORAGE}/b/{name}/o", params=params,
                headers=self._headers(), timeout=60,
            )
            if resp.status_code != 200:
                raise WIFError(f"list_objects failed: {resp.status_code} {resp.text[:300]}")
            body = resp.json()
            out.extend(body.get("items", []))
            page = body.get("nextPageToken")
            if not page:
                return out

    def upload(self, src: str | Path, dest: str, bucket: str | None = None,
               content_type: str | None = None) -> dict:
        """Upload a local file. A dest ending in '/' keeps the source filename."""
        src = Path(src).expanduser().resolve()
        if not src.is_file():
            raise WIFError(f"Not a file: {src}")
        if not dest or dest.endswith("/"):
            key = f"{dest.rstrip('/')}/{src.name}".lstrip("/")
        else:
            key = dest
        name = self._bucket(bucket)
        ctype = content_type or mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        size = src.stat().st_size

        if size <= SIMPLE_UPLOAD_MAX:
            with src.open("rb") as fh:
                resp = self._session.post(
                    f"{UPLOAD}/b/{name}/o",
                    params={"uploadType": "media", "name": key},
                    headers=self._headers(**{"Content-Type": ctype}),
                    data=fh, timeout=300,
                )
            if resp.status_code not in (200, 201):
                raise WIFError(f"upload failed: {resp.status_code} {resp.text[:400]}")
            return resp.json()

        return self._upload_resumable(src, key, name, ctype, size)

    def _upload_resumable(self, src: Path, key: str, bucket: str,
                          ctype: str, size: int) -> dict:
        """Chunked upload so a dropped connection doesn't restart from zero."""
        start = self._session.post(
            f"{UPLOAD}/b/{bucket}/o",
            params={"uploadType": "resumable", "name": key},
            headers=self._headers(**{
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": ctype,
                "X-Upload-Content-Length": str(size),
            }),
            json={"name": key}, timeout=60,
        )
        if start.status_code not in (200, 201):
            raise WIFError(f"resumable init failed: {start.status_code} {start.text[:300]}")
        session_url = start.headers["Location"]

        with src.open("rb") as fh:
            offset = 0
            while offset < size:
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                resp = self._session.put(
                    session_url,
                    headers={"Content-Range": f"bytes {offset}-{end}/{size}"},
                    data=chunk, timeout=600,
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code != 308:
                    raise WIFError(f"chunk upload failed: {resp.status_code} {resp.text[:300]}")
                rng = resp.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else end + 1
                fh.seek(offset)
        raise WIFError("resumable upload ended without a completion response")

    def download(self, key: str, dest: str | Path, bucket: str | None = None) -> Path:
        name = self._bucket(bucket)
        dest = Path(dest).expanduser()
        if dest.is_dir():
            dest = dest / Path(key).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self._session.get(
            f"{STORAGE}/b/{name}/o/{quote(key, safe='')}",
            params={"alt": "media"}, headers=self._headers(),
            stream=True, timeout=600,
        )
        if resp.status_code != 200:
            raise WIFError(f"download failed: {resp.status_code} {resp.text[:300]}")
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
        return dest

    def stat(self, key: str, bucket: str | None = None) -> dict:
        name = self._bucket(bucket)
        resp = self._session.get(
            f"{STORAGE}/b/{name}/o/{quote(key, safe='')}",
            headers=self._headers(), timeout=30,
        )
        if resp.status_code != 200:
            raise WIFError(f"stat failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()


# ---------------- CLI ----------------

def _human(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:,.0f} {unit}" if unit == "B" else f"{val:,.1f} {unit}"
        val /= 1024
    return f"{val:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--bucket", help="override GCS_DEFAULT_BUCKET")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="verify the WIF chain works")
    p_ls = sub.add_parser("ls", help="list objects (no prefix = list buckets)")
    p_ls.add_argument("prefix", nargs="?", default=None)
    p_up = sub.add_parser("upload")
    p_up.add_argument("src")
    p_up.add_argument("dest", help="object key, or prefix ending in /")
    p_dn = sub.add_parser("download")
    p_dn.add_argument("key")
    p_dn.add_argument("dest", nargs="?", default=".")

    args = parser.parse_args(argv)

    try:
        gcs = GCS(bucket=args.bucket)

        if args.cmd == "check":
            gcs.token()
            print(f"auth OK   SA={gcs.sa}")
            print(f"project   {gcs.project}")
            buckets = gcs.list_buckets()
            print(f"buckets   {len(buckets)} visible")
            for b in buckets:
                print(f"          - {b}")

        elif args.cmd == "ls":
            if args.prefix is None and not (args.bucket or gcs.bucket):
                for b in gcs.list_buckets():
                    print(b)
            else:
                items = gcs.list_objects(args.prefix or "")
                for obj in items:
                    print(f"{_human(int(obj['size'])):>12}  {obj['updated'][:19]}  {obj['name']}")
                print(f"\n{len(items)} object(s)")

        elif args.cmd == "upload":
            meta = gcs.upload(args.src, args.dest)
            print(f"uploaded  gs://{meta['bucket']}/{meta['name']}")
            print(f"          {_human(int(meta['size']))}  md5={meta['md5Hash']}")

        elif args.cmd == "download":
            path = gcs.download(args.key, args.dest)
            print(f"saved     {path}  ({_human(path.stat().st_size)})")

    except WIFError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

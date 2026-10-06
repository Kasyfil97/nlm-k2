"""Unduh objek GCS lewat Workload Identity Federation (Entra ID -> GCP), tanpa SA key maupun
metadata server VM.

Dipakai `scripts/fetch_weights.py` ketika service berjalan di VM yang service account-nya tidak
boleh dipakai -- metadata server menolak mint token dengan 403 ("Service account is invalid/disabled
or not allowed to be used on this VM"). Kredensial diambil dari env (tidak ada yang hardcoded):

    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
    GCP_PROJECT_NUMBER, GCP_POOL_ID, GCP_PROVIDER_ID, GCP_SERVICE_ACCOUNT_EMAIL

Alur: client-credentials ke Entra ID -> JWT; STS token-exchange -> federated token; impersonate SA
(IAM Credentials generateAccessToken) -> access token GCP ber-scope cloud-platform.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import requests

STS_URL = "https://sts.googleapis.com/v1/token"
IAMC_URL = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateAccessToken"
STORAGE = "https://storage.googleapis.com/storage/v1"
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"

REQUIRED = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "GCP_PROJECT_NUMBER",
    "GCP_POOL_ID",
    "GCP_PROVIDER_ID",
    "GCP_SERVICE_ACCOUNT_EMAIL",
)


class WIFError(RuntimeError):
    """Gagal auth/API. Tidak pernah membawa token atau secret di pesannya."""


def wif_configured() -> bool:
    """True kalau semua env WIF terisi -- penanda untuk memilih jalur ini alih-alih ADC."""
    return all(os.environ.get(name) for name in REQUIRED)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise WIFError(f"env WIF {name} belum di-set")
    return val


def _entra_token(session: requests.Session) -> str:
    """Langkah 1: OIDC token dari Entra ID via client credentials."""
    cid = _require("AZURE_CLIENT_ID")
    tenant = _require("AZURE_TENANT_ID")
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    # App registration ini tidak mengekspos Application ID URI, jadi scope client-id telanjang yang
    # berhasil; api:// dicoba dulu hanya untuk portabilitas dengan SA lain di spreadsheet.
    last = ""
    for scope in (f"{cid}/.default", f"api://{cid}/.default"):
        resp = session.post(
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
    raise WIFError(f"permintaan token Entra ID gagal: {last}")


def _sts_token(session: requests.Session, entra_token: str) -> str:
    """Langkah 2: tukar JWT Entra jadi federated token GCP."""
    audience = (
        f"//iam.googleapis.com/projects/{_require('GCP_PROJECT_NUMBER')}"
        f"/locations/global/workloadIdentityPools/{_require('GCP_POOL_ID')}"
        f"/providers/{_require('GCP_PROVIDER_ID')}"
    )
    resp = session.post(
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
        raise WIFError(f"STS exchange gagal: {resp.status_code} {resp.text[:300]}")
    return resp.json()["access_token"]


def _access_token(session: requests.Session) -> str:
    """Langkah 3: impersonate SA untuk dapat access token GCP."""
    sa = _require("GCP_SERVICE_ACCOUNT_EMAIL")
    resp = session.post(
        IAMC_URL.format(sa=sa),
        headers={"Authorization": f"Bearer {_sts_token(session, _entra_token(session))}"},
        json={"scope": [CLOUD_PLATFORM], "lifetime": "3600s"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise WIFError(f"impersonasi {sa} gagal: {resp.status_code} {resp.text[:300]}")
    return resp.json()["accessToken"]


def download(uri: str, target: Path) -> None:
    """Unduh satu objek `gs://bucket/blob` ke `target` lewat token WIF."""
    if not uri.startswith("gs://"):
        raise WIFError(f"bukan URI GCS: {uri!r}")
    bucket, _, blob = uri[len("gs://"):].partition("/")
    if not bucket or not blob:
        raise WIFError(f"URI GCS tidak lengkap (butuh gs://bucket/objek): {uri!r}")

    session = requests.Session()
    resp = session.get(
        f"{STORAGE}/b/{bucket}/o/{quote(blob, safe='')}",
        params={"alt": "media"},
        headers={"Authorization": f"Bearer {_access_token(session)}"},
        stream=True,
        timeout=600,
    )
    if resp.status_code != 200:
        raise WIFError(f"unduh gs://{bucket}/{blob} gagal: {resp.status_code} {resp.text[:300]}")
    with open(target, "wb") as handle:
        for chunk in resp.iter_content(1 << 20):
            handle.write(chunk)

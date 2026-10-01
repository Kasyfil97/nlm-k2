"""R18a: guardrails downloads only when `GUARDRAILS_FETCH_URL` says so, and it downloads through
the shared policy or not at all.

The start-up half of the requirement is in `test_config.py`. This is the request half, plus the
check that matters more than either: that this service did not grow a URL policy of its own. Two
divergent SSRF implementations is the failure the plan names outright, and it is the kind that
passes every test each half has.
"""

import pathlib

from ocr_common.testing import image_upload

from tests.conftest import JPEG

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
URL = "https://minio.example.internal/bucket/kk.jpg"


def test_a_file_url_is_refused_while_the_switch_is_off(client, auth):
    response = client.post("/v1/guardrails/check", data={"request_id": "OCR_U1", "file_url": URL}, headers=auth)
    assert response.status_code == 400
    assert "GUARDRAILS_FETCH_URL=false" in response.json()["message"]


def test_the_switch_being_off_does_not_break_the_normal_upload(client, auth):
    response = client.post(
        "/v1/guardrails/check", data={"request_id": "OCR_U2"}, files=image_upload("kk.jpg", JPEG), headers=auth
    )
    assert response.status_code == 200


def test_with_the_switch_on_the_url_reaches_the_shared_fetcher(client, auth, settings_override):
    """Past the switch, the refusal text comes from `ocr_common.clients.fetch_url` -- which is the
    point: the policy is the one Unit 2 hardened, applied by `read_image`, not a copy."""
    settings_override(guardrails_fetch_url=True)
    response = client.post(
        "/v1/guardrails/check",
        data={"request_id": "OCR_U3", "file_url": "ftp://minio.example.internal/bucket/kk.jpg"},
        headers=auth,
    )
    assert response.status_code == 400
    assert response.json()["message"] == "Unsupported URL scheme: ftp"


def test_sending_both_a_file_and_a_url_is_still_refused(client, auth, settings_override):
    settings_override(guardrails_fetch_url=True)
    response = client.post(
        "/v1/guardrails/check",
        data={"request_id": "OCR_U4", "file_url": URL},
        files=image_upload("kk.jpg", JPEG),
        headers=auth,
    )
    assert response.status_code == 400
    assert response.json()["message"] == "Send exactly one of file or file_url"


def test_this_service_has_no_url_policy_of_its_own():
    """The switch is the only thing this unit added. Anything below -- host lists, DNS resolution,
    address classification, a second `UrlPolicy` -- means two implementations that will drift, and
    the one that drifts is the one nobody is testing."""
    forbidden = ("UrlPolicy(", "getaddrinfo", "ipaddress", "ip_address", "allowed_hosts =", "urlsplit")
    offenders = [
        f"{path.relative_to(APP)}: {term}"
        for path in APP.rglob("*.py")
        for term in forbidden
        if term in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"guardrails is reimplementing the file_url policy: {offenders}"

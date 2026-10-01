import pytest
from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.testclient import TestClient

from ocr_common.config import BaseServiceSettings
from ocr_common.errors import BadRequest
from ocr_common.testing import TEST_API_KEY
from ocr_common.web.app import create_app
from ocr_common.web.envelope import envelope
from ocr_common.web.intake import FileField, FileUrlField, read_image
from ocr_common.web.request_id import REQUEST_ID_HEADER, adopt_request_id, get_request_id, reset_request_id
from ocr_common.web.security import verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.post("/v1/echo")
async def echo(request: Request, file: UploadFile | str | None = FileField, file_url: str | None = FileUrlField):
    content, filename, content_type = await read_image(request, file, file_url)
    return envelope(200, "Success", {"size": len(content), "filename": filename}, get_request_id(request))


@router.post("/v1/json")
async def json_endpoint(request: Request, body: dict):
    return envelope(200, "Success", body, get_request_id(request))


settings = BaseServiceSettings(api_key=TEST_API_KEY, environment="local", _env_file=None)
app = create_app(
    settings=settings,
    title="Demo",
    description="demo",
    routers=[router],
    backends={"demo": "mock"},
)
client = TestClient(app, raise_server_exceptions=False)
AUTH = {"X-API-Key": TEST_API_KEY}


def test_health_is_public():
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["backends"] == {"demo": "mock"}


def test_missing_api_key_is_401_envelope():
    response = client.post("/v1/echo", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert response.status_code == 401
    body = response.json()
    assert body["status_desc"] == "Unauthorized"
    assert (body["message"], body["errors"]) == ("Invalid or missing API key", "UNAUTHORIZED")


@pytest.mark.parametrize("key", ["salah", "kk", "ké"])
def test_wrong_api_key_is_401_including_non_ascii(key):
    response = client.post("/v1/json", json={}, headers={"X-API-Key": key.encode("latin-1")})
    assert response.status_code == 401


def test_auth_disabled_skips_the_api_key_check():
    open_app = create_app(
        settings=BaseServiceSettings(api_key=TEST_API_KEY, auth_disabled=True, environment="local", _env_file=None),
        title="Demo",
        description="demo",
        routers=[router],
    )
    open_client = TestClient(open_app, raise_server_exceptions=False)
    assert open_client.post("/v1/echo", files={"file": ("a.jpg", b"x", "image/jpeg")}).status_code == 200
    assert (
        open_client.post(
            "/v1/echo", files={"file": ("a.jpg", b"x", "image/jpeg")}, headers={"X-API-Key": "salah"}
        ).status_code
        == 200
    )


def _app_with_readiness(readiness):
    return TestClient(
        create_app(settings=settings, title="Demo", description="demo", readiness=readiness),
        raise_server_exceptions=False,
    )


def test_ready_without_dependencies_is_ready_and_public():
    response = _app_with_readiness(None).get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {}}


def test_ready_is_503_when_a_required_dependency_fails_but_health_stays_up():
    async def ok():
        return None

    async def down():
        raise ConnectionError("postgresql://user:secret@db:5432 unreachable")

    probe = _app_with_readiness({"database": down, "cache": ok})
    response = probe.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed", "cache": "ok"}}
    assert "secret" not in response.text
    assert probe.get("/health").status_code == 200


def test_request_id_header_is_echoed_or_generated():
    response = client.post(
        "/v1/echo", files={"file": ("a.jpg", b"x", "image/jpeg")}, headers={**AUTH, "X-Request-ID": "OCR_1"}
    )
    assert response.json()["request_id"] == "OCR_1"
    assert response.headers["X-Request-ID"] == "OCR_1"
    generated = client.post("/v1/echo", files={"file": ("a.jpg", b"x", "image/jpeg")}, headers=AUTH)
    assert generated.json()["request_id"].startswith("REQ_")


def test_intake_requires_exactly_one_of_file_or_file_url():
    neither = client.post("/v1/echo", data={"file": ""}, headers=AUTH)
    assert neither.status_code == 400
    assert neither.json()["message"] == "Send exactly one of file or file_url"
    both = client.post(
        "/v1/echo", data={"file_url": "http://x/y.jpg"}, files={"file": ("a.jpg", b"x", "image/jpeg")}, headers=AUTH
    )
    assert both.status_code == 400


def test_intake_rejects_bad_url_scheme_as_400():
    response = client.post("/v1/echo", data={"file_url": "file:///etc/passwd"}, headers=AUTH)
    assert response.status_code == 400
    assert "Unsupported URL scheme" in response.json()["message"]


# Kebijakan `file_url` sengaja longgar dengan ENVIRONMENT=local (MinIO di laptop tanpa TLS, alamat
# privat, allow-list kosong). Kedua uji di bawah karenanya butuh app yang settings-nya TER-DEPLOY --
# menjalankannya terhadap app lokal justru akan benar-benar mencoba menghubungi alamatnya.
deployed_app = create_app(
    settings=BaseServiceSettings(
        api_key=TEST_API_KEY,
        environment="production",
        file_url_allowed_hosts="storage.example.com",
        _env_file=None,
    ),
    title="Demo",
    description="demo",
    routers=[router],
    backends={"demo": "remote"},
)
deployed_client = TestClient(deployed_app, raise_server_exceptions=False)


def test_intake_refuses_internal_file_url_as_400():
    response = deployed_client.post(
        "/v1/echo", data={"file_url": "https://169.254.169.254/latest/meta-data/"}, headers=AUTH
    )
    assert response.status_code == 400
    assert response.json()["message"] == "file_url host is not allowed: 169.254.169.254"


def test_intake_refuses_plain_http_as_400():
    """Skema diperiksa sebelum host, jadi http ditolak lebih dulu dan tidak pernah di-resolve."""
    response = deployed_client.post("/v1/echo", data={"file_url": "http://storage.example.com/a.jpg"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["message"] == "Unsupported URL scheme: http (https is required)"


def test_an_unlisted_host_is_refused_even_over_https():
    response = deployed_client.post("/v1/echo", data={"file_url": "https://evil.test/a.jpg"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["message"] == "file_url host is not allowed: evil.test"


def test_validation_error_uses_envelope_with_code():
    response = client.post("/v1/json", json=[1, 2], headers=AUTH)
    assert response.status_code == 422
    body = response.json()
    assert body["errors"] == "VALIDATION_ERROR"
    assert body["message"].startswith("body:")


def test_every_accepted_key_opens_the_door_during_a_rotation():
    rotating = create_app(
        settings=BaseServiceSettings(api_key="baru", api_keys="lama, baru ,", environment="local", _env_file=None),
        title="Demo",
        description="demo",
        routers=[router],
    )
    rotating_client = TestClient(rotating, raise_server_exceptions=False)
    assert rotating.state.settings.accepted_api_keys == ("baru", "lama")
    for key in ("lama", "baru"):
        assert rotating_client.post("/v1/json", json={}, headers={"X-API-Key": key}).status_code == 200
    assert rotating_client.post("/v1/json", json={}, headers={"X-API-Key": TEST_API_KEY}).status_code == 401


adopting = APIRouter()


@adopting.post("/v1/adopt")
async def adopt(request: Request, request_id: str = Form(...)):
    token = adopt_request_id(request, request_id)
    try:
        raise BadRequest("Uploaded file is empty")
    finally:
        reset_request_id(token)


def test_an_adopted_request_id_is_in_the_error_envelope_and_the_response_header():
    """An id sent in the body, not in the header, replaces the middleware's once the handler adopts it."""
    adopting_app = create_app(settings=settings, title="Demo", description="demo", routers=[adopting])
    response = TestClient(adopting_app, raise_server_exceptions=False).post(
        "/v1/adopt", data={"request_id": "OCR_from_form"}, headers={REQUEST_ID_HEADER: "REQ_from_header"}
    )

    assert response.status_code == 400
    assert response.json()["request_id"] == "OCR_from_form"
    assert response.headers[REQUEST_ID_HEADER] == "OCR_from_form"


# --- stable error codes and the service that answered (ported from nilam 2a40723 / 624d34c / e4a30e2) --


def test_every_error_carries_a_stable_code():
    from ocr_common.errors import NotFound, PayloadTooLarge, UpstreamTimeout

    errors = {
        "empty": BadRequest("Uploaded file is empty", "EMPTY_FILE"),
        "missing": NotFound("No request found for request_id X"),
        "big": PayloadTooLarge("Ukuran dokumen melebihi batas 5 MB"),
        "slow": UpstreamTimeout("model timed out"),
    }
    router = APIRouter()

    @router.get("/boom/{name}")
    async def boom(name: str):
        if name == "bug":
            raise RuntimeError("secret internals")
        raise errors[name]

    probe = TestClient(
        create_app(settings=settings, title="Demo", description="demo", routers=[router]),
        raise_server_exceptions=False,
    )

    codes = {name: probe.get(f"/boom/{name}").json()["errors"] for name in [*errors, "bug"]}

    assert codes == {
        "empty": "EMPTY_FILE",
        "missing": "REQUEST_ID_NOT_FOUND",
        "big": "FILE_TOO_LARGE",
        "slow": "DOWNSTREAM_TIMEOUT",
        "bug": "INTERNAL_SERVER_ERROR",
    }
    crash = probe.get("/boom/bug")
    assert crash.status_code == 500 and crash.json()["status_desc"] == "Internal Server Error"
    assert "secret" not in crash.text


def test_a_path_or_method_that_does_not_exist_has_its_own_code():
    """An unknown path is not an unknown request_id, and a wrong method is not a generic error."""
    probe = TestClient(create_app(settings=settings, title="Demo", description="demo"))

    missing = probe.get("/v1/nope").json()
    wrong_method = probe.post("/health").json()

    assert (missing["status_code"], missing["errors"]) == (404, "NOT_FOUND")
    assert (wrong_method["status_code"], wrong_method["status_desc"], wrong_method["errors"]) == (
        405,
        "Method Not Allowed",
        "METHOD_NOT_ALLOWED",
    )


def test_an_error_names_the_service_it_comes_from():
    from ocr_common.errors import UpstreamUnavailable

    class CalledServiceDown(UpstreamUnavailable):
        service = "ekstraksi"

    router = APIRouter()

    @router.get("/own")
    async def own():
        raise BadRequest("Uploaded file is empty", "EMPTY_FILE")

    @router.get("/called")
    async def called():
        raise CalledServiceDown("ekstraksi service is unavailable")

    probe = TestClient(
        create_app(settings=settings, title="Demo", description="demo", service_name="scoring", routers=[router]),
        raise_server_exceptions=False,
    )

    assert probe.get("/own").json()["pipeline_last_stage"] == "scoring"
    assert probe.get("/called").json()["pipeline_last_stage"] == "ekstraksi"
    assert probe.get("/v1/unknown-route").json()["pipeline_last_stage"] == "scoring"


def test_the_document_checks_have_their_codes():
    from ocr_common.errors import ServiceError
    from ocr_common.image_validation import validate_image

    def code(content_type, content, **update):
        try:
            validate_image(content_type, content, settings.model_copy(update=update))
        except ServiceError as exc:
            return exc.status_code, exc.code
        return None

    assert code("image/jpeg", b"") == (400, "EMPTY_FILE")
    assert code("image/gif", b"x") == (400, "UNSUPPORTED_FILE_TYPE")
    assert code("image/jpeg", b"xx", max_upload_bytes=1) == (413, "FILE_TOO_LARGE")
    assert client.post("/v1/echo", headers=AUTH).json()["errors"] == "INVALID_FILE_SOURCE"

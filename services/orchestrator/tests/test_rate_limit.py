"""The rate limit, against its own small app.

The real app is exercised elsewhere with the limit set out of the way (see `conftest`), because a
suite that shares one API key shares one bucket. Here the number under test IS the number that
matters, so each test builds exactly the app it needs -- and wraps the middleware by hand, so the
test holds the instance whose state it wants to inspect.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from ocr_common.testing import TEST_API_KEY

from app.main import app as real_app
from app.middleware import EXEMPT_PATHS, RateLimitMiddleware, client_key

KEY = {"X-API-Key": TEST_API_KEY}
OTHER = {"X-API-Key": "another-caller-entirely"}


def _app(requests: int = 2, window: float = 60.0) -> tuple[TestClient, RateLimitMiddleware]:
    inner = FastAPI()

    @inner.get("/v1/thing")
    def thing():
        return {"ok": True}

    @inner.get("/health")
    def health():
        return {"ok": True}

    limiter = RateLimitMiddleware(inner, requests=requests, window_seconds=window)
    return TestClient(limiter), limiter


def test_requests_up_to_the_limit_pass_and_the_next_is_429():
    client, _ = _app(requests=2)
    assert [client.get("/v1/thing", headers=KEY).status_code for _ in range(3)] == [200, 200, 429]


def test_the_429_carries_the_standard_envelope_and_a_retry_after():
    client, _ = _app(requests=1)
    client.get("/v1/thing", headers=KEY)
    response = client.get("/v1/thing", headers=KEY)
    body = response.json()
    assert response.status_code == 429
    assert (body["status_code"], body["status_desc"]) == (429, "Too Many Requests")
    assert (body["errors"], body["pipeline_last_stage"], body["data"]) == ("TOO_MANY_REQUESTS", "orchestrator", None)
    assert body["message"].startswith("Terlalu banyak permintaan"), "the Indonesian sentence stays in `message`"
    assert int(response.headers["Retry-After"]) >= 1


def test_the_429_echoes_the_callers_request_id():
    """The limiter runs outside `RequestIdMiddleware`, so it reads `X-Request-ID` itself. Without
    this, the one response a caller most needs to correlate would be the one without an id."""
    client, _ = _app(requests=1)
    headers = {**KEY, "X-Request-ID": "REQ_abc"}
    client.get("/v1/thing", headers=headers)
    response = client.get("/v1/thing", headers=headers)
    assert response.json()["request_id"] == "REQ_abc"
    assert response.headers["X-Request-ID"] == "REQ_abc"


def test_a_caller_without_a_request_id_still_gets_a_well_formed_429():
    client, _ = _app(requests=1)
    client.get("/v1/thing", headers=KEY)
    assert client.get("/v1/thing", headers=KEY).json()["request_id"] is None


def test_callers_are_counted_separately():
    client, _ = _app(requests=1)
    assert client.get("/v1/thing", headers=KEY).status_code == 200
    assert client.get("/v1/thing", headers=OTHER).status_code == 200
    assert client.get("/v1/thing", headers=KEY).status_code == 429


def test_the_probes_are_never_limited():
    """A 429 on /ready takes the pod out of the Service exactly when it is busiest, and kubelet
    calls it far more often than any client does."""
    client, _ = _app(requests=1)
    assert {client.get("/health").status_code for _ in range(5)} == {200}
    assert {"/health", "/ready", "/metrics"} <= EXEMPT_PATHS


def test_the_window_slides():
    """`time.monotonic` is not patched: the recorded hit is pushed into the past instead, so what
    this tests is the pruning rather than the clock."""
    client, limiter = _app(requests=1, window=60.0)
    assert client.get("/v1/thing", headers=KEY).status_code == 200
    assert client.get("/v1/thing", headers=KEY).status_code == 429

    for hits in limiter._hits.values():
        hits[0] -= 61.0
    assert client.get("/v1/thing", headers=KEY).status_code == 200


def test_idle_callers_are_swept_so_the_table_cannot_grow_without_bound():
    """Twenty distinct callers, a window short enough that each is idle by the next request: the
    table must shrink. Without the sweep it grows one entry per key ever seen, for the life of the
    process."""
    client, limiter = _app(requests=10, window=0.0001)
    for index in range(20):
        client.get("/v1/thing", headers={"X-API-Key": f"caller-number-{index}"})
    assert len(limiter._hits) < 20


def test_the_key_is_hashed_not_stored():
    """A raw API key used as a dict key outlives its request on the heap and shows up whole in any
    memory dump or traceback that carries this middleware's state."""

    class Request:
        headers = {"X-API-Key": TEST_API_KEY}
        client = None

    key = client_key(Request())
    assert key.startswith("k:") and TEST_API_KEY not in key


def test_a_caller_without_an_api_key_is_counted_by_address():
    class Request:
        headers: dict[str, str] = {}
        client = type("C", (), {"host": "10.0.0.7"})()

    assert client_key(Request()) == "a:10.0.0.7"


def test_the_real_app_has_the_limiter_installed():
    """Every test above would still pass if `add_edge_middleware` were never called."""
    assert RateLimitMiddleware in [middleware.cls for middleware in real_app.user_middleware]


# --- CORS -----------------------------------------------------------------------------------


def _cors_app(origins: str) -> FastAPI:
    from app.config import Settings
    from app.middleware import add_edge_middleware

    app = FastAPI()

    @app.get("/v1/thing")
    def thing():
        return {"ok": True}

    settings = Settings(
        api_key=TEST_API_KEY,
        _env_file=None,
        environment="local",
        rate_limit_enabled=False,
        cors_allow_origins=origins,
    )
    add_edge_middleware(app, settings)
    return app


def test_no_cors_middleware_at_all_when_no_origin_is_configured():
    """Off is the shipped default. An open CORS policy on an API-key service is how a leaked key
    becomes usable from any page the victim visits."""
    assert CORSMiddleware not in [middleware.cls for middleware in _cors_app("").user_middleware]


def test_a_configured_origin_gets_the_header_and_others_do_not():
    client = TestClient(_cors_app("https://console.example"))
    allowed = client.get("/v1/thing", headers={"Origin": "https://console.example"})
    other = client.get("/v1/thing", headers={"Origin": "https://evil.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://console.example"
    assert "access-control-allow-origin" not in other.headers


def test_credentials_are_never_allowed():
    """Authentication here is a header, not a cookie, so there is no browser credential to allow --
    and `allow_credentials` together with a wildcard is a combination the CORS spec forbids."""
    client = TestClient(_cors_app("https://console.example"))
    response = client.options(
        "/v1/thing",
        headers={
            "Origin": "https://console.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-API-Key",
        },
    )
    assert response.status_code == 200
    assert "access-control-allow-credentials" not in response.headers
    assert "x-api-key" in response.headers["access-control-allow-headers"].lower()

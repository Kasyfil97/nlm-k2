from fastapi import APIRouter
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from ocr_common.config import BaseServiceSettings
from ocr_common.pipeline import metrics
from ocr_common.pipeline.outbox import OutboxStats
from ocr_common.testing import TEST_API_KEY
from ocr_common.web.app import create_app


def _sample(name: str, **labels) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def _client() -> TestClient:
    router = APIRouter()

    @router.get("/v1/things/{thing_id}")
    def thing(thing_id: str) -> dict:
        return {"id": thing_id}

    settings = BaseServiceSettings(api_key=TEST_API_KEY, environment="local", _env_file=None)
    return TestClient(
        create_app(settings=settings, title="Demo", description="demo", service_name="metricsdemo", routers=[router])
    )


def test_requests_are_counted_by_route_template_not_by_concrete_path():
    client = _client()
    labels = {"service": "metricsdemo", "method": "GET", "path": "/v1/things/{thing_id}", "status": "200"}
    before = _sample("http_requests_total", **labels) or 0

    client.get("/v1/things/a", headers={"X-API-Key": TEST_API_KEY})
    client.get("/v1/things/b", headers={"X-API-Key": TEST_API_KEY})

    assert _sample("http_requests_total", **labels) == before + 2
    assert _sample("http_requests_total", **{**labels, "path": "/v1/things/a"}) is None


def test_probes_and_the_metrics_endpoint_are_not_measured():
    client = _client()

    for path in ("/health", "/ready", "/metrics"):
        client.get(path)

    for path in ("/health", "/ready", "/metrics"):
        assert not any(
            sample.labels.get("service") == "metricsdemo" and sample.labels.get("path") == path
            for family in REGISTRY.collect()
            if family.name.startswith("http_request")
            for sample in family.samples
        ), path


def test_the_metrics_endpoint_serves_the_prometheus_text_format():
    response = _client().get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text


def test_observe_outbox_copies_the_backlog_into_the_gauges():
    stats = OutboxStats(stage="scoring", pending=5, retrying=2, oldest_pending_seconds=42.5, dead_letters=1)

    metrics.observe_outbox(stats)

    assert _sample("pipeline_outbox_pending", stage="scoring") == 5
    assert _sample("pipeline_outbox_retrying", stage="scoring") == 2
    assert _sample("pipeline_outbox_dead_letters", stage="scoring") == 1
    assert _sample("pipeline_outbox_oldest_pending_seconds", stage="scoring") == 42.5


def test_observe_outbox_reports_zero_age_when_nothing_is_pending():
    metrics.observe_outbox(
        OutboxStats(stage="idle", pending=0, retrying=0, oldest_pending_seconds=None, dead_letters=0)
    )

    assert _sample("pipeline_outbox_oldest_pending_seconds", stage="idle") == 0.0

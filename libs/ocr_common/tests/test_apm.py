import json
import logging

import pytest
from elasticapm.contrib.starlette import ElasticAPM
from elasticapm.traces import execution_context
from fastapi.testclient import TestClient

from ocr_common.config import BaseServiceSettings
from ocr_common.pipeline.callbacks import TRACE_PARENT_KEY, _without_answer
from ocr_common.pipeline.outbox import callback_message, handoff_message
from ocr_common.testing import TEST_API_KEY
from ocr_common.web import apm
from ocr_common.web.apm import apm_config
from ocr_common.web.app import create_app
from ocr_common.web.logging import JsonFormatter
from ocr_common.web.request_id import bind_request_id, reset_request_id


def make_settings(**values) -> BaseServiceSettings:
    return BaseServiceSettings(api_key=TEST_API_KEY, environment="local", _env_file=None, **values)


def build(settings: BaseServiceSettings):
    return create_app(settings=settings, title="Demo", description="demo", service_name="scoring")


def has_apm(app) -> bool:
    return any(middleware.cls is ElasticAPM for middleware in app.user_middleware)


@pytest.fixture
def no_send(monkeypatch):
    """The agent never talks to a server in tests."""
    monkeypatch.setenv("ELASTIC_APM_DISABLE_SEND", "true")
    monkeypatch.setenv("ELASTIC_APM_CLOUD_PROVIDER", "none")


def test_apm_is_off_without_a_server_url():
    assert not has_apm(build(make_settings()))


def test_config_names_each_service_and_keeps_payloads_out():
    config = apm_config(
        make_settings(
            elastic_apm_server_url="http://apm:8200",
            elastic_apm_secret_token="test-token",
            elastic_apm_sanitize_field_names="password, *token,,auth",
        ),
        "scoring",
    )
    assert config["SERVICE_NAME"] == "nilam-ocr-kk-scoring"
    assert config["ENVIRONMENT"] == "local"
    assert config["SECRET_TOKEN"] == "test-token"
    assert config["SANITIZE_FIELD_NAMES"] == ["password", "*token", "auth"]
    assert (config["CAPTURE_BODY"], config["CAPTURE_HEADERS"]) == ("off", False)
    assert (config["COLLECT_LOCAL_VARIABLES"], config["CENTRAL_CONFIG"]) == ("off", False)


def test_explicit_service_name_and_environment_win():
    config = apm_config(
        make_settings(
            elastic_apm_server_url="http://apm:8200", elastic_apm_service_name="x", elastic_apm_environment="dev"
        ),
        "scoring",
    )
    assert (config["SERVICE_NAME"], config["ENVIRONMENT"]) == ("x", "dev")


def test_apm_middleware_is_installed_and_requests_still_work(no_send):
    app = build(make_settings(elastic_apm_server_url="http://apm:8200"))
    assert has_apm(app)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_turning_body_capture_back_on_refuses_to_start(no_send, monkeypatch):
    monkeypatch.setenv("ELASTIC_APM_CAPTURE_BODY", "all")
    with pytest.raises(ValueError, match="ELASTIC_APM_CAPTURE_BODY"):
        build(make_settings(elastic_apm_server_url="http://apm:8200"))


def test_start_keeps_the_client_and_stop_clears_it(no_send, monkeypatch):
    monkeypatch.setattr(apm, "_client", None)
    client = apm.start(make_settings(elastic_apm_server_url="http://apm:8200", elastic_apm_api_key="a"), "scoring")
    try:
        assert apm._client is client
    finally:
        apm.stop()
    assert apm._client is None


def test_a_refused_start_leaves_no_client(no_send, monkeypatch):
    monkeypatch.setattr(apm, "_client", None)
    monkeypatch.setenv("ELASTIC_APM_COLLECT_LOCAL_VARIABLES", "all")
    with pytest.raises(ValueError, match="COLLECT_LOCAL_VARIABLES"):
        apm.start(make_settings(elastic_apm_server_url="http://apm:8200"), "scoring")
    assert apm._client is None


def test_without_a_client_everything_is_a_no_op():
    assert apm.trace_fields() == {}
    assert apm.trace_parent() is None
    with apm.job_transaction("OCR", "REQ_1"):
        apm.job_failed()
    assert TRACE_PARENT_KEY not in handoff_message("STRUCTURING", {}).payload


def test_a_job_is_a_transaction_labelled_with_its_request_id(apm_client):
    with apm.job_transaction("OCR", "REQ_1"):
        transaction = execution_context.get_transaction()
        assert transaction.transaction_type == "job"
        assert transaction.labels == {"request_id": "REQ_1", "stage": "OCR"}

    assert (transaction.name, transaction.result, transaction.outcome) == ("OCR job", "done", "success")
    assert execution_context.get_transaction() is None


def test_a_failed_job_is_marked_failed(apm_client):
    with apm.job_transaction("SCORING", "REQ_2"):
        transaction = execution_context.get_transaction()
        apm.job_failed()

    assert (transaction.result, transaction.outcome) == ("failed", "failure")


def test_an_interrupted_job_is_marked_and_the_exception_goes_on(apm_client):
    with pytest.raises(RuntimeError), apm.job_transaction("OCR", "REQ_3"):
        transaction = execution_context.get_transaction()
        raise RuntimeError("shutdown")

    assert (transaction.result, transaction.outcome) == ("interrupted", "failure")


def test_binding_a_request_id_labels_the_current_transaction(apm_client):
    apm_client.begin_transaction("request")
    token = bind_request_id("REQ_adopted")
    try:
        assert execution_context.get_transaction().labels["request_id"] == "REQ_adopted"
    finally:
        reset_request_id(token)
        apm_client.end_transaction("t")


def test_json_log_lines_carry_the_trace_of_the_active_transaction(apm_client):
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    assert "trace.id" not in json.loads(JsonFormatter(None).format(record))

    apm_client.begin_transaction("request")
    try:
        line = json.loads(JsonFormatter(None).format(record))
        transaction = execution_context.get_transaction()
        assert line["trace.id"] == transaction.trace_parent.trace_id
        assert line["transaction.id"] == transaction.id
    finally:
        apm_client.end_transaction("t")


def test_a_job_continues_the_trace_of_the_request_that_queued_it(apm_client):
    request = apm_client.begin_transaction("request")
    try:
        with apm.job_transaction("OCR", "REQ_4"):
            job = execution_context.get_transaction()
    finally:
        apm_client.end_transaction("POST /v1/ocr/jobs")

    assert job is not request
    assert job.trace_parent.trace_id == request.trace_parent.trace_id
    assert job.trace_parent.span_id == request.id, "a child of the request, not of whoever called the request"


def test_a_job_without_a_request_starts_its_own_trace(apm_client):
    with apm.job_transaction("OCR", "REQ_5"):
        job = execution_context.get_transaction()

    assert job.trace_parent.span_id == job.id, "no parent: the root of a new trace"


def test_the_trace_parent_names_the_active_transaction_and_nothing_without_one(apm_client):
    assert apm.trace_parent() is None
    transaction = apm_client.begin_transaction("job")
    try:
        parent = apm.trace_parent()
    finally:
        apm_client.end_transaction("t")

    assert parent == f"00-{transaction.trace_parent.trace_id}-{transaction.id}-01"


def test_outbox_messages_carry_the_trace_of_the_job_that_wrote_them(apm_client):
    assert TRACE_PARENT_KEY not in callback_message("REQ_6", "OCR", "DONE").payload, "no transaction, no trace"

    with apm.job_transaction("OCR", "REQ_6"):
        parent = apm.trace_parent()
        callback = callback_message("REQ_6", "OCR", "DONE")
        handoff = handoff_message("STRUCTURING", {"request_id": "REQ_6"})

    assert callback.payload[TRACE_PARENT_KEY] == parent
    assert handoff.payload == {"next_stage": "STRUCTURING", "body": {"request_id": "REQ_6"}, TRACE_PARENT_KEY: parent}
    assert TRACE_PARENT_KEY not in _without_answer(callback.payload), "never sent to the orchestrator"


@pytest.mark.parametrize(
    ("result", "outcome"), [(apm.DELIVERED, "success"), (apm.RETRY, "failure"), (apm.DEAD, "failure")]
)
def test_a_delivery_is_a_transaction_in_the_trace_of_its_message(apm_client, result, outcome):
    job = apm_client.begin_transaction("job")
    parent = apm.trace_parent()
    apm_client.end_transaction("OCR job")

    with apm.delivery_transaction("OCR", "handoff", "REQ_8", parent, attempt=2, target="STRUCTURING"):
        delivery = execution_context.get_transaction()
        apm.delivery_result(result)

    assert (delivery.name, delivery.transaction_type) == ("OCR handoff", "outbox")
    assert (delivery.result, delivery.outcome) == (result, outcome)
    assert delivery.labels == {
        "request_id": "REQ_8",
        "stage": "OCR",
        "kind": "handoff",
        "attempt": 2,
        "target": "STRUCTURING",
    }
    assert (delivery.trace_parent.trace_id, delivery.trace_parent.span_id) == (job.trace_parent.trace_id, job.id)


def test_a_delivery_that_raises_is_an_error(apm_client):
    with pytest.raises(RuntimeError), apm.delivery_transaction("OCR", "callback", "REQ_9", None, attempt=1):
        delivery = execution_context.get_transaction()
        raise RuntimeError("database gone")

    assert (delivery.result, delivery.outcome) == ("error", "failure")
    assert delivery.trace_parent.span_id == delivery.id, "no parent: the root of a new trace"

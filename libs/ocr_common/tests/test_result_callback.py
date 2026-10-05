import json
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from ocr_common.clients.remote import RemoteClientError, RemoteModelClient
from ocr_common.config import PipelineSettings
from ocr_common.errors import ServiceError
from ocr_common.kk import final_result
from ocr_common.pipeline import callbacks
from ocr_common.pipeline.callbacks import (
    OrchestrationCallback,
    ResultCallback,
    not_ready,
    result_callback_body,
    stage_callback_body,
)
from ocr_common.pipeline.factory import build_callback
from ocr_common.testing import TEST_API_KEY
from ocr_common.types import ScoringResult, StructuringResult

RID = "OCR_9cb01af2-493d-446d-b191-af120333f6d0"
GUARDRAILS = {"passed": True, "reason": None, "document": {"verdict": "accepted", "confidence": 0.98}}
PATH = "/v1/ocr-callback"


def _c(value: str, confidence: int) -> dict[str, Any]:
    return {"value": value, "confidence": confidence}


# The `data` of the extract-ocr 200 for the same request: what scoring's outcome_data gives.
ANSWER = {
    "no_kk": _c("3273012345678901", 0),
    "nama_kepala_keluarga": _c("BUDI SANTOSO", 1),
    "anggota_keluarga": [
        {
            "nama_lengkap": _c("BUDI SANTOSO", 1),
            "nik": _c("3273011203850001", 1),
            "pendidikan": _c("S1", 0),
            "jenis_pekerjaan": _c("KARYAWAN SWASTA", 0),
            "status_hubungan_dalam_rumah_tangga": _c("KEPALA KELUARGA", 1),
            "ayah": _c("SUTRISNO", 0),
            "ibu": _c("SITI AMINAH", 0),
        }
    ],
}


def _f(value, ocr_conf=0.99):
    return {"value": value, "ocr_conf": ocr_conf, "crf_conf": None}


def _member(nama="BUDI SANTOSO", nik="3273011203850001"):
    return {
        "nama_lengkap": _f(nama),
        "nik": _f(nik),
        "pendidikan": _f("S1"),
        "jenis_pekerjaan": _f("KARYAWAN SWASTA"),
        "status_hubungan_dalam_keluarga": _f("KEPALA KELUARGA"),
        "ayah": _f("SUTRISNO"),
        "ibu": _f("SITI AMINAH"),
    }


def _member_scores(**overrides):
    scores = {
        "nama_lengkap": 0.9702,
        "nik": 0.9655,
        "pendidikan": 0.8410,
        "jenis_pekerjaan": 0.7733,
        "status_hubungan_dalam_keluarga": 0.9218,
        "ayah": 0.8064,
        "ibu": 0.3951,
    }
    return {**scores, **overrides}


def _final(**scoring_extra: Any) -> dict[str, Any]:
    structuring = {
        "nomor_kk": _f("3273012345678901"),
        "nama_kepala_keluarga": _f("BUDI SANTOSO"),
        "anggota_keluarga": [_member()],
        "reject_reason": None,
    }
    scoring = {
        "fields": {"nomor_kk": 0.98291, "nama_kepala_keluarga": 0.9512},
        "anggota_keluarga": [_member_scores()],
        **scoring_extra,
    }
    return dict(final_result("kk", GUARDRAILS, cast(StructuringResult, structuring), cast(ScoringResult, scoring)))


def test_scoring_done_carries_exactly_the_200_data_and_guardrails_0():
    stage_body = stage_callback_body(RID, "SCORING", "DONE", result=_final(), final=True, answer=ANSWER)

    assert result_callback_body(stage_body) == {
        "request_id": RID,
        "status": "completed",
        "result": ANSWER,
        "guardrails": 0,
    }


def test_a_sequence_that_ends_early_carries_that_stage_result_as_the_200_does():
    structuring = {"nomor_kk": _f("1"), "anggota_keluarga": [], "reject_reason": None}
    stage_body = stage_callback_body(RID, "STRUCTURING", "DONE", result=structuring, final=True)

    assert result_callback_body(stage_body) == {
        "request_id": RID,
        "status": "completed",
        "result": structuring,
        "guardrails": 0,
    }


def test_a_scoring_body_queued_before_answer_existed_is_projected_from_its_decisions():
    decisions = {
        "no_kk": {**ANSWER["no_kk"], "threshold": None},
        "nama_kepala_keluarga": {**ANSWER["nama_kepala_keluarga"], "threshold": 0.8},
        "anggota_keluarga": [
            {name: {**field, "threshold": 0.9} for name, field in ANSWER["anggota_keluarga"][0].items()}
        ],
    }
    body = result_callback_body(stage_callback_body(RID, "SCORING", "DONE", result=_final(decisions=decisions)))

    assert body is not None
    assert (body["result"], body["guardrails"]) == (ANSWER, 0)


def test_a_scoring_body_without_decisions_gets_the_0_1_data_at_the_default_threshold():
    body = result_callback_body(stage_callback_body(RID, "SCORING", "DONE", result=_final()))

    assert body is not None
    result = body["result"]
    assert result["no_kk"] == {"value": "3273012345678901", "confidence": 1}
    assert result["anggota_keluarga"][0]["ibu"] == {"value": "SITI AMINAH", "confidence": 0}
    assert result["anggota_keluarga"][0]["status_hubungan_dalam_rumah_tangga"]["confidence"] == 1


def test_a_rejection_is_completed_with_null_result_guardrails_1_and_the_reason():
    reason = "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil extraction tidak lengkap"
    body = result_callback_body(
        stage_callback_body(
            RID, "STRUCTURING", "FAILED", error_message=reason, error_code="DOWNSTREAM_VALIDATION_ERROR"
        )
    )

    assert body == {
        "request_id": RID,
        "status": "completed",
        "result": None,
        "guardrails": 1,
        "message": reason,
        "error_code": "DOWNSTREAM_VALIDATION_ERROR",
    }


@pytest.mark.parametrize(
    ("stage", "error_message", "expected_code"),
    [
        ("OCR", "extraction OCR model is unavailable", "OCR_FAILED"),
        ("SCORING", "Internal error in SCORING stage", "SCORING_FAILED"),
    ],
)
def test_a_failed_stage_becomes_the_failed_result_callback(stage, error_message, expected_code):
    body = result_callback_body(stage_callback_body(RID, stage, "FAILED", error_message=error_message))

    assert body == {"request_id": RID, "status": "failed", "error_code": expected_code, "message": error_message}


@pytest.mark.parametrize("stage", ["OCR", "STRUCTURING"])
def test_a_stage_that_does_not_end_the_request_sends_nothing(stage):
    assert result_callback_body(stage_callback_body(RID, stage, "DONE", result={"texts": []})) is None


async def test_the_stage_callback_never_sends_the_answer():
    seen: list[httpx.Request] = []
    callback = OrchestrationCallback(_recording_client(seen), "/v1/callbacks/stage")

    await callback.send(stage_callback_body(RID, "SCORING", "DONE", result={"x": 1}, final=True, answer=ANSWER))

    [request] = seen
    assert "answer" not in json.loads(request.content)


def _recording_client(seen: list[httpx.Request], statuses: list[tuple[int, dict]] | None = None) -> RemoteModelClient:
    answers = list(statuses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = answers.pop(0) if answers else (200, {})
        return httpx.Response(status, json=body)

    return RemoteModelClient(
        "http://ocr-orchestration.ocr-dev.svc.cluster.local",
        1.0,
        name="orchestration result callback",
        headers={"X-Callback-Key": "secret"},
        passthrough_client_errors=True,
        transport=httpx.MockTransport(handler),
    )


def _central_error(status: int, code: str) -> tuple[int, dict]:
    """An error answer in the central orchestrator's envelope."""
    return status, {"status_code": status, "status_desc": "x", "message": code, "data": None, "errors": code}


async def test_notify_posts_the_result_with_the_callback_key_once_the_request_ends():
    seen: list[httpx.Request] = []
    callback = ResultCallback(_recording_client(seen), PATH, attempts=1, delay=0)

    assert await callback.notify(RID, "OCR", "DONE", result={"texts": []}) is False
    assert await callback.notify(RID, "SCORING", "DONE", result=_final(), final=True, answer=ANSWER)

    [request] = seen
    assert request.url.path == PATH
    assert request.headers["X-Callback-Key"] == "secret"
    assert json.loads(request.content) == {"request_id": RID, "status": "completed", "result": ANSWER, "guardrails": 0}


async def test_notify_sends_again_while_the_orchestrator_has_not_recorded_the_202(monkeypatch):
    monkeypatch.setattr(callbacks, "NOT_READY_DELAY_SECONDS", 0)
    seen: list[httpx.Request] = []
    client = _recording_client(seen, [_central_error(409, "RESULT_NOT_READY")] * 2)
    callback = ResultCallback(client, PATH, attempts=1, delay=0)

    assert await callback.notify(RID, "SCORING", "DONE", final=True, answer=ANSWER) is True
    assert len(seen) == 3


async def test_notify_gives_up_after_the_not_ready_retries(monkeypatch):
    monkeypatch.setattr(callbacks, "NOT_READY_DELAY_SECONDS", 0)
    seen: list[httpx.Request] = []
    client = _recording_client(seen, [_central_error(409, "RESULT_NOT_READY")] * 10)
    callback = ResultCallback(client, PATH, attempts=1, delay=0)

    assert await callback.notify(RID, "SCORING", "DONE", final=True, answer=ANSWER) is False
    assert len(seen) == 1 + callbacks.NOT_READY_RETRIES


async def test_notify_does_not_send_again_on_any_other_409():
    seen: list[httpx.Request] = []
    client = _recording_client(seen, [_central_error(409, "RESULT_CONFLICT")])
    callback = ResultCallback(client, PATH, attempts=1, delay=0)

    assert await callback.notify(RID, "SCORING", "DONE", final=True, answer=ANSWER) is False
    assert len(seen) == 1


def test_only_a_409_result_not_ready_from_the_remote_is_not_ready():
    assert not_ready(RemoteClientError(409, "x", "RESULT_NOT_READY"))
    assert not not_ready(RemoteClientError(409, "x", "CALLBACK_NOT_EXPECTED"))
    assert not not_ready(ServiceError(409, "x", "RESULT_NOT_READY"))


async def test_outbox_send_turns_the_stored_stage_body_into_the_result():
    seen: list[httpx.Request] = []
    callback = ResultCallback(_recording_client(seen), PATH)

    await callback.send(stage_callback_body(RID, "STRUCTURING", "DONE", result={"anggota_keluarga": []}))
    await callback.send(stage_callback_body(RID, "STRUCTURING", "FAILED", error_message="parser crashed"))

    [request] = seen
    assert json.loads(request.content) == {
        "request_id": RID,
        "status": "failed",
        "error_code": "STRUCTURING_FAILED",
        "message": "parser crashed",
    }


def _settings(**overrides) -> PipelineSettings:
    return PipelineSettings(api_key=TEST_API_KEY, environment="local", _env_file=None, **overrides)


def test_the_callback_format_picks_the_callback_class():
    assert isinstance(build_callback(_settings(orchestration_url="http://orch")), OrchestrationCallback)
    result = build_callback(
        _settings(
            orchestration_url="http://orch", orchestration_callback_format="result", orchestration_callback_key="s"
        )
    )
    assert isinstance(result, ResultCallback)


def test_the_switch_turns_callbacks_off_even_with_a_url():
    assert _settings(orchestration_url="http://orch").callbacks_enabled
    assert not _settings(orchestration_url="http://orch", orchestration_callback_enabled=False).callbacks_enabled
    assert not _settings().callbacks_enabled


def _production(callback_key: str | None = None, **overrides) -> PipelineSettings:
    values: dict[str, Any] = {
        "orchestration_url": "http://ocr-orchestration.ocr-dev.svc.cluster.local",
        "orchestration_callback_format": "result",
        "orchestration_callback_key": callback_key,
        **overrides,
    }
    return PipelineSettings(
        api_key=TEST_API_KEY,
        environment="production",
        database_url="postgresql+asyncpg://u:p@db/x",
        _env_file=None,
        **values,
    )


def test_the_result_format_needs_the_callback_key_outside_local():
    with pytest.raises(ValidationError, match="ORCHESTRATION_CALLBACK_KEY must be set"):
        _production(None)
    assert _production("secret").orchestration_callback_key == "secret"


def test_with_the_switch_off_neither_the_key_nor_a_url_is_needed():
    settings = _production(None, orchestration_callback_enabled=False)
    assert not settings.callbacks_enabled
    assert not _production(None, orchestration_url=None, orchestration_callback_enabled=False).callbacks_enabled


def test_with_the_switch_on_a_production_stage_still_needs_a_way_to_report():
    with pytest.raises(ValidationError, match="ORCHESTRATION_CALLBACK_ENABLED=false"):
        _production(None, orchestration_url=None)

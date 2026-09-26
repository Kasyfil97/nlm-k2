import json

import httpx
import pytest
from pydantic import ValidationError

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.config import PipelineSettings
from ocr_common.kk import final_result
from ocr_common.pipeline.callbacks import (
    OrchestrationCallback,
    ResultCallback,
    result_callback_body,
    stage_callback_body,
)
from ocr_common.pipeline.factory import build_callback

RID = "OCR_9cb01af2-493d-446d-b191-af120333f6d0"
GUARDRAILS = {"passed": True, "reason": None, "document": {"verdict": "accepted", "confidence": 0.98}}
PATH = "/v1/ocr-callback"


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
        "ibu": 0.7951,
    }
    return {**scores, **overrides}


def _final(nomor_kk="3273012345678901", kk_confidence=0.98291, members=None, member_scores=None):
    structuring = {
        "nomor_kk": _f(nomor_kk),
        "nama_kepala_keluarga": _f("BUDI SANTOSO"),
        "anggota_keluarga": [_member()] if members is None else members,
        "reject_reason": None,
    }
    scoring = {
        "fields": {"nomor_kk": kk_confidence, "nama_kepala_keluarga": 0.9512},
        "anggota_keluarga": [_member_scores()] if member_scores is None else member_scores,
    }
    return dict(final_result("kk", GUARDRAILS, structuring, scoring))


def test_scoring_done_becomes_the_completed_result_callback():
    body = result_callback_body(stage_callback_body(RID, "SCORING", "DONE", result=_final()))

    assert body == {
        "request_id": RID,
        "status": "completed",
        "result": {
            "no_kk": {"value": "3273012345678901", "confidence": 0.9829},
            "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "confidence": 0.9512},
            "anggota_keluarga": [
                {
                    "nama_lengkap": {"value": "BUDI SANTOSO", "confidence": 0.9702},
                    "nik": {"value": "3273011203850001", "confidence": 0.9655},
                    "pendidikan": {"value": "S1", "confidence": 0.841},
                    "jenis_pekerjaan": {"value": "KARYAWAN SWASTA", "confidence": 0.7733},
                    "status_hubungan_dalam_rumah_tangga": {"value": "KEPALA KELUARGA", "confidence": 0.9218},
                    "ayah": {"value": "SUTRISNO", "confidence": 0.8064},
                    "ibu": {"value": "SITI AMINAH", "confidence": 0.7951},
                }
            ],
        },
        "guardrails": GUARDRAILS,
    }


def test_the_callback_keeps_the_raw_probability_not_the_zero_one_flag():
    """`data` of extract-ocr carries 0/1; this callback carries the number, because the threshold
    belongs to the caller."""
    body = result_callback_body(stage_callback_body(RID, "SCORING", "DONE", result=_final()))

    assert body is not None
    assert body["result"]["anggota_keluarga"][0]["ibu"]["confidence"] == 0.7951


def test_member_scores_stay_with_their_own_member():
    two = [_member(nama="BUDI SANTOSO"), _member(nama="SITI NURHALIZA", nik="3273015506880002")]
    scores = [_member_scores(), _member_scores(nama_lengkap=0.1234)]
    body = result_callback_body(
        stage_callback_body(RID, "SCORING", "DONE", result=_final(members=two, member_scores=scores))
    )

    assert body is not None
    first, second = body["result"]["anggota_keluarga"]
    assert first["nama_lengkap"] == {"value": "BUDI SANTOSO", "confidence": 0.9702}
    assert second["nama_lengkap"] == {"value": "SITI NURHALIZA", "confidence": 0.1234}


def test_a_field_that_was_not_found_is_empty_with_confidence_0():
    body = result_callback_body(
        stage_callback_body(RID, "SCORING", "DONE", result=_final(nomor_kk="", kk_confidence=None))
    )

    assert body is not None
    assert body["result"]["no_kk"] == {"value": "", "confidence": 0.0}


@pytest.mark.parametrize(
    ("stage", "error_message", "error_code", "expected_code"),
    [
        ("OCR", "ekstraksi OCR model is unavailable", None, "OCR_FAILED"),
        ("SCORING", "Internal error in SCORING stage", None, "SCORING_FAILED"),
        (
            "STRUCTURING",
            "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil ekstraksi tidak lengkap",
            "DOWNSTREAM_VALIDATION_ERROR",
            "DOWNSTREAM_VALIDATION_ERROR",
        ),
    ],
)
def test_a_failed_stage_becomes_the_failed_result_callback(stage, error_message, error_code, expected_code):
    body = result_callback_body(
        stage_callback_body(RID, stage, "FAILED", error_message=error_message, error_code=error_code)
    )

    assert body == {
        "request_id": RID,
        "status": "failed",
        "result": None,
        "guardrails": {},
        "error_code": expected_code,
        "error_message": error_message,
    }


@pytest.mark.parametrize("stage", ["OCR", "STRUCTURING"])
def test_a_stage_that_does_not_end_the_request_sends_nothing(stage):
    assert result_callback_body(stage_callback_body(RID, stage, "DONE", result={"texts": []})) is None


def _recording_client(seen: list[httpx.Request], status: int = 200) -> RemoteModelClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={})

    return RemoteModelClient(
        "http://ocr-orchestration.ocr-dev.svc.cluster.local",
        1.0,
        name="orchestration result callback",
        headers={"X-Callback-Key": "secret"},
        passthrough_client_errors=True,
        transport=httpx.MockTransport(handler),
    )


async def test_notify_posts_the_result_with_the_callback_key_once_the_request_ends():
    seen: list[httpx.Request] = []
    callback = ResultCallback(_recording_client(seen), PATH, attempts=1, delay=0)

    assert await callback.notify(RID, "OCR", "DONE", result={"texts": []}) is False
    assert await callback.notify(RID, "SCORING", "DONE", result=_final()) is True

    [request] = seen
    assert request.url.path == PATH
    assert request.headers["X-Callback-Key"] == "secret"
    assert json.loads(request.content)["status"] == "completed"


async def test_outbox_send_turns_the_stored_stage_body_into_the_result():
    seen: list[httpx.Request] = []
    callback = ResultCallback(_recording_client(seen), PATH)

    await callback.send(stage_callback_body(RID, "STRUCTURING", "DONE", result={"anggota_keluarga": []}))
    await callback.send(
        stage_callback_body(RID, "STRUCTURING", "FAILED", error_message="dokumen blur / blank", error_code="X")
    )

    [request] = seen
    assert json.loads(request.content) == {
        "request_id": RID,
        "status": "failed",
        "result": None,
        "guardrails": {},
        "error_code": "X",
        "error_message": "dokumen blur / blank",
    }


def _settings(**overrides) -> PipelineSettings:
    return PipelineSettings(api_key="k", environment="local", _env_file=None, **overrides)


def test_the_callback_format_picks_the_callback_class():
    assert isinstance(build_callback(_settings(orchestration_url="http://orch")), OrchestrationCallback)
    result = build_callback(
        _settings(
            orchestration_url="http://orch", orchestration_callback_format="result", orchestration_callback_key="s"
        )
    )
    assert isinstance(result, ResultCallback)


def _production(callback_key: str | None) -> PipelineSettings:
    return PipelineSettings(
        api_key="k",
        environment="production",
        database_url="postgresql+asyncpg://u:p@db/x",
        orchestration_url="http://ocr-orchestration.ocr-dev.svc.cluster.local",
        orchestration_callback_format="result",
        orchestration_callback_key=callback_key,
        _env_file=None,
    )


def test_the_result_format_needs_the_callback_key_outside_local():
    with pytest.raises(ValidationError, match="ORCHESTRATION_CALLBACK_KEY must be set"):
        _production(None)
    assert _production("secret").orchestration_callback_key == "secret"

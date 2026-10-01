from collections.abc import Mapping
from typing import Any

from ocr_common.kk import REJECTED_CODE, contract_data, contract_fields
from ocr_common.pipeline import EXTRACTION, GUARDRAILS, SERVICE_OF_STAGE, STAGE_SCORING, STATUS_DONE, STATUS_FAILED
from ocr_common.web.envelope import envelope

from app.services.pipeline_waiter import STATUS_REJECTED

COMPLETED_MESSAGE = "OCR extraction completed successfully"
PROCESSING_MESSAGE = "OCR job accepted; still processing"


def extract_body(
    status_code: int,
    message: str,
    *,
    request_id: str | None,
    document_type: str | None,
    data: Mapping[str, Any] | None = None,
    errors: str | None = None,
    job_status: str | None = None,
    guardrails: int | None = None,
    params: Any = None,
    pipeline_last_stage: str | None = None,
) -> dict[str, Any]:
    return {
        **envelope(status_code, message, dict(data) if data is not None else None, request_id, errors=errors),
        "document_type": document_type,
        "job_status": job_status,
        "guardrails": guardrails,
        "pipeline_last_stage": pipeline_last_stage,
        "params": params,
    }


def last_stage(outcome: dict[str, Any]) -> str | None:
    """The pipeline service this answer comes from: guardrails when it rejected, else the stage the
    pipeline reached (the one that finished it, failed, rejected, or is still running), and extraction
    right after the hand-off when there was no wait."""
    if not outcome["passed"]:
        return GUARDRAILS
    pipeline = outcome.get("pipeline")
    if pipeline:
        return SERVICE_OF_STAGE.get(pipeline["stage"])
    return EXTRACTION if outcome.get("job") else None


def extract_response(
    outcome: dict[str, Any],
    *,
    request_id: str,
    document_type: str,
    params: Any,
    threshold: float,
    column_thresholds: Mapping[str, float] | None = None,
) -> tuple[int, dict[str, Any]]:
    """`threshold` is FIELD_CONFIDENCE_THRESHOLD and `column_thresholds` the central orchestrator's per-field
    ones; both only matter for a scoring result stored without `decisions` (see `_contract_data`)."""
    stage_name = last_stage(outcome)
    if not outcome["passed"]:
        body = extract_body(
            400,
            outcome["reason"],
            errors=REJECTED_CODE,
            job_status="failed",
            guardrails=1,
            request_id=request_id,
            document_type=document_type,
            params=params,
            pipeline_last_stage=stage_name,
        )
        return 400, body
    pipeline = outcome["pipeline"] or {}
    if pipeline.get("status") == STATUS_REJECTED:
        # A rejecting rule of the KK validity gate: answered like a guardrails rejection, with the
        # gate's own Indonesian reason as the message.
        return 400, extract_body(
            400,
            pipeline["error_message"],
            errors=REJECTED_CODE,
            job_status="failed",
            guardrails=1,
            request_id=request_id,
            document_type=document_type,
            params=params,
            pipeline_last_stage=stage_name,
        )
    if pipeline.get("status") == STATUS_DONE:
        # Scoring ended the request: `result` is the FinalResult, whose two stage payloads are kept whole,
        # so the projection takes both -- the very function the scoring stage calls (§8.5). An earlier
        # last service of the pipeline_name_sequence: its result, as it is.
        final = outcome["result"]
        if pipeline.get("stage") == STAGE_SCORING:
            data = _contract_data(final, threshold, column_thresholds)
        else:
            data = final
        return 200, extract_body(
            200,
            COMPLETED_MESSAGE,
            data=data,
            job_status="completed",
            guardrails=0,
            request_id=request_id,
            document_type=document_type,
            params=params,
            pipeline_last_stage=stage_name,
        )
    if pipeline.get("status") == STATUS_FAILED:
        stage = pipeline["stage"]
        message = pipeline.get("error_message") or f"{stage} stage failed"
        return 422, extract_body(
            422,
            message,
            errors=f"{stage}_FAILED",
            job_status="failed",
            guardrails=0,
            request_id=request_id,
            document_type=document_type,
            params=params,
            pipeline_last_stage=stage_name,
        )
    return 202, extract_body(
        202,
        PROCESSING_MESSAGE,
        job_status="processing",
        request_id=request_id,
        document_type=document_type,
        params=params,
        pipeline_last_stage=stage_name,
    )


def _contract_data(
    final: Mapping[str, Any], threshold: float, column_thresholds: Mapping[str, float] | None
) -> Mapping[str, Any]:
    """The nine `{value, confidence 0|1}` fields. From the decisions scoring stored with its result when there
    are any -- the same ones the outcome row was written from, so the POST, the GET and the row cannot
    disagree -- else decided here, for a result stored before `decisions` existed."""
    decisions = (final.get("scoring") or {}).get("decisions")
    if decisions:
        return contract_data(decisions)
    return contract_fields(final["structuring"], final["scoring"], threshold, column_thresholds)

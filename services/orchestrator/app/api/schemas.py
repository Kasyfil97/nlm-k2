from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatus = Literal["pending", "processing", "completed", "failed"]


class ContractField(BaseModel):
    value: str = Field(
        "",
        description='The value read; `""` when the field was not found -- never null, and the object itself '
        "is never replaced by null",
        examples=["3273012345678901"],
    )
    confidence: Literal[0, 1] = Field(
        ...,
        description=(
            "1 when the trust model's probability that this value is exactly correct reaches the field's "
            "threshold, else 0 (also when there is no value), as in nilam. The threshold: the request's "
            "`column_confidence_threshold` for this field, else the trust model's own for it (the point above "
            "which every held-out sample was correct). A field the model has no threshold for (`no_kk`) is 0 "
            "unless the request gives it one"
        ),
        examples=[1],
    )


class KkMember(BaseModel):
    """One row of `anggota_keluarga`. All seven keys are always present."""

    nama_lengkap: ContractField
    nik: ContractField
    pendidikan: ContractField
    jenis_pekerjaan: ContractField
    status_hubungan_dalam_rumah_tangga: ContractField = Field(
        ...,
        description=(
            "The card itself prints STATUS HUBUNGAN DALAM KELUARGA; this contract says "
            "`...dalam_rumah_tangga` because the consumer asked for it. Internally the field keeps the "
            "card's name -- one of only two keys the projection renames"
        ),
    )
    ayah: ContractField
    ibu: ContractField


class KkData(BaseModel):
    """`data` of a completed request: nine fields, and nothing else.

    Structuring extracts 11 document fields and 15 per member; only these leave. The rest stay in
    `structuring_results` and are readable through `GET /v1/structuring/jobs/{request_id}`.
    """

    no_kk: ContractField = Field(..., description="The 16-digit KK number")
    nama_kepala_keluarga: ContractField = Field(..., description="Name of the head of the household")
    anggota_keluarga: list[KkMember] = Field(
        ..., description="One entry per member, in the order printed on the card; may be empty"
    )


class ExtractOcrResponse(BaseModel):
    status_code: int = Field(..., description="Same as the HTTP status code", examples=[200])
    status_desc: str = Field(..., description="Reason phrase of `status_code`", examples=["OK"])
    message: str = Field(
        ...,
        description="Human-readable; its wording may change, so branch on `errors` instead",
        examples=["OCR extraction completed successfully"],
    )
    data: KkData | dict[str, Any] | None = Field(
        None,
        description=(
            "The result when `job_status` is `completed`; null otherwise. The nine fields when scoring ended the "
            "request; the result of the last service of `pipeline_name_sequence`, as it is, when the sequence "
            "ends earlier (the guardrails report, the OCR result, or the structuring result)"
        ),
    )
    errors: str | None = Field(
        None, description="Failure code when the request failed or was refused; null otherwise", examples=[None]
    )
    request_id: str | None = Field(None, description="The request_id this response belongs to")
    pipeline_last_stage: Literal["orchestrator", "guardrails", "ekstraksi", "structuring", "scoring"] | None = Field(
        None,
        description=(
            "The service this answer comes from, named as in `pipeline_name_sequence`: the last service of the "
            "sequence when `completed`; the one that rejected (`guardrails`, `structuring`) or failed; the one "
            "still running on 202; the one that could not be reached or answered an error. `orchestrator` when "
            "this service refused the request itself before calling any pipeline service (file checks, "
            "`pipeline_name_sequence`, thresholds, `params`, `document_type`)"
        ),
        examples=["scoring"],
    )
    document_type: str | None = Field(None, description="Document type of the request", examples=["kk"])
    job_status: JobStatus | None = Field(
        None,
        description=(
            "`completed` (200), `processing` (202), or `failed`; null when the request was refused before "
            "anything was processed"
        ),
        examples=["completed"],
    )
    guardrails: Literal[0, 1] | None = Field(
        None,
        description=(
            "0: the document passed the guardrails model (and the pipeline ran); 1: it was rejected by the "
            "guardrails model or by the KK validity gate. Null on 202, and when the request was refused before "
            "the check"
        ),
        examples=[0],
    )
    params: Any = Field(
        None,
        description=(
            "The `params` sent with `POST /v1/extract-ocr`, returned unchanged; null when not sent. Not stored, so "
            "always null on `GET /v1/extract-ocr/{request_id}`"
        ),
        examples=[{"nik": "9901011203850001", "refno": "PK19039Y8U"}],
    )

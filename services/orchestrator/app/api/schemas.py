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
            "1 when the trust model gives this value a probability of being correct of at least "
            "`FIELD_CONFIDENCE_THRESHOLD` (0.5 by default); 0 when it is lower, or when there is no value"
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
    data: KkData | None = Field(None, description="The OCR fields when `job_status` is `completed`; null otherwise")
    errors: str | None = Field(
        None, description="Failure code when the request failed or was refused; null otherwise", examples=[None]
    )
    request_id: str | None = Field(None, description="The request_id this response belongs to")
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

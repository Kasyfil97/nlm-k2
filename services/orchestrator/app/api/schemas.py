from typing import Any, Literal

from pydantic import BaseModel, Field


class ContractField(BaseModel):
    value: str = Field(
        "",
        description='The value read; `""` when the field was not found -- never null, and the object itself '
        "is never replaced by null",
        examples=["3273012345678901"],
    )
    confidence: int | float = Field(
        ...,
        ge=0,
        le=1,
        description=(
            "With a threshold for this field in the request's `column_confidence_threshold` (its own key or "
            "`all_field`): 1 when the trust model's probability that this value is exactly correct reaches it, "
            "else 0 (also when there is no value), as in nilam. **Without a threshold**: that probability itself, "
            "a float from 0 to 1 as the model gave it (0 when there is no value)"
        ),
        examples=[1, 0.9731],
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
    `nilam_structuring_results` and are readable through `GET /v1/structuring/jobs/{request_id}`.
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
            "The result on 200 (finished); null otherwise. The nine fields when scoring ended the "
            "request; the result of the last service of `pipeline_name_sequence`, as it is, when the sequence "
            "ends earlier (the guardrails report, the OCR result, or the structuring result)"
        ),
    )
    errors: str | None = Field(
        None, description="Failure code when the request failed or was refused; null otherwise", examples=[None]
    )
    request_id: str | None = Field(None, description="The request_id this response belongs to")
    pipeline_last_stage: Literal["orchestrator", "guardrails", "extraction", "structuring", "scoring"] | None = Field(
        None,
        description=(
            "Null on a success answer (200 finished, 202 still running). On an error, the service it comes "
            "from, named as in `pipeline_name_sequence`: the one that rejected (`guardrails`, `structuring`) or "
            "failed; the one that could not be reached or answered an error. `orchestrator` when this service "
            "refused the request itself before calling any pipeline service (file checks, "
            "`pipeline_name_sequence`, thresholds, `document_type`, an unknown request_id)"
        ),
        examples=["structuring"],
    )
    guardrails: int | float | None = Field(
        None,
        description=(
            "With `guardrails_confidence_threshold`: 0 the document passed the guardrails model (and the pipeline "
            "ran). Without it (left out, `{}`, or only null values) the document is accepted whatever the model "
            "says, and this is the model's accepted probability, `1 - probability_bad`, a float from 0 to 1 "
            "(4 decimals). 1: rejected, by the guardrails model (with a threshold, or an image it could not "
            "assess) or by the KK validity gate. Null on 202, and when the request was refused before the check"
        ),
        examples=[0, 0.9713],
    )

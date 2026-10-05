"""Pydantic models of the payloads passed between stages and to the orchestrator, used by the routes
for validation and by the OpenAPI documents.

These are the twins of the TypedDicts in `ocr_common.types`: those annotate what moves in memory,
these validate what arrives over HTTP. Every model allows extra keys, so adding a field upstream
never breaks a downstream service.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ocr_common.web.schemas import REQUEST_ID_EXAMPLE, Stage

Verdict = Literal["accepted", "reject", "unassessable"]
"""`unassessable` is the answer for an image the model could not judge at all -- undecodable, or
dimensions outside the range the checkpoint was trained on. It is deliberately a verdict rather than
an error: §5.2 requires guardrails to answer 200 with the verdict in `data.passed`, and the model
core raises on this path in a dozen places. Keeping it distinct from `reject` matters operationally
-- "we judged this bad" and "we could not judge" need different alerts and different fixes."""


class _Forwarded(BaseModel):
    model_config = ConfigDict(extra="allow")


Coordinate = Annotated[list[float], Field(min_length=2, max_length=2)]
"""One point of a `poly`: exactly two numbers.

The field said "four points of two coordinates" and only checked the four. A point could carry ten
thousand numbers and pass -- which matters because these polygons are the one part of the payload
whose size nothing else bounds, and structuring walks every one of them. Enforcing the second half
of the sentence is a bug fix, not a new limit: a three-coordinate point was never a valid §7.1 box.
"""


# --- guardrails (§5.2) ---------------------------------------------------------------------


class GuardrailsDocument(_Forwarded):
    """The guardrails verdict. A Kartu Keluarga is always a single image, so there is no page
    concept here and no aggregation across pages."""

    verdict: Verdict | None = Field(None, description="Document verdict", examples=["accepted"])
    confidence: float | None = Field(
        None,
        ge=0,
        le=1,
        description=(
            "Confidence in the verdict: `probability_bad` when rejected, `1 - probability_bad` when accepted. "
            "Null when the image could not be assessed"
        ),
        examples=[0.9713],
    )
    probability_bad: float | None = Field(
        None,
        ge=0,
        le=1,
        description=(
            "Raw model output. Travels unchanged to scoring as the `guardrail_probability` feature. "
            "Null when `verdict` is `unassessable`, which scoring must handle rather than assume a number"
        ),
        examples=[0.0287],
    )
    threshold_used: float | None = Field(
        None,
        gt=0,
        lt=1,
        description=(
            "The threshold in force at the moment of the judgement. Echoed because it can be changed at the "
            "central orchestrator without a deploy here, so the verdict records the one it actually used"
        ),
        examples=[0.5],
    )
    threshold_target: Literal["accept", "reject"] | None = Field(
        None,
        description="The side `threshold_used` applied to: `reject` (the default) or `accept` (on 1 - probability_bad)",
        examples=["reject"],
    )


class GuardrailsResult(_Forwarded):
    """The guardrails report, forwarded unchanged down the pipeline."""

    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "The guardrails report (`data` of the guardrails service's POST /v1/guardrails/check, sent on by "
                "the orchestrator), forwarded unchanged down the pipeline."
            )
        }
    )

    passed: bool | None = Field(None, description="true: the document may proceed to OCR", examples=[True])
    reason: str | None = Field(
        None, description="Why it was rejected, in Indonesian; null when passed", examples=[None]
    )
    document: GuardrailsDocument | None = Field(None, description="The verdict and its numbers")


# --- OCR (§6.1 / §7.1 / §10) ---------------------------------------------------------------


class OcrBoxPayload(_Forwarded):
    """One recognised text line."""

    text: str = Field(..., description="The recognised text", examples=["9924187486671285"])
    score: float = Field(..., ge=0, le=1, description="Recognition score of this line", examples=[0.9991])
    poly: list[Coordinate] = Field(
        ...,
        min_length=4,
        max_length=4,
        description=(
            "Four points of two coordinates, in pixels. A genuine quadrilateral, not an upright box: the "
            "detector returns visibly tilted quads on real cards, so an x1/y1/x2/y2 rectangle would lose "
            "information. Structuring depends on this geometry to assign cells to columns"
        ),
        examples=[[[291.0, 7.0], [306.0, 6.0], [309.0, 26.0], [294.0, 28.0]]],
    )


class OcrPayload(_Forwarded):
    """The OCR stage result as the next stages receive it."""

    model_config = ConfigDict(
        json_schema_extra={
            "description": "Result of the OCR stage (`nilam_ocr_results`), forwarded to structuring and scoring."
        }
    )

    engine: str | None = Field(None, description="OCR backend that produced the boxes", examples=["kk_ocr"])
    model: str | None = Field(
        None,
        description="Model identity reported by the OCR backend",
        examples=["PP-OCRv5_server_det+PP-OCRv5_server_rec"],
    )
    elapsed_ms: float | None = Field(None, description="Time spent in the OCR model", examples=[4182.7])
    text_regions_count: int = Field(0, ge=0, description="Number of text boxes; may be 0", examples=[177])
    avg_doc_score: float | None = Field(
        None,
        ge=0,
        le=1,
        description="Mean of `texts[].score`; null when `texts` is empty rather than 0",
        examples=[0.814],
    )
    min_doc_score: float | None = Field(
        None, ge=0, le=1, description="Lowest of `texts[].score`; null when `texts` is empty", examples=[0.2822]
    )
    texts: list[OcrBoxPayload] = Field(
        default_factory=list,
        description=(
            "Text boxes. May be EMPTY: an image with no readable text must reach the structuring rules to be "
            "rejected there, so neither this model nor the structuring job endpoint may require at least one"
        ),
    )


# --- structuring (§7.3) --------------------------------------------------------------------


class StructuredFieldPayload(_Forwarded):
    """One named field read by structuring, carrying two scores that are deliberately not fused."""

    value: str = Field(
        "",
        description='The value; `""` when not found -- never null',
        examples=["3273012345678901"],
    )
    ocr_conf: float | None = Field(
        None,
        ge=0,
        le=1,
        description="Lowest recognition score among the OCR boxes that formed this value; null when not found",
        examples=[0.9991],
    )
    crf_conf: float | None = Field(
        None,
        ge=0,
        le=1,
        description=(
            "Forward-backward marginal of the column placement. ALWAYS null for document fields, which are found "
            "by regex or position and never pass through Viterbi, and null for member cells Viterbi did not place"
        ),
        examples=[None],
    )
    features: dict[str, float] | None = Field(
        None,
        description=(
            "The trust model's input vector for this field, present only for the NINE scored contract fields "
            "and null everywhere else. It exists because the two scores above do not separate a correct value "
            "from a wrong one: over 1686 hand-labelled cells `crf_conf` scores AUC 0.502, no better than a "
            "coin, while the parser internals that do carry the signal are computed and then discarded. "
            "Names are pinned by `kk.MEMBER_CELL_FEATURES` (member cells) and `kk.DOC_CELL_FEATURES` "
            "(document fields); scoring assembles them and never recomputes one, because the same feature "
            "computed in two places is how a model ends up good in training and bad in production"
        ),
        examples=[{"marg_min": 0.9967, "margin_min": 0.9934, "emis_assigned_min": 0.0, "lebar_rel": 0.87}],
    )


class StructuredMemberPayload(_Forwarded):
    """One row of `anggota_keluarga`. All fifteen keys are always present, even when empty."""

    nama_lengkap: StructuredFieldPayload
    nik: StructuredFieldPayload
    jenis_kelamin: StructuredFieldPayload
    tempat_lahir: StructuredFieldPayload
    tanggal_lahir: StructuredFieldPayload
    agama: StructuredFieldPayload
    pendidikan: StructuredFieldPayload
    jenis_pekerjaan: StructuredFieldPayload
    golongan_darah: StructuredFieldPayload
    status_perkawinan: StructuredFieldPayload
    tanggal_perkawinan: StructuredFieldPayload
    status_hubungan_dalam_keluarga: StructuredFieldPayload
    kewarganegaraan: StructuredFieldPayload
    ayah: StructuredFieldPayload
    ibu: StructuredFieldPayload


class StructuringPayload(_Forwarded):
    """The structuring stage result as scoring receives it: the flat K2Regex-v2 shape.

    The eleven document fields are top-level keys -- there is no `fields` wrapper -- and there is no
    `document_type`, `flag` or `flag_reason`. The keys are spelled out rather than left as a mapping
    because this is the shape the freeze pins down, and a renamed key is exactly what the model
    should catch.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "description": "Result of the structuring stage (`nilam_structuring_results`), forwarded to scoring."
        }
    )

    nomor_kk: StructuredFieldPayload = Field(
        ..., description="The KK number. Note `nomor_kk`, not `no_kk`: the rename happens in the orchestrator"
    )
    nama_kepala_keluarga: StructuredFieldPayload
    alamat: StructuredFieldPayload
    desa_kelurahan: StructuredFieldPayload
    rt: StructuredFieldPayload = Field(
        ..., description="Shares one `ocr_conf` with `rw`: both are cut from the same OCR box"
    )
    rw: StructuredFieldPayload
    kecamatan: StructuredFieldPayload
    kabupaten_kota: StructuredFieldPayload
    provinsi: StructuredFieldPayload
    kode_pos: StructuredFieldPayload
    tanggal_dikeluarkan: StructuredFieldPayload
    anggota_keluarga: list[StructuredMemberPayload] = Field(
        default_factory=list,
        description="One entry per row on the card, in card order. May be empty, which the validity gate rejects",
    )
    reject_reason: str | None = Field(
        None,
        description=(
            "The first rejecting rule of the KK validity gate, in Indonesian; null when accepted. It lives in "
            "this payload rather than only in the outcome row because the orchestrator is stateless and reads "
            "stages through their API -- a reason kept elsewhere could never become the client's 400"
        ),
        examples=[None],
    )


# --- scoring (§8.3) ------------------------------------------------------------------------


class ScoringPayload(_Forwarded):
    """The trust model's per-field confidences.

    Scores only the nine contract fields, under their INTERNAL names; the rename to the outgoing
    contract happens in the orchestrator. `anggota_keluarga` is positionally aligned with the
    structuring result's list and must be the same length.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "description": "Trust model output: P(each extracted field is correct), fused and calibrated."
        }
    )

    document_type: str | None = Field(None, description="Document type the fields were read as", examples=["kk"])
    fields: dict[str, float | None] = Field(
        default_factory=dict,
        description="`nomor_kk` and `nama_kepala_keluarga`. Null for a field whose value is empty",
        examples=[{"nomor_kk": 0.9412, "nama_kepala_keluarga": 0.8871}],
    )
    anggota_keluarga: list[dict[str, float | None]] = Field(
        default_factory=list,
        description=(
            "Seven scores per member, positionally aligned with the structuring result. A length mismatch is a "
            "defect in this pipeline, not a property of the document, and is rejected rather than truncated"
        ),
    )
    model: str | None = Field(None, description="Identity of the trust model", examples=["kk-trust-isotonic-v1"])
    payload: dict[str, Any] | None = Field(
        None, description="Exactly what was scored, as an audit trail, so the numbers can be reproduced"
    )
    thresholds: dict[str, float] | None = Field(
        None,
        description=(
            "The confidence above which a field of that name was correct on every held-out sample -- one per "
            "scored field, keyed by INTERNAL name. They travel in the result rather than in configuration "
            "because they are a property of the trained model, not of the deployment, and because this is what "
            "makes the orchestrator's `confidence` and the outcome row's identical by construction instead of "
            "by keeping two env vars in step. A field absent here had no such point and is 0 unless the request's "
            "`column_confidence_threshold` gives it one; `FIELD_CONFIDENCE_THRESHOLD` applies only to a result "
            "that carries no thresholds at all (the `mock` backend)"
        ),
        examples=[{"nik": 0.9637, "ayah": 0.9353, "pendidikan": 0.9929}],
    )
    bin_edges: dict[str, list[float]] | None = Field(
        None,
        description=(
            "Bin boundaries low to high, under `fields` and `anggota_keluarga` -- the two model families have "
            "their own. Deliberately NOT equal width: the top bin is cut exactly where held-out precision "
            "reached 100%, which no fixed grid lands on. Kept for audit; the contract no longer carries a bin"
        ),
        examples=[{"anggota_keluarga": [0.0, 0.631, 0.776, 0.839, 0.899, 0.925, 0.943, 0.965, 0.979, 0.9874, 1.0]}],
    )
    decisions: dict[str, Any] | None = Field(
        None,
        description=(
            "The 0/1 decision per CONTRACT field, with the threshold that decided it: "
            "`{no_kk: {value, confidence, threshold}, nama_kepala_keluarga: {...}, anggota_keluarga: [{...}]}`. "
            "The outcome row and the `extract-ocr` answer are projected from it, so both say the same for one "
            "request. `threshold` null: no threshold applied, so `confidence` is 0"
        ),
    )


# --- final result and callbacks ------------------------------------------------------------


class FinalResult(BaseModel):
    """What the SCORING callback carries when the request completed."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "description": "What the pipeline produced for one request_id. Carried by the SCORING / DONE callback."
        },
    )

    document_type: str = Field(..., description="Document type the fields were read as", examples=["kk"])
    structuring: StructuringPayload = Field(
        ..., description="The structuring result whole: all 11 document fields and 15 per member"
    )
    scoring: ScoringPayload = Field(
        ...,
        description=(
            "Per-field trust for the nine contract fields. There is NO document-level score and NO "
            "approve / reject decision: thresholds belong to the caller"
        ),
    )
    guardrails: GuardrailsResult | None = Field(
        None, description="The guardrails result that was submitted with the job, returned unchanged"
    )


class StageCallback(BaseModel):
    """Body of the callback of the OCR and STRUCTURING stages."""

    model_config = ConfigDict(
        json_schema_extra={"description": "Body of the callback a stage POSTs to the orchestrator when it finishes."}
    )

    request_id: str = Field(
        ..., description="The request_id you submitted with `POST /v1/extraction/jobs`", examples=[REQUEST_ID_EXAMPLE]
    )
    stage: Stage = Field(
        ...,
        description=(
            "Stage the status is about. Usually the sender's own stage; when the hand-off to the NEXT stage "
            "fails after retries, the sender reports `status: FAILED` with the next stage's name, because that "
            "stage never received the job and cannot report for itself"
        ),
        examples=["OCR"],
    )
    status: Literal["DONE", "FAILED"] = Field(
        ..., description="Outcome of the stage. `FAILED` of any stage ends the request", examples=["DONE"]
    )
    result: None = Field(
        None, description="Always null for OCR and STRUCTURING; read the stage result from GET .../jobs/{request_id}"
    )
    error_message: str | None = Field(None, description="Why it failed; null when `status` is `DONE`", examples=[None])
    error_code: str | None = Field(
        None,
        description=(
            "Only on a rejection: `DOWNSTREAM_VALIDATION_ERROR` when the KK validity gate rejected the document "
            "(`error_message` is the Indonesian reason). Absent when a stage broke"
        ),
        examples=[None],
    )


class ScoringStageCallback(StageCallback):
    """Body of the SCORING callback: the same, plus the final result."""

    stage: Literal["SCORING"] = Field("SCORING", description="Always `SCORING`: the last stage", examples=["SCORING"])
    result: FinalResult | None = Field(  # type: ignore[assignment]
        None, description="The final result of the request when `status` is `DONE`; null when `FAILED`"
    )

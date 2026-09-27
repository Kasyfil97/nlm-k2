"""The shapes of the data that travels between the stages, as TypedDicts.

They are the Python-side twin of the Pydantic payloads in `ocr_common.pipeline.schemas`: the schemas
validate what arrives over HTTP, these annotate what the engines, services and the pipeline pass
around in memory. At runtime they are plain dicts, so nothing changes in what is stored or sent.

Two shapes here differ structurally from a single-value document such as a tax card, and both are
load-bearing:

* A Kartu Keluarga carries a **variable-length member list**, so `anggota_keluarga` is a list at every
  hop -- structuring, scoring and the outgoing contract. The lists are **positionally aligned**; a
  length mismatch is an error, never a silent truncation (`kk.contract_fields`).
* Every structured field carries **two scores**, not one. They fail differently and are deliberately
  not fused: a low `ocr_conf` means the recogniser doubted the glyphs, a low `crf_conf` means the text
  was read but its column placement is unclear. Fusing them into one P(correct) is scoring's job.
"""

from typing import Any, NotRequired, TypedDict


class OcrBox(TypedDict):
    """One recognised text line.

    `poly` is four points of two coordinates, not an upright box: the detector returns genuine
    quadrilaterals (observed tilt of several degrees on a real card), so an `x1/y1/x2/y2` rectangle
    cannot represent it without losing information.
    """

    text: str
    score: float
    poly: list[list[float]]


class OcrEngineResult(TypedDict):
    """What an OCR engine (`app/ml/*` of ekstraksi) returns."""

    texts: list[OcrBox]
    model: str | None


class OcrResult(OcrEngineResult):
    """The stored result of the OCR stage (`ocr_results.result`), forwarded to structuring and scoring.

    The aggregates are derived from `texts` rather than reported by the model, and are `None` for an
    empty `texts` -- an image with no readable text is a rejection at structuring, not a failure here.
    """

    engine: str
    elapsed_ms: float
    text_regions_count: int
    avg_doc_score: float | None
    min_doc_score: float | None


class StructuredField(TypedDict):
    """One named field read from the OCR lines.

    `value` is `""` when the field was not found -- never `None`. Both scores are `None` in that case,
    and `crf_conf` is additionally always `None` for document-level fields, which are found by regex
    or position and never pass through Viterbi.

    `features` is the trust model's input vector for this field, and it is present only for the nine
    scored contract fields. It exists because the two scores alone do not separate a correct value
    from a wrong one -- over 1686 hand-labelled cells `crf_conf` scores AUC 0.502, no better than a
    coin -- while the parser internals that do carry the signal are computed and then discarded. The
    names are pinned by `kk.MEMBER_CELL_FEATURES` and `kk.DOC_CELL_FEATURES`; scoring assembles them
    and never recomputes one, because a feature computed in two places is how a model ends up good in
    training and bad in production.
    """

    value: str
    ocr_conf: float | None
    crf_conf: float | None
    features: NotRequired[dict[str, float] | None]


StructuredMember = dict[str, StructuredField]


class StructuredDocument(TypedDict):
    """What a structurer (`app/ml/*` of structuring) returns: the flat K2Regex-v2 shape.

    The eleven document fields are top-level keys -- there is no `fields` wrapper -- plus
    `anggota_keluarga` and `reject_reason`. `reject_reason` is the first rejecting rule of the KK
    validity gate; when it is set the pipeline stops at structuring and the client gets a 400 with
    that message. It lives *in the result payload* on purpose: the orchestrator is stateless and only
    sees stages through their API, so a reason kept anywhere else could never reach the client.

    There is no `flag` / `flag_reason` counterpart. nilam has one as a soft signal for its trust
    model; K2Regex-v2 produces nothing equivalent, so it is deliberately not invented here.

    The eleven document keys are spelled out rather than left as a loose mapping: they are what the
    freeze pins down, and a typo in one of them is exactly the class of error the type exists to
    catch. `kk.DOC_FIELDS` carries the same names at runtime.
    """

    nomor_kk: StructuredField
    nama_kepala_keluarga: StructuredField
    alamat: StructuredField
    desa_kelurahan: StructuredField
    rt: StructuredField
    rw: StructuredField
    kecamatan: StructuredField
    kabupaten_kota: StructuredField
    provinsi: StructuredField
    kode_pos: StructuredField
    tanggal_dikeluarkan: StructuredField
    anggota_keluarga: list[StructuredMember]
    reject_reason: NotRequired[str | None]


StructuringResult = StructuredDocument
"""The stored result of the structuring stage. Unlike nilam there is no `document_type` key: §7.3
says the payload is exactly what the parser builds, and the parser does not emit one."""


class ScoringResult(TypedDict):
    """The stored result of the scoring stage: P(field is correct) after fusion and calibration.

    Scores only the nine contract fields (2 document + 7 per member), under their **internal** names
    -- the rename to the outgoing contract happens in the orchestrator. A field whose value is empty
    scores `None`. `anggota_keluarga` is positionally aligned with the structuring result's list.
    """

    document_type: str
    fields: dict[str, float | None]
    anggota_keluarga: list[dict[str, float | None]]
    model: NotRequired[str | None]
    payload: NotRequired[dict[str, Any]]
    thresholds: NotRequired[dict[str, float]]
    bin_edges: NotRequired[dict[str, list[float]]]


class FinalResult(TypedDict):
    """What the pipeline produced for one request: carried by the SCORING callback.

    Keeps the structuring and scoring payloads whole rather than flattening them, so a consumer that
    wants the eleven-plus-fifteen internal fields still has them; `kk.contract_fields` projects the
    nine that leave.
    """

    document_type: str
    structuring: StructuringResult
    scoring: ScoringResult
    guardrails: dict[str, Any] | None


class ContractField(TypedDict):
    """A field of the orchestrator's `extract-ocr` contract.

    `value` is a string, never `None`, and the object itself is never replaced by null: a field that
    was not found is `{"value": "", "confidence": 0.0, "bin": 1, "auto": False}`.

    `confidence` is the calibrated P(this value is exactly correct) as a float. It replaced a 1/0
    flag, which threw away the one thing a consumer needs to triage: a field at 0.52 and a field at
    0.99 are not the same claim. `bin` is the model's ten-bin placement, and `auto` says the field
    cleared its own field-specific threshold -- the gate calibrated so that every field above it was
    correct on held-out data. Read `auto` to decide whether a human has to look; read `confidence`
    to decide what to look at first.
    """

    value: str
    confidence: float
    bin: int
    auto: bool


ContractMember = dict[str, ContractField]


class ContractData(TypedDict):
    """`data` of the orchestrator's `extract-ocr` contract: two document fields and a member list.

    Note `no_kk`, not `nomor_kk` -- this is one of the two keys the projection renames.
    """

    no_kk: ContractField
    nama_kepala_keluarga: ContractField
    anggota_keluarga: list[ContractMember]

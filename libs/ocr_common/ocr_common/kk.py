"""What the Kartu Keluarga pipeline produces for the orchestrator: the field names, the final result
carried by the SCORING callback, and its projection to the orchestrator's `extract-ocr` contract.

There are **two field lists and they must not be confused**. The internal names (`DOC_FIELDS`,
`MEMBER_FIELDS`) are what structuring extracts and scoring scores, stored in the stage tables and
readable through `GET /v1/<stage>/jobs/{request_id}`. The contract names (`CONTRACT_DOC_FIELDS`,
`CONTRACT_MEMBER_FIELDS`) are the nine that leave in `data`. Exactly two of them are renamed on the
way out; the other seven keep their names, which is precisely what makes the mix-up easy.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from ocr_common.types import (
    ContractData,
    ContractField,
    ContractMember,
    FinalResult,
    ScoringResult,
    StructuringResult,
)

DOCUMENT_TYPE = "kk"

# `errors` of a 400 for a document that is not accepted: by the guardrails model, or by a rejecting
# rule of the KK validity gate at structuring.
REJECTED_CODE = "DOWNSTREAM_VALIDATION_ERROR"

# --- list A: internal names, used by structuring and scoring -------------------------------

DOC_FIELDS: tuple[str, ...] = (
    "nomor_kk",
    "nama_kepala_keluarga",
    "alamat",
    "desa_kelurahan",
    "rt",
    "rw",
    "kecamatan",
    "kabupaten_kota",
    "provinsi",
    "kode_pos",
    "tanggal_dikeluarkan",
)

MEMBER_FIELDS: tuple[str, ...] = (
    "nama_lengkap",
    "nik",
    "jenis_kelamin",
    "tempat_lahir",
    "tanggal_lahir",
    "agama",
    "pendidikan",
    "jenis_pekerjaan",
    "golongan_darah",
    "status_perkawinan",
    "tanggal_perkawinan",
    "status_hubungan_dalam_keluarga",
    "kewarganegaraan",
    "ayah",
    "ibu",
)

# --- list B: contract names, the only ones that leave in `data` ----------------------------

CONTRACT_DOC_FIELDS: tuple[str, ...] = ("no_kk", "nama_kepala_keluarga")

CONTRACT_MEMBER_FIELDS: tuple[str, ...] = (
    "nama_lengkap",
    "nik",
    "pendidikan",
    "jenis_pekerjaan",
    "status_hubungan_dalam_rumah_tangga",
    "ayah",
    "ibu",
)

# A -> B. Only two rows rename. The card itself prints "STATUS HUBUNGAN DALAM KELUARGA"; the
# outgoing contract says `status_hubungan_dalam_rumah_tangga` because the consumer asked for it. If
# that turns out to be wrong, fix it here alone -- the internal names do not follow.
DOC_PROJECTION: tuple[tuple[str, str], ...] = (
    ("no_kk", "nomor_kk"),
    ("nama_kepala_keluarga", "nama_kepala_keluarga"),
)

MEMBER_PROJECTION: tuple[tuple[str, str], ...] = (
    ("nama_lengkap", "nama_lengkap"),
    ("nik", "nik"),
    ("pendidikan", "pendidikan"),
    ("jenis_pekerjaan", "jenis_pekerjaan"),
    ("status_hubungan_dalam_rumah_tangga", "status_hubungan_dalam_keluarga"),
    ("ayah", "ayah"),
    ("ibu", "ibu"),
)

# The internal names scoring actually scores: the nine contract fields, no more. Each field needs
# its own calibrator, and training 26 of them for 9 numbers anyone reads would be waste.
SCORED_DOC_FIELDS: tuple[str, ...] = tuple(internal for _, internal in DOC_PROJECTION)
SCORED_MEMBER_FIELDS: tuple[str, ...] = tuple(internal for _, internal in MEMBER_PROJECTION)


def contract_fields(
    structuring: Mapping[str, Any],
    scoring: Mapping[str, Any],
    threshold: float,
) -> ContractData:
    """The `data` of the orchestrator's `extract-ocr` contract: nine fields, each `{value, confidence}`.

    Both the orchestrator and the scoring stage call this, and §8.5 requires them to agree exactly --
    otherwise the outcome row and the `extract-ocr` response could differ for one request. Keeping it
    a single function with a single threshold from `ocr_common.config` is what makes that true by
    construction rather than by discipline.

    The member lists of `structuring` and `scoring` are **positionally aligned**. A length mismatch
    is a defect in this pipeline, not a property of the document, so it raises rather than truncating
    or padding: an off-by-one would otherwise produce a 200 that looks right while carrying another
    person's confidence.
    """
    members = structuring.get("anggota_keluarga") or []
    scored_members = scoring.get("anggota_keluarga") or []
    if len(members) != len(scored_members):
        raise ValueError(
            f"anggota_keluarga length mismatch between structuring and scoring: {len(members)} vs {len(scored_members)}"
        )

    doc_scores = scoring.get("fields") or {}
    data: dict[str, Any] = {
        out: _field(structuring.get(internal), doc_scores.get(internal), threshold) for out, internal in DOC_PROJECTION
    }
    data["anggota_keluarga"] = [
        _member(member, scores, threshold) for member, scores in zip(members, scored_members, strict=True)
    ]
    return data  # type: ignore[return-value]


def _member(member: Mapping[str, Any], scores: Mapping[str, Any], threshold: float) -> ContractMember:
    return {out: _field(member.get(internal), scores.get(internal), threshold) for out, internal in MEMBER_PROJECTION}


def _field(field: Mapping[str, Any] | None, score: Any, threshold: float) -> ContractField:
    """One contract field. `value` is always a string and the object is never null; `confidence` is 1
    only when there is a value *and* a score that reaches the threshold."""
    value = "" if field is None else str(field.get("value") or "").strip()
    confident = bool(value) and isinstance(score, int | float) and not isinstance(score, bool) and score >= threshold
    return {"value": value, "confidence": 1 if confident else 0}


def final_result(
    document_type: str,
    guardrails: dict[str, Any] | None,
    structuring: StructuringResult,
    scoring: ScoringResult,
) -> FinalResult:
    """What the SCORING callback carries: the two stage payloads whole, plus the guardrails report
    that was submitted. Nothing is flattened, so a consumer that wants the eleven-plus-fifteen
    internal fields still has them and `contract_fields` stays the only place the nine are chosen."""
    return {
        "document_type": document_type,
        "structuring": structuring,
        "scoring": scoring,
        "guardrails": guardrails,
    }


def ocr_aggregates(texts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """`text_regions_count`, `avg_doc_score` and `min_doc_score` derived from `texts`.

    Both aggregates are `None` for an empty `texts` rather than 0: an image with no readable text is
    a rejection at structuring (the first rule of the validity gate), not a document that scored zero.
    """
    scores = [float(box["score"]) for box in texts if box.get("score") is not None]
    if not scores:
        return {"text_regions_count": len(texts), "avg_doc_score": None, "min_doc_score": None}
    return {
        "text_regions_count": len(texts),
        "avg_doc_score": round(sum(scores) / len(scores), 4),
        "min_doc_score": round(min(scores), 4),
    }

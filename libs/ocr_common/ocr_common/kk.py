"""What the Kartu Keluarga pipeline produces for the orchestrator: the field names, the final result
carried by the SCORING callback, and its projection to the orchestrator's `extract-ocr` contract.

There are **two field lists and they must not be confused**. The internal names (`DOC_FIELDS`,
`MEMBER_FIELDS`) are what structuring extracts and scoring scores, stored in the stage tables and
readable through `GET /v1/<stage>/jobs/{request_id}`. The contract names (`CONTRACT_DOC_FIELDS`,
`CONTRACT_MEMBER_FIELDS`) are the nine that leave in `data`. Exactly two of them are renamed on the
way out; the other seven keep their names, which is precisely what makes the mix-up easy.
"""

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any, cast

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

# --- list C: the trust model's feature vector, emitted by structuring -----------------------
#
# `ocr_conf` and `crf_conf` alone are not enough to tell a correct value from a wrong one: measured
# over 1686 hand-labelled cells, `crf_conf` scores AUC 0.502 -- indistinguishable from a coin. The
# signal that does work lives inside the parser and is discarded today: the raw emission score, the
# geometry of the cell, the lexicon runner-up gap, the cross-checks against NIK.
#
# Structuring emits these under `StructuredField.features` because that is where the parser runs;
# scoring only assembles them into a vector and scores it. Computing the same feature in two places
# is the most reliable way to build a model that is good in training and bad in production, so no
# feature below is ever recomputed downstream.
#
# ORDER DOES NOT MATTER here -- the model artifact carries its own column order and looks each name
# up. What matters is that every name below is present; a missing one becomes NaN, and the model was
# not trained to read it as "absent".

#: Features for a scored MEMBER cell (`SCORED_MEMBER_FIELDS`). Two more reach the model from the
#: field itself: `ocr_min` <- `ocr_conf` and `crf_conf` <- `crf_conf`, so they are not repeated here.
MEMBER_CELL_FEATURES: tuple[str, ...] = (
    # CRF: the structure of the decision, not just its outcome. `marg_min` is the un-rounded twin of
    # `crf_conf`; the rest is what forward-backward computes and the parser throws away.
    "marg_min",
    "marg_mean",
    "margin_min",
    "margin_mean",
    "logit_margin_min",
    "entropy_max",
    "entropy_mean",
    "emis_gap_min",
    "emis_assigned_min",
    "n_tokens",
    # geometry. `lebar_rel` > 1 marks a merged cell -- the case that produces ayah="KARRAM SUSYANI",
    # two names fused into one field, which every pattern check passes.
    "inside_frac",
    "dist_sigma_max",
    "lebar_rel",
    "h_rel",
    # OCR beyond the minimum
    "ocr_mean",
    "ocr_spread",
    "n_low_conf",
    # lexicon: graded similarity, rejected as an EMISSION weight but valid as a feature
    "has_vocab",
    "vocab_size",
    "vocab_sim",
    "vocab_gap",
    "vocab_nties",
    "vocab_cocok_char",
    "vocab_ok",
    "norm_changed",
    "is_nik_ok",
    # row level: placement errors arrive in groups, so one bad row taints all its cells
    "row_n_cells",
    "row_n_tokens",
    "row_cols_skipped",
    "row_marg_mean",
    "row_marg_min",
    # shape of the text itself
    "txt_len",
    "digit_ratio",
    "nonalnum_ratio",
    "n_space",
    "confusable_ratio",
    # cross-checks: the only features that carry independent evidence about the TEXT
    "nik_valid",
    "nik_vs_tgl",
    "nik_vs_gender",
    "was_repaired",
    "name_fixed",
    "nik_dup",
    "parent_match",
    # document level
    "doc_skew_abs",
    "doc_n_members",
    "row_pos_rel",
)

#: Features for a scored DOCUMENT cell (`SCORED_DOC_FIELDS`). A different family, and not by style:
#: `crf_conf` genuinely does not exist for these fields, so the placement evidence has to come from
#: somewhere else. For `nama_kepala_keluarga` that somewhere is `kepala_votes` -- four independent
#: witnesses (the identity label, the KEPALA KELUARGA row, the signature block, and a child's
#: `ayah`) of which `consensus()` picks one. How many agree is real redundancy, the same role
#: NIK-versus-birthdate plays for a member cell.
#:
#: Four more reach the model from elsewhere: `ocr_conf` from the field, and `avg_doc_score`,
#: `min_doc_score`, `text_regions_count` from the OCR aggregates, which scoring has and structuring
#: does not.
DOC_CELL_FEATURES: tuple[str, ...] = (
    "is_16digit",
    "sama_dengan_nik",
    "n_votes_terisi",
    "n_votes_setuju",
    "votes_sim_maks",
    "name_fixed",
    "n_anggota",
    "doc_skew_abs",
    "txt_len",
    "digit_ratio",
    "nonalnum_ratio",
    "n_space",
    "confusable_ratio",
)


#: Ten equal-width bins, used when the scoring result carries no edges of its own (the `mock`
#: backend). The calibrated model ships adaptive edges instead: its top bin is cut exactly where
#: held-out precision reaches 100%, which no fixed grid can land on.
DEFAULT_BIN_EDGES: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


def contract_fields(
    structuring: Mapping[str, Any],
    scoring: Mapping[str, Any],
    threshold: float,
) -> ContractData:
    """The `data` of the orchestrator's `extract-ocr` contract: nine fields, each
    `{value, confidence, bin, auto}`.

    Both the orchestrator and the scoring stage call this, and §8.5 requires them to agree exactly --
    otherwise the outcome row and the `extract-ocr` response could differ for one request. Agreement
    here is by construction, not by discipline: every number comes from the stored scoring result,
    including the per-field thresholds and the bin edges. `threshold` is only the fallback for a
    result that carries none, and a shared env constant is no longer what has to match.

    Why the thresholds travel in the result rather than in configuration: they are a property of the
    trained model, not of the deployment. They differ per field by design -- measured on held-out
    data, `ayah` reaches 100% precision at 0.935 while `pendidikan` needs 0.993 -- and a single
    global number collapses the usable coverage to zero. A model swap must move them together with
    the weights or the numbers silently stop meaning what they say.

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

    thresholds = scoring.get("thresholds") or {}
    edges = scoring.get("bin_edges") or {}
    doc_edges = tuple(edges.get("fields") or DEFAULT_BIN_EDGES)
    member_edges = tuple(edges.get("anggota_keluarga") or DEFAULT_BIN_EDGES)

    doc_scores = scoring.get("fields") or {}
    data: dict[str, Any] = {
        out: _field(structuring.get(internal), doc_scores.get(internal), thresholds, internal, threshold, doc_edges)
        for out, internal in DOC_PROJECTION
    }
    data["anggota_keluarga"] = [
        _member(member, scores, thresholds, threshold, member_edges)
        for member, scores in zip(members, scored_members, strict=True)
    ]
    return cast(ContractData, data)


def _member(
    member: Mapping[str, Any],
    scores: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    fallback: float,
    edges: tuple[float, ...],
) -> ContractMember:
    return {
        out: _field(member.get(internal), scores.get(internal), thresholds, internal, fallback, edges)
        for out, internal in MEMBER_PROJECTION
    }


def _field(
    field: Mapping[str, Any] | None,
    score: Any,
    thresholds: Mapping[str, Any],
    name: str,
    fallback: float,
    edges: tuple[float, ...],
) -> ContractField:
    """One contract field.

    `value` is always a string and the object is never null. `confidence` is the calibrated
    P(this value is exactly correct) -- a float, not the 1/0 flag this contract carried before --
    `bin` places it in one of the model's ten bins, and `auto` says whether it cleared the threshold
    for this field, which is the gate calibrated so that everything above it was correct on held-out
    data.

    A field with no value, or with no score, is `confidence: 0.0, bin: 1, auto: false`. It is not
    null: a consumer reading `confidence` must never have to test for null before comparing.
    """
    value = "" if field is None else str(field.get("value") or "").strip()
    if not value or not isinstance(score, int | float) or isinstance(score, bool):
        return {"value": value, "confidence": 0.0, "bin": 1, "auto": False}
    confidence = max(0.0, min(1.0, float(score)))
    limit = thresholds.get(name)
    limit = float(limit) if isinstance(limit, int | float) and not isinstance(limit, bool) else fallback
    return {
        "value": value,
        "confidence": round(confidence, 4),
        "bin": _bin(confidence, edges),
        "auto": confidence >= limit,
    }


def _bin(confidence: float, edges: tuple[float, ...]) -> int:
    """1-based bin of `confidence` over `edges`, which are bin boundaries low to high.

    The last bin is closed on the right so a confidence of exactly 1.0 lands in the top bin rather
    than past the end, and anything below the first edge is clamped into bin 1 rather than returning
    0 -- a bin number is shown to people, and there is no bin zero.
    """
    if len(edges) < 2:
        return 1
    index = bisect_right(edges, confidence) - 1
    return max(1, min(len(edges) - 1, index + 1))


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

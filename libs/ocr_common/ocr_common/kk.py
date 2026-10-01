"""What the Kartu Keluarga pipeline produces for the orchestrator: the field names, the final result
carried by the SCORING callback, and its projection to the orchestrator's `extract-ocr` contract.

There are **two field lists and they must not be confused**. The internal names (`DOC_FIELDS`,
`MEMBER_FIELDS`) are what structuring extracts and scoring scores, stored in the stage tables and
readable through `GET /v1/<stage>/jobs/{request_id}`. The contract names (`CONTRACT_DOC_FIELDS`,
`CONTRACT_MEMBER_FIELDS`) are the nine that leave in `data`. Exactly two of them are renamed on the
way out; the other seven keep their names, which is precisely what makes the mix-up easy.
"""

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ocr_common.types import (
    ContractData,
    ContractField,
    FinalResult,
    ScoredData,
    ScoredField,
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


CONTRACT_FIELDS: tuple[str, ...] = CONTRACT_DOC_FIELDS + CONTRACT_MEMBER_FIELDS

ALL_FIELD = "all_field"

COLUMN_THRESHOLD_DESCRIPTION = (
    "Per field, from the central orchestrator: the trust model's probability that the field's value is correct "
    "must reach it for the field's `confidence` to be `1` (else `0`). Keys are the contract names: `no_kk`, "
    "`nama_kepala_keluarga`, and the seven member fields (`nama_lengkap`, `nik`, `pendidikan`, `jenis_pekerjaan`, "
    "`status_hubungan_dalam_rumah_tangga`, `ayah`, `ibu`), a member field's threshold applying to every member. "
    "`all_field` sets one threshold for every field; a key for a single field overrides it. "
    "A field left out (or the whole map omitted) uses the trust model's own threshold for that field; a field the "
    "model has none for (`no_kk`) is then `0`"
)


def parse_column_thresholds(value: Any) -> dict[str, float] | None:
    """`column_confidence_threshold` checked: None, or an object whose keys are `CONTRACT_FIELDS` and whose
    values are numbers from 0 to 1. Raises ValueError with the reason otherwise. Ported from nilam."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('column_confidence_threshold must be a JSON object, e.g. {"all_field": 0.8} or {"no_kk": 0.9, "nik": 0.8}')
    unknown = sorted(set(value) - set(CONTRACT_FIELDS) - {ALL_FIELD})
    if unknown:
        raise ValueError(
            f"column_confidence_threshold has unknown field(s) {', '.join(unknown)}; "
            f"expected {ALL_FIELD} or {', '.join(CONTRACT_FIELDS)}"
        )
    thresholds = {}
    for name, threshold in value.items():
        if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not math.isfinite(threshold):
            raise ValueError(f"column_confidence_threshold.{name} must be a number, got {threshold!r}")
        if not 0 <= threshold <= 1:
            raise ValueError(f"column_confidence_threshold.{name} must be between 0 and 1, got {threshold}")
        thresholds[name] = float(threshold)
    if ALL_FIELD in thresholds:
        every = thresholds.pop(ALL_FIELD)
        thresholds = {**dict.fromkeys(CONTRACT_FIELDS, every), **thresholds}
    return thresholds or None


def column_thresholds_from_json(raw: str | None) -> dict[str, float] | None:
    """`column_confidence_threshold` as a form field carries it (a JSON object string); None when empty.
    Raises ValueError."""
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("column_confidence_threshold must be valid JSON: an object of field -> threshold") from exc
    return parse_column_thresholds(value)


def contract_fields(
    structuring: Mapping[str, Any],
    scoring: Mapping[str, Any],
    threshold: float,
    column_thresholds: Mapping[str, float] | None = None,
) -> ContractData:
    """The `data` of the orchestrator's `extract-ocr` contract: nine fields, each `{value, confidence}`
    with `confidence` 1 or 0, as in nilam. See `scored_fields` for how each field is decided."""
    return contract_data(scored_fields(structuring, scoring, threshold, column_thresholds))


def scored_fields(
    structuring: Mapping[str, Any],
    scoring: Mapping[str, Any],
    threshold: float,
    column_thresholds: Mapping[str, float] | None = None,
) -> ScoredData:
    """`contract_fields` plus the threshold each field was decided with: what the scoring stage stores
    (`decisions`), so the outcome row, the POST and the GET answer the same 0/1 for one request.

    A field's `confidence` is 1 when it has a value and the trust model's probability reaches the field's
    threshold, else 0. The threshold, first that applies:

    1. `column_thresholds[contract name]` -- the central orchestrator's, for this request;
    2. `scoring["thresholds"][internal name]` -- the trust model's own, the point above which every held-out
       sample of that field was correct. They travel in the result because they belong to the trained model;
    3. `threshold` (FIELD_CONFIDENCE_THRESHOLD), only for a result that carries no thresholds at all (the
       `mock` backend). A result that does carry thresholds and leaves a field out is saying the field had
       no such point (`nomor_kk` today), so that field is 0 unless the request gives it a threshold.

    The member lists of `structuring` and `scoring` are **positionally aligned**. A length mismatch is a
    defect in this pipeline, not a property of the document, so it raises rather than truncating or
    padding: an off-by-one would otherwise produce a 200 carrying another person's confidence.
    """
    members = structuring.get("anggota_keluarga") or []
    scored_members = scoring.get("anggota_keluarga") or []
    if len(members) != len(scored_members):
        raise ValueError(
            f"anggota_keluarga length mismatch between structuring and scoring: {len(members)} vs {len(scored_members)}"
        )

    model = scoring.get("thresholds") or {}
    columns = column_thresholds or {}
    # A model that ships its own thresholds decides every field; the env fallback is for one that ships none.
    fallback = None if model else threshold

    def limit(out: str, internal: str) -> float | None:
        for candidate in (columns.get(out), model.get(internal)):
            if isinstance(candidate, int | float) and not isinstance(candidate, bool):
                return float(candidate)
        return fallback

    doc_scores = scoring.get("fields") or {}
    data: dict[str, Any] = {
        out: _field(structuring.get(internal), doc_scores.get(internal), limit(out, internal))
        for out, internal in DOC_PROJECTION
    }
    data["anggota_keluarga"] = [
        {
            out: _field(member.get(internal), scores.get(internal), limit(out, internal))
            for out, internal in MEMBER_PROJECTION
        }
        for member, scores in zip(members, scored_members, strict=True)
    ]
    return cast(ScoredData, data)


def contract_data(scored: Mapping[str, Any]) -> ContractData:
    """The `extract-ocr` `data` of stored scored fields: value and 0/1 confidence, without the threshold."""

    def plain(field: Mapping[str, Any]) -> ContractField:
        return {"value": field["value"], "confidence": field["confidence"]}

    data: dict[str, Any] = {out: plain(scored[out]) for out, _ in DOC_PROJECTION}
    data["anggota_keluarga"] = [
        {out: plain(member[out]) for out, _ in MEMBER_PROJECTION} for member in scored.get("anggota_keluarga") or []
    ]
    return cast(ContractData, data)


def _field(field: Mapping[str, Any] | None, score: Any, threshold: float | None) -> ScoredField:
    """One decided field. `value` is always a string, `""` when not found, and the object is never null."""
    value = "" if field is None else str(field.get("value") or "").strip()
    numeric = isinstance(score, int | float) and not isinstance(score, bool)
    confident = bool(value) and numeric and threshold is not None and float(score) >= threshold
    return {"value": value, "confidence": 1 if confident else 0, "threshold": threshold}


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

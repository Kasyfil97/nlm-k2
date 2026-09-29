"""The `calibrated` trust model: P(field is exactly correct), from a trained and calibrated model.

Two families, not one model with a flag. Member cells have `crf_conf` and the whole CRF internals;
document fields have no `crf_conf` at all -- they are found by regex or position and never pass
through Viterbi -- so their evidence comes from somewhere else entirely (`kepala_votes`, the OCR
aggregates). One model over both would have to learn to ignore half its inputs depending on a flag,
and would be measured on a mixture of two very different base rates.

What the numbers mean, and what they do not:

* `confidence` is the calibrated probability that the value matches the ground truth **exactly**
  (upper-cased, whitespace collapsed). It covers placement and recognition together, because both
  have to be right for the strings to be equal.
* The per-field thresholds are the point above which every held-out sample of that field was
  correct. They are reported with the scores so the orchestrator's `auto` cannot drift from this
  stage's: nothing in configuration has to agree.
* 100% is a measurement on held-out data, not a guarantee. The training report carries the
  Clopper-Pearson lower bound, which is the number that can be promised.

A scored field with a value but **no feature vector** scores `None` rather than a guessed number.
That is the case when structuring has not been upgraded to emit `features`, and inventing a
confidence there would be the one failure this stage exists to prevent: a number that looks
authoritative and was computed from nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ocr_common.kk import (
    DOC_CELL_FEATURES,
    MEMBER_CELL_FEATURES,
    SCORED_DOC_FIELDS,
    SCORED_MEMBER_FIELDS,
)

logger = logging.getLogger(__name__)

#: Features the trust model reads from somewhere other than `field.features`, because structuring
#: does not have them. Kept here rather than in `ocr_common.kk` because this is a property of how
#: THIS backend assembles its vector, not of the contract between the two services.
MEMBER_FROM_FIELD = {"ocr_min": "ocr_conf", "crf_conf": "crf_conf"}
DOC_FROM_FIELD = {"ocr_conf": "ocr_conf"}
DOC_FROM_PAYLOAD = ("avg_doc_score", "min_doc_score", "text_regions_count")


class CalibratedTrustModel:
    """Scores the §8.3 shape from a joblib artifact produced by the training repo.

    The artifact carries its own column order, its own bin edges and its own thresholds. None of
    those are configuration: a model swap has to move them together with the weights, or the numbers
    quietly stop meaning what they say.
    """

    name = "calibrated"

    def __init__(self, path: str | Path):
        import joblib

        self._path = Path(path)
        artefak = joblib.load(self._path)
        self._version = artefak.get("versi") or "kk-trust-unknown"
        self._member = artefak.get("member")
        self._doc = artefak.get("doc")
        if not self._member:
            raise RuntimeError(f"trust model artifact {self._path} carries no `member` family")
        # The artifact's column list is the ONLY source of vector order, so a mismatch against the
        # estimator it was saved with has to fail here rather than produce plausible wrong numbers.
        for keluarga, family in (("member", self._member), ("doc", self._doc)):
            if not family:
                continue
            diharapkan = getattr(family["model"], "n_features_in_", None)
            if diharapkan is not None and diharapkan != len(family["kolom"]):
                raise RuntimeError(
                    f"trust model artifact {self._path}: family {keluarga} lists {len(family['kolom'])} "
                    f"columns but its estimator was fitted on {diharapkan}"
                )
        logger.info(
            "trust model loaded: %s from %s (member %d features, doc %s)",
            self._version,
            self._path,
            len(self._member["kolom"]),
            "absent" if not self._doc else f"{len(self._doc['kolom'])} features",
        )

    # --- public -------------------------------------------------------------------------

    def predict(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        structuring = payload.get("structuring") or {}
        members = structuring.get("anggota_keluarga") or []
        hilang: list[str] = []

        doc_scores = self._score_family(
            self._doc,
            [self._doc_row(structuring, name, payload, hilang) for name in SCORED_DOC_FIELDS],
            SCORED_DOC_FIELDS,
        )
        member_scores = [
            self._score_family(
                self._member,
                [self._member_row(member, name, hilang) for name in SCORED_MEMBER_FIELDS],
                SCORED_MEMBER_FIELDS,
            )
            for member in members
        ]
        if hilang:
            # Deliberately loud and deliberately not fatal: the pipeline still returns a result, but
            # the affected fields carry no confidence at all rather than a fabricated one.
            logger.warning(
                "trust model scored %d field(s) as null for want of a feature vector: %s",
                len(hilang),
                ", ".join(sorted(set(hilang))),
            )
        return {
            "fields": doc_scores,
            "anggota_keluarga": member_scores,
            "model": self._version,
            "thresholds": self.thresholds,
            "bin_edges": self.bin_edges,
        }

    @property
    def thresholds(self) -> dict[str, float]:
        """Per-field confidence above which every held-out sample was correct, both families in one
        flat map. A field missing here has no such point in the training data -- `nomor_kk` is one,
        with only 5 clean cells out of 97 -- and is therefore never `auto` (`ocr_common.kk.contract_fields`)."""
        out = dict(self._member.get("ambang") or {})
        out.update((self._doc or {}).get("ambang") or {})
        return {k: float(v) for k, v in out.items()}

    @property
    def bin_edges(self) -> dict[str, list[float]]:
        edges = {"anggota_keluarga": [float(x) for x in (self._member.get("tepi_bin") or [])]}
        if self._doc:
            edges["fields"] = [float(x) for x in (self._doc.get("tepi_bin") or [])]
        return {k: v for k, v in edges.items() if v}

    # --- assembling one row --------------------------------------------------------------

    def _member_row(self, member: Mapping[str, Any], name: str, hilang: list[str]) -> dict[str, float] | None:
        return _row(member.get(name), MEMBER_CELL_FEATURES, MEMBER_FROM_FIELD, {}, name, hilang)

    def _doc_row(
        self, structuring: Mapping[str, Any], name: str, payload: Mapping[str, Any], hilang: list[str]
    ) -> dict[str, float] | None:
        extra = {key: payload.get(key) for key in DOC_FROM_PAYLOAD}
        return _row(structuring.get(name), DOC_CELL_FEATURES, DOC_FROM_FIELD, extra, name, hilang)

    # --- scoring one family -------------------------------------------------------------

    def _score_family(
        self, family: Mapping[str, Any] | None, rows: Sequence[dict[str, float] | None], names: Sequence[str]
    ) -> dict[str, float | None]:
        """Score `rows` (aligned with `names`); a `None` row scores `None` and never reaches the model."""
        if family is None:
            return dict.fromkeys(names, None)
        dapat = [(index, row) for index, row in enumerate(rows) if row is not None]
        out: dict[str, float | None] = dict.fromkeys(names, None)
        if not dapat:
            return out
        import numpy as np

        kolom = family["kolom"]
        fields = family["fields"]
        X = np.array(
            [[_cell(row, column, names[index], fields) for column in kolom] for index, row in dapat],
            dtype=float,
        )
        raw = family["model"].predict_proba(X)[:, 1]
        kalibrasi = family["isotonic"].predict(raw)
        for (index, _row), p in zip(dapat, kalibrasi, strict=True):
            out[names[index]] = round(float(p), 4)
        return out


def _row(
    field: Mapping[str, Any] | None,
    expected: Sequence[str],
    from_field: Mapping[str, str],
    extra: Mapping[str, Any],
    name: str,
    hilang: list[str],
) -> dict[str, float] | None:
    """One feature row, or `None` when this field cannot honestly be scored.

    `None` for an empty value (§8.3: a field with no value scores null) and `None` for a value whose
    feature vector is absent, which means structuring is older than this model.
    """
    if not isinstance(field, Mapping) or not str(field.get("value") or "").strip():
        return None
    features = field.get("features")
    if not isinstance(features, Mapping) or not features:
        hilang.append(name)
        return None
    row = {key: _number(value) for key, value in features.items()}
    for feature, source in from_field.items():
        row[feature] = _number(field.get(source))
    for key, value in extra.items():
        row[key] = _number(value)
    kurang = [key for key in expected if key not in row]
    if kurang:
        # A subset arrived: score it, but say which names were absent. NaN is how the model was
        # trained to read a missing number, so this degrades rather than breaks.
        logger.warning("field %s is missing %d feature(s): %s", name, len(kurang), ", ".join(kurang))
    return row


def _cell(row: Mapping[str, float], column: str, field_name: str, fields: Sequence[str]) -> float:
    """One matrix cell. The one-hot columns are rebuilt from the field name, exactly as training did.

    Rebuilt from a fixed list rather than from the data: a field absent from one document must still
    produce its zero column, and anything that derives the columns from what happens to be present
    would silently shift the whole vector.
    """
    if column.startswith("f_") and column[2:] in fields:
        return 1.0 if column[2:] == field_name else 0.0
    return _number(row.get(column))


def _number(value: Any) -> float:
    """Optional number -> float, with NaN for absent.

    NaN, not 0 or -1: the model handles missing values natively, and encoding "no guardrail result"
    as a real 0.0 would have it read as "the guardrail said 0.0".
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return float("nan")
    return float(value)

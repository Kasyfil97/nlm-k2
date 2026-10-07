"""The `kk_field` trust model: P(field is correct), the s11 blend over `kk_model` structuring.

**Correct** means both: the OCR boxes that make up the field are the ground truth's, in the right
member, AND the final text equals the ground truth's (A-Z0-9 normalised). One model for all nine
fields -- document and member alike -- with the field as a one-hot, because every field is built the
same way by `kk_model` and carries the same 42 features (`kk.FIELD_FEATURES`).

It is only valid over the structuring it was trained on. That is enforced, not hoped for: a field is
scored only when its `features` carry every `FIELD_FEATURES` name, which a `kk_regex` vector does not,
so pairing it with the wrong structuring degrades to `None` (a human looks) rather than to a number
computed from features it never saw.

## The artifact

`SCORING_MODEL_PATH` (default `weights/kk_trust_model.joblib`), written by
`scoring/training/export_nlm_k2.py`:

```
{"versi", "structuring_versi", "dataset", "fields", "ambang",
 "spec": {"model": "blend", ...}, "est": {"parts": [{"spec", "est": {"main", "text"}, "names"}, ...]}, "names"}
```

Each part is a model over the design matrix `field_features.design` builds in training: the features
(`spec.feats`), a one-hot per field, field x feature interactions (`spec.inter`), and -- with
`text_stack` -- the logit of a char n-gram model over `"<field[:4]>|<value>"`. A blend averages its
parts' probabilities. The column names each part was fitted on travel in the artifact and are rebuilt
and compared at load, so a drift between this code and training fails at start, not in the numbers.

`ambang` is the threshold the training picked on cross-validation for 98% precision, one per field
under its internal name; it travels with the scores as `thresholds`, like `calibrated`'s did.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ocr_common.kk import FIELD_FEATURES, MODEL_FIELD_NAMES, SCORED_DOC_FIELDS, SCORED_MEMBER_FIELDS

logger = logging.getLogger(__name__)

EPS = 1e-6


class FieldTrustModel:
    name = "kk_field"

    def __init__(self, path: str | Path):
        import joblib

        self._path = Path(path)
        artefak = joblib.load(self._path)
        if not isinstance(artefak, dict) or "spec" not in artefak or "est" not in artefak:
            raise RuntimeError(f"trust model artifact {self._path} is not a kk_field export (no spec/est)")
        self._fields: list[str] = list(artefak["fields"])
        unknown = sorted(set(MODEL_FIELD_NAMES.values()) - set(self._fields))
        if unknown:
            raise RuntimeError(f"trust model artifact {self._path} does not know field(s) {', '.join(unknown)}")
        self._parts = _parts(artefak)
        for part in self._parts:
            expected = _names(part["spec"], self._fields)
            if expected != list(part["names"]):
                raise RuntimeError(
                    f"trust model artifact {self._path}: columns of part {part['spec']} differ from what this "
                    f"service builds ({len(part['names'])} vs {len(expected)})"
                )
            missing = sorted(set(part["spec"]["feats"]) - set(FIELD_FEATURES))
            if missing:
                raise RuntimeError(f"trust model artifact {self._path} reads unknown feature(s) {', '.join(missing)}")
        self._version: str = artefak.get("versi") or "kk-field-unknown"
        self._thresholds = {str(k): float(v) for k, v in (artefak.get("ambang") or {}).items()}
        logger.info(
            "trust model loaded: %s from %s (pair of %s, %d part(s))",
            self._version,
            self._path,
            artefak.get("structuring_versi"),
            len(self._parts),
        )

    @property
    def thresholds(self) -> dict[str, float]:
        return dict(self._thresholds)

    def predict(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        structuring = payload.get("structuring") or {}
        members = structuring.get("anggota_keluarga") or []
        hilang: list[str] = []
        # (slot, internal name, row): slot None = document field, else the member index.
        rows: list[tuple[int | None, str, dict[str, Any]]] = []
        for name in SCORED_DOC_FIELDS:
            row = _row(structuring.get(name), name, hilang)
            if row is not None:
                rows.append((None, name, row))
        for index, member in enumerate(members):
            for name in SCORED_MEMBER_FIELDS:
                row = _row(member.get(name), name, hilang)
                if row is not None:
                    rows.append((index, name, row))
        if hilang:
            logger.warning(
                "trust model scored %d field(s) as null for want of a kk_model feature vector: %s",
                len(hilang),
                ", ".join(sorted(set(hilang))),
            )

        scores = self._predict([row for _, _, row in rows]) if rows else []
        fields: dict[str, float | None] = dict.fromkeys(SCORED_DOC_FIELDS, None)
        member_scores: list[dict[str, float | None]] = [dict.fromkeys(SCORED_MEMBER_FIELDS, None) for _ in members]
        for (slot, name, _), p in zip(rows, scores, strict=True):
            target = fields if slot is None else member_scores[slot]
            target[name] = round(float(p), 4)
        return {
            "fields": fields,
            "anggota_keluarga": member_scores,
            "model": self._version,
            "thresholds": self.thresholds,
        }

    def _predict(self, rows: Sequence[Mapping[str, Any]]) -> list[float]:
        import numpy as np

        return list(np.mean([_predict_part(part, rows, self._fields) for part in self._parts], axis=0))


def _parts(artefak: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The leaf models of the artifact: a blend is the mean of its parts, anything else is one part."""
    est = artefak["est"]
    if isinstance(est, Mapping) and "parts" in est:
        out = []
        for part in est["parts"]:
            out += _parts(part)
        return out
    if "feats" not in artefak["spec"]:
        raise RuntimeError("trust model part has no explicit `feats`; re-export with export_nlm_k2.py")
    return [{"spec": artefak["spec"], "est": est, "names": artefak["names"]}]


def _names(spec: Mapping[str, Any], fields: Sequence[str]) -> list[str]:
    """The column names `field_features.design` + `FieldScorer.matrix` produce, in their order."""
    names = list(spec["feats"])
    for k in fields:
        names.append(f"is_{k}")
        names += [f"{k}*{c}" for c in spec.get("inter", ())]
    if not spec.get("onehot", True):
        names = [n for n in names if not n.startswith("is_")]
    if spec.get("text_stack"):
        names.append("lg_txt_p")
    return names


def _predict_part(part: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> Any:
    import numpy as np

    spec = part["spec"]
    field = np.array([row["field"] for row in rows], dtype=object)
    cols: dict[str, Any] = {c: np.array([row[c] for row in rows], dtype=float) for c in spec["feats"]}
    for k in fields:
        oh = (field == k).astype(float)
        cols[f"is_{k}"] = oh
        for c in spec.get("inter", ()):
            cols[f"{k}*{c}"] = oh * cols[c]
    if not spec.get("onehot", True):
        cols = {n: v for n, v in cols.items() if not n.startswith("is_")}
    if spec.get("text_stack"):
        text = np.array([f"{row['field'][:4]}|{row['value']}" for row in rows], dtype=object)
        p = np.clip(part["est"]["text"].predict_proba(text)[:, 1], EPS, 1 - EPS)
        cols["lg_txt_p"] = np.log(p / (1 - p))
    X = np.column_stack([cols[n] for n in part["names"]])
    return part["est"]["main"].predict_proba(X)[:, 1]


def _row(field: Mapping[str, Any] | None, name: str, hilang: list[str]) -> dict[str, Any] | None:
    """One field's row, or `None`: an empty value scores null (§8.3), and so does a value without the full
    `FIELD_FEATURES` vector -- a structuring other than `kk_model` produced it."""
    if not isinstance(field, Mapping):
        return None
    value = str(field.get("value") or "")
    if not value.strip():
        return None
    features = field.get("features")
    if not isinstance(features, Mapping) or any(
        not isinstance(features.get(k), int | float) or isinstance(features.get(k), bool) for k in FIELD_FEATURES
    ):
        hilang.append(name)
        return None
    row: dict[str, Any] = {k: float(features[k]) for k in FIELD_FEATURES}
    row["field"] = MODEL_FIELD_NAMES[name]
    row["value"] = value
    return row

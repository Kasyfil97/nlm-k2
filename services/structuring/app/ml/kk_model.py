"""The `kk_model` structurer: the trained key classifier m04, reading the boxes extraction produced.

One OCR box at a time, the model gives the probability of each of the nine KK fields (or none); the
boxes are then grouped into family members by row and column, and the three closed fields are snapped
to a vocabulary. It is the structuring the `kk_field` trust model at scoring was trained on, which is
the reason it exists here: that trust model's numbers mean something only over this structuring, the
same way the `calibrated` one's meant something only over `kk_regex`.

**The model code is vendored**, in `app/vendor/kk_model/`, from the OCR-KK training workspace
(`scoring/training/{features,structure,field_features}.py`). This module is only the adapter: it loads
the artifact, runs the vendored functions, and maps their output onto §7.3. Nothing about how a box
is classified, grouped or snapped is decided here.

## The artifact

`STRUCTURING_MODEL_PATH` (default `weights/kk_structuring_model.joblib`), written by
`scoring/training/export_nlm_k2.py` from the training artifact. It carries the three chained models
(`text` -> `stage1` -> `ctx1`) as plain sklearn/xgboost objects, the class order, and the closed
vocabulary with its thresholds, frozen from the training ground truth -- the training code rebuilds that
vocabulary from the ground truth at inference time, which a service does not have.

## Page size

The model normalises coordinates by the page's width and height. §7.1 carries no page dimensions, and
on purpose (see extraction's `remote.py`), so the page is taken to be the extent of the polys. Measured
on the 100 test documents against the real dimensions: 4 of 2428 fields change, AUROC of the trust
model 0.9374 -> 0.9360.

## What it fills in

| part of `StructuredField` | source |
|---|---|
| `value`     | the joined text of the field's boxes, cleaned (`no_kk`/`nik`: 16 digits) or snapped |
| `ocr_conf`  | the lowest recognition score of the field's boxes |
| `crf_conf`  | member fields: the lowest box-class probability (the placement evidence); doc fields: None |
| `features`  | the 42 `kk.FIELD_FEATURES` of the nine scored fields, for `kk_field` at scoring |

These nine are the whole payload (`kk.DOC_FIELDS` + `kk.MEMBER_FIELDS`); the model reads nothing else
from the card.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Any, cast

from ocr_common.kk import DOC_FIELDS, FIELD_FEATURES, MEMBER_FIELDS, MODEL_FIELD_NAMES
from ocr_common.types import OcrBox, StructuringResult

from app.ml.validity import NO_TEXT, empty, field, reject_reason
from app.vendor.kk_model import features as vf
from app.vendor.kk_model import field_features as vff
from app.vendor.kk_model.structure import Lexicon, group_boxes, predict_doc

logger = logging.getLogger(__name__)

#: model name -> internal name, for the nine fields the model reads.
INTERNAL = {model: internal for internal, model in MODEL_FIELD_NAMES.items()}


def shape(text: str) -> str:
    """Preprocessor of the box text model: digits folded to `9`. Identical to the training `pipeline.shape`,
    which the export strips from the artifact because the service has no module of that name."""
    return re.sub(r"\d", "9", text.upper())


class _Full:
    """An estimator whose `predict_proba` always has one column per class (training `pipeline.Full`)."""

    def __init__(self, est: Any, cls: list[int], n_classes: int):
        import numpy as np

        self._est, self._cls, self._n = est, np.asarray(cls, int), n_classes

    def predict_proba(self, X: Any) -> Any:
        import numpy as np

        P = self._est.predict_proba(X)
        out = np.zeros((len(X), self._n))
        out[:, self._cls] = P
        return out


class KKModelStructurer:
    """Model-based structuring of a Kartu Keluarga from the boxes of §7.1."""

    name = "kk_model"
    #: Trained on the OCR output as is: `StructuringService` passes blank boxes through (see there).
    reads_blank_boxes = True

    def __init__(self, path: str | Path):
        import joblib

        if tuple(vff.NUM_FEATS) != FIELD_FEATURES:
            raise RuntimeError("kk_model: vendored field_features.NUM_FEATS differs from ocr_common.kk.FIELD_FEATURES")
        self._path = Path(path)
        artefak = joblib.load(self._path)
        if not isinstance(artefak, dict) or "models" not in artefak or "lexicon" not in artefak:
            raise RuntimeError(f"structuring model artifact {self._path} is not a kk_model export (no models/lexicon)")
        if list(artefak.get("classes") or []) != list(vf.CLASSES):
            raise RuntimeError(f"structuring model artifact {self._path}: class order differs from the vendored code")
        n = len(vf.CLASSES)
        models: dict[str, _Full] = {}
        for name, spec in artefak["models"].items():
            est = _Softprob(spec["xgb_json"], spec["intercept"]) if "xgb_json" in spec else spec["est"]
            if spec.get("preprocessor_shape"):
                est.named_steps[spec["preprocessor_shape"]].preprocessor = shape
            models[name] = _Full(est, spec["cls"], n)
        if "stage1" not in models:
            raise RuntimeError(f"structuring model artifact {self._path} has no stage1 model")
        self._model = {"models": models}
        self._lexicon = Lexicon(artefak["lexicon"]["vocab"], artefak["lexicon"]["theta"])
        self._fill_tau = artefak.get("fill_tau", 0.05)
        self.version: str = artefak.get("versi") or "kk-structuring-unknown"
        logger.info("kk_model structurer ready: %s from %s (%s)", self.version, self._path, " -> ".join(models))

    def structure(self, texts: list[OcrBox]) -> StructuringResult:
        boxes = [box for box in texts if _usable(box.get("poly"))]
        if not any((box.get("text") or "").strip() for box in boxes):
            return empty(NO_TEXT)
        doc = {"texts": boxes, **_page(boxes)}
        P, aux = predict_doc(self._model, doc)
        st = group_boxes(doc, P, aux, fill_tau=self._fill_tau)
        rows = {(row["member"], row["field"]): row for row in vff.doc_rows(doc, P, st, self._lexicon, aux)}

        document: dict[str, object] = {name: field("") for name in DOC_FIELDS}
        for model_name in ("no_kk", "nama_kepala_keluarga"):
            document[INTERNAL[model_name]] = _field(rows.get(("", model_name)), member=False)
        members = []
        for index in range(len(st["members"])):
            member = {name: field("") for name in MEMBER_FIELDS}
            for (row_member, model_name), row in rows.items():
                if row_member == index:
                    member[INTERNAL[model_name]] = _field(row, member=True)
            members.append(member)
        document["anggota_keluarga"] = members
        document["reject_reason"] = reject_reason(cast(dict, document["nomor_kk"])["value"], members)
        return cast(StructuringResult, document)


def _field(row: dict[str, Any] | None, *, member: bool):
    if row is None:
        return field("")
    return field(
        str(row["value"]),
        round(float(row["ocr_conf"]), 4),
        round(float(row["structuring_conf"]), 4) if member else None,
        features={name: _finite(row[name]) for name in FIELD_FEATURES},
    )


def _finite(value: Any) -> float | None:
    """A JSON-safe number: NaN/inf would not survive the stage table, and `kk_field` scores a field with a
    `None` feature as null rather than guessing it."""
    number = float(value)
    return number if math.isfinite(number) else None


class _Softprob:
    """`XGBClassifier.predict_proba` of a multi:softprob model, its per-class intercept given as `base_margin`.

    The export keeps the intercept out of the trees' JSON: XGBoost >= 3.1 stores a multi-class
    `base_score` as a vector of margins, which the xgboost guardrails pins (3.0.5, one dev env for every
    service) does not read -- loaded as is, its probabilities are off by up to 0.7. The trees themselves
    evaluate the same in both versions, so the intercept is passed per row instead. Equality with the
    trained model is asserted in `tests/test_kk_model.py`.
    """

    def __init__(self, raw: bytes, intercept: list[float]):
        import numpy as np
        import xgboost

        self._booster = xgboost.Booster()
        self._booster.load_model(bytearray(raw))
        self._intercept = np.asarray(intercept, np.float32)

    def predict_proba(self, X: Any) -> Any:
        import numpy as np
        import xgboost

        # The intercept goes in as XGBoost's own `base_margin`, so it is XGBoost that adds the trees to
        # it and takes the softmax, in its order and its float32: the trust model reads these through a
        # logit, where 1e-7 near p=1 is not noise.
        X = np.asarray(X, float)
        dmatrix = xgboost.DMatrix(X, base_margin=np.tile(self._intercept, (len(X), 1)))
        return self._booster.predict(dmatrix).reshape(len(X), -1)


def _usable(poly: Any) -> bool:
    """Four points of two numbers: what the vendored geometry indexes into."""
    if not isinstance(poly, list) or len(poly) < 4:
        return False
    return all(
        isinstance(p, list | tuple) and len(p) >= 2 and all(isinstance(v, int | float) for v in p[:2]) for p in poly
    )


def _page(boxes: list[OcrBox]) -> dict[str, float]:
    """Page width/height as the extent of the polys (§7.1 has no page size; see the module docstring)."""
    xs = [float(p[0]) for box in boxes for p in box["poly"]]
    ys = [float(p[1]) for box in boxes for p in box["poly"]]
    return {"W": max(max(xs), 1.0), "H": max(max(ys), 1.0)}

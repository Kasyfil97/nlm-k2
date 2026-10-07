"""The `kk_regex` structurer: the K2Regex-v2 layout parser, reading the boxes extraction produced.

This is the backend the `mock` one stood in for. Everything it returns is derived from the OCR boxes
of the submitted image -- which is the whole point, and is exactly what `mock` could not do: `mock`
generates a household, so two different cards come back byte-identical and a document that is not a
Kartu Keluarga is answered 200 with a perfect one.

**The parser is vendored verbatim**, as `app/vendor/kk_layout_parser.py` -- the directory the repo
already excludes from ruff, ty and pyrefly for exactly this. Its extraction logic is not edited
here; this module is only an adapter, and the split is deliberate. The parser's own
header calls itself a vendored snapshot of a prototype pinned by content hash, so every edit made on
the way through would have to be re-applied by hand at the next refresh, and the one thing worse
than a stale parser is one that silently disagrees with the corpus it was measured against.

Provenance of the copy:

* from   `K2Regex-v2/src/services/kk_layout_parser.py`
* sha256 `1515c5dbd08c89c094a25586fdbaf90793967836346b194e7b7618718808732d` (upstream bytes, CRLF)
* the only transformation is CRLF -> LF, which this repository requires; the vendored file hashes
  `7cd62c4f5466b6fe7fc5963075f7e96b4bc1ea28c0d6cca586b4b0ac04677048`.

`kk_template.json` travels with it (upstream `2ed7fa34...`, vendored `0c108595...`). The parser finds
it next to itself, so it must stay in `app/vendor`.

## Configuration is checked at start-up, not logged

Three conditions have to hold for the parser to be the one whose accuracy was measured, and none of
them shows up in a single document -- a misconfigured parser reads a card perfectly well and is
several points worse across a corpus. So they are start-up failures, reported together:

* `kk_template.json` resolves, or column boundaries fall back to headers alone;
* `CRF_KOLOM` is on, or columns are assigned by x-range and a row can be placed in an order the
  card cannot print;
* **no** `kk_kolom_model.json` resolves. That one reads backwards: the hand-tuned emission weights
  are the 89.02% configuration, and a stray trained model costs about 2.4 points silently. The
  first version of this file logged its presence as "trained" and carried on.

`test_kk_regex_baseline.py` re-checks all three without a corpus, and with one re-runs 200 real
documents against K2Regex-v2's own hash baseline.

## What it fills in, and what it leaves empty

| part of `StructuredField` | source |
|---|---|
| `value`     | the parser's own output, normalised to the Dukcapil vocabularies |
| `ocr_conf`  | `_meta.conf` -- the recognition score of the tokens the cell was built from |
| `crf_conf`  | `_meta.crf_conf` -- the forward-backward marginal of the column assignment |
| `features`  | `app/vendor/kk_features.py`, for the nine SCORED fields only |

`features` is the trust model's input vector (`kk.MEMBER_CELL_FEATURES`, 46 per scored member cell;
`kk.DOC_CELL_FEATURES`, 13 per document field). Without it `calibrated` scores every field `None`,
which the contract renders as `confidence: 0.0, bin: 1, auto: false` -- so the whole document asks
for a human.

**It is not computed here.** `kk_features.py` is the extractor the trust model was *trained* with,
vendored from `conf_model/`, and that is the point: a feature computed one way in training and
another way in production is the most reliable way to build a model that is good on paper and bad in
use. The same file now runs on both sides, so they cannot disagree.

It reaches the parser's internals by wrapping `assign_columns_viterbi` -- the original still decides
every value and every placement, and the wrapper only recomputes the emission scores and
forward-backward marginals the parser throws away. Two tests hold that down: the 200-document value
baseline (nothing the parser returns may change) and `diff_maks()` against the parser's own
`kol_conf` (the recomputed marginals must be the parser's, not a second opinion).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, cast

from ocr_common.kk import (
    DOC_CELL_FEATURES,
    DOC_FIELDS,
    MEMBER_CELL_FEATURES,
    MEMBER_FIELDS,
    SCORED_DOC_FIELDS,
    SCORED_MEMBER_FIELDS,
)
from ocr_common.types import OcrBox, StructuredField, StructuringResult

from app.ml.validity import NO_TEXT, empty, field, reject_reason
from app.vendor import kk_features, kk_layout_parser

logger = logging.getLogger(__name__)

#: `kk_features` records the parser's per-row internals in a module-global list and clears it at the
#: start of each document, so two documents parsed at once would interleave into each other's
#: vectors -- and the result would be plausible numbers about the wrong cells, which is worse than
#: none. Serialising costs nothing real: the parser is pure Python, so the GIL already prevents two
#: of these from running at the same time.
_PARSE_LOCK = threading.Lock()


class KKRegexStructurer:
    """Layout-aware structuring of a Kartu Keluarga from the boxes of §7.1."""

    name = "kk_regex"

    def __init__(self) -> None:
        """Refuse to start unless the parser is in the configuration it was measured in.

        All three checks guard the same failure: a parser that runs, answers a plausible card, and
        is quietly several points less accurate than the one the corpus figures describe. None of
        them is visible in a single document, which is why they are start-up failures and not logs.
        """
        problems = [
            problem
            for check in (_template_present, _crf_enabled, _no_trained_column_model)
            if (problem := check()) is not None
        ]
        if problems:
            raise RuntimeError("kk_regex refuses to start: " + "; ".join(problems))
        logger.info("kk_regex structurer ready (template resolved, CRF column assignment on, hand-tuned emissions)")

    def structure(self, texts: list[OcrBox]) -> StructuringResult:
        if not texts:
            return empty(NO_TEXT)

        raw = _adapt(texts)
        with _PARSE_LOCK:
            # `fitur_dokumen` runs the parser itself (debug=True) and returns the per-cell feature
            # rows alongside its output, which is why the call is not `kk_layout_parser.structure`.
            parsed, member_rows = kk_features.fitur_dokumen(raw)
            doc_rows = kk_features.fitur_doc_fields(parsed, kk_features.agregat_ocr(raw)) if parsed else []
        if not parsed:
            # The parser answers a bare `{}` when nothing survived `load_boxes` -- boxes arrived but
            # none had usable text or geometry. That is rule one, not rule two: there is no text to
            # judge, so saying "not a Kartu Keluarga" would blame the document for a blank page.
            return empty(NO_TEXT)

        meta = parsed.get("_meta") or {}
        doc_conf: dict[str, Any] = meta.get("conf") or {}
        member_conf: list[dict[str, Any]] = doc_conf.get("anggota_keluarga") or []
        crf_conf: list[dict[str, Any]] = meta.get("crf_conf") or []

        doc_vectors = _doc_vectors(doc_rows)
        member_vectors = _member_vectors(member_rows)

        document: dict[str, object] = {
            name: field(
                _text(parsed.get(name)),
                _score(doc_conf.get(name)),
                features=doc_vectors.get(name),
            )
            for name in DOC_FIELDS
        }
        members = [
            {
                name: field(
                    _text(member.get(name)),
                    _score(_at(member_conf, index).get(name)),
                    _score(_at(crf_conf, index).get(name)),
                    features=member_vectors.get((index, name)),
                )
                for name in MEMBER_FIELDS
            }
            for index, member in enumerate(parsed.get("anggota_keluarga") or [])
        ]
        document["anggota_keluarga"] = members
        document["reject_reason"] = reject_reason(_text(parsed.get("nomor_kk")), members)
        _log_repairs(meta)
        _log_missing_vectors(cast(dict[str, StructuredField], document), members)
        return cast(StructuringResult, document)


def _member_vectors(rows: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, float]]:
    """`{(member index, field): vector}` for the seven scored member fields.

    `kk_features` emits one row per scored cell it could trace, tagged with `member` and `field`,
    and skips a cell the Viterbi never placed. A skipped cell simply has no vector, and scoring
    answers `None` for it -- which is the honest outcome, not a gap to paper over.
    """
    return {
        (int(row["member"]), str(row["field"])): _vector(row, MEMBER_CELL_FEATURES)
        for row in rows
        if row.get("field") in SCORED_MEMBER_FIELDS and isinstance(row.get("member"), int)
    }


def _doc_vectors(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    return {str(row["field"]): _vector(row, DOC_CELL_FEATURES) for row in rows if row.get("field") in SCORED_DOC_FIELDS}


def _vector(row: dict[str, Any], names: tuple[str, ...]) -> dict[str, float]:
    """The row cut down to exactly the names the contract lists, in one place.

    `kk_features` also emits the handful of values scoring takes from elsewhere -- `ocr_min` and
    `crf_conf` from the field itself, `avg_doc_score` and friends from the OCR aggregates, and
    `guardrail_probability`, which the trained artefact drops. Sending them too would not break
    anything (scoring overwrites them), but it would make two services look like the authority on
    the same number, and the next reader would have to work out which one wins.
    """
    return {name: _finite(row.get(name)) for name in names}


def _finite(value: object) -> float:
    """A JSON-safe float. NaN and infinity become 0.0 with the name kept.

    JSON has no NaN, and this dictionary is serialised into the stage result and the handoff body.
    Dropping the key instead would be worse: scoring reads a missing name as absent and says so in
    a warning, while the model was trained to read NaN as "not measured" -- and the only features
    that can arrive NaN here are ones training also saw NaN.
    """
    if not isinstance(value, int | float) or isinstance(value, bool):
        return 0.0
    number = float(value)
    return round(number, 6) if number == number and number not in (float("inf"), float("-inf")) else 0.0


def _log_missing_vectors(document: dict[str, StructuredField], members: list[dict[str, Any]]) -> None:
    """Say so when a field has a value but no vector, because scoring will score it `None`.

    Scoring warns too, but by then the cause is out of sight: the vector is missing because the
    parser never placed that cell, which is a fact about THIS stage and this document.
    """
    missing = [name for name in SCORED_DOC_FIELDS if document[name]["value"] and not document[name]["features"]]
    missing += [
        f"{name}[{index}]"
        for index, member in enumerate(members)
        for name in SCORED_MEMBER_FIELDS
        if member[name]["value"] and not member[name]["features"]
    ]
    if missing:
        logger.warning(
            "kk_regex: %d field(s) with a value carry no feature vector, so scoring will answer null: %s",
            len(missing),
            ", ".join(missing),
        )


def _template_present() -> str | None:
    """`kk_template.json` next to the parser. Its own loader answers `{}` and carries on."""
    if kk_layout_parser.load_template():
        return None
    return (
        "kk_template.json does not resolve next to kk_layout_parser.py, so column boundaries would "
        "fall back to headers alone"
    )


def _crf_enabled() -> str | None:
    """`CRF_KOLOM` off replaces the Viterbi with plain x-range membership.

    It is a constant in the vendored file rather than a setting, so this can only trip after a
    re-vendor -- which is exactly when it should.
    """
    if kk_layout_parser.CRF_KOLOM:
        return None
    return "CRF_KOLOM is off in the vendored parser, so columns would be assigned by x-range alone"


def _no_trained_column_model() -> str | None:
    """A resolvable `kk_kolom_model.json` is a FAULT, and this is the least obvious of the three.

    The hand-tuned emission weights are the configuration the parser's 89.02% field accuracy was
    measured in. A stray `kk_kolom_model.json` anywhere on its search path silently swaps in trained
    pointwise weights and drops it to about 86.65% -- with no other signal at all. K2Regex-v2 fails
    startup on it for that reason, and so does this.

    Worth stating plainly because the intuition runs the other way: here the trained artefact is the
    worse one, and an earlier version of this file logged its presence as "trained" and carried on.
    """
    if not kk_layout_parser.load_model_kolom():
        return None
    return (
        "kk_kolom_model.json resolves on the parser search path; remove it -- the hand-tuned weights "
        "are the measured configuration and the model file costs about 2.4 points of field accuracy"
    )


def _log_repairs(meta: dict[str, Any]) -> None:
    """Announce every value the parser changed away from what it read.

    `repair_from_nik` rewrites `tanggal_lahir` and `jenis_kelamin` when the NIK disagrees with them,
    and `unify_names` rewrites a name to match another spelling of it in the same document. Both are
    usually right and both are invisible in the result: §7.3 has no slot for an audit trail, so the
    field simply reports a value the card does not print. Measured on a real card, `12-03-1954`
    became `01-07-1960` because the NIK said so -- a defensible call, but not one that should
    happen silently.

    The log line names the field, never the value: these are `tanggal_lahir` and names, and this
    stage logs no PII (R30).
    """
    for entry in meta.get("repairs") or []:
        logger.warning("kk_regex: row %s field %s rewritten from the NIK", entry.get("row"), entry.get("field"))
    for entry in meta.get("name_fixes") or []:
        logger.warning(
            "kk_regex: row %s field %s unified with another spelling in the same document",
            entry.get("row"),
            entry.get("field"),
        )


def _adapt(texts: list[OcrBox]) -> list:
    """§7.1 boxes -> the parser's `[[poly, [text, score]], ...]`.

    Same mapping K2Regex-v2 does in `kk_response.adapt_texts`. Boxes whose geometry the parser
    cannot use are dropped by its own `load_boxes`, so nothing is filtered here.
    """
    return [[box["poly"], [box["text"], float(box["score"])]] for box in texts]


def _at(rows: list[dict[str, Any]], index: int) -> dict[str, Any]:
    """Row `index` of a per-member side table, or `{}` when it is shorter than the member list.

    `_meta.conf` and `_meta.crf_conf` are built in separate passes from `anggota_keluarga`, and a
    short one has to mean "no score" -- lining up by position after a gap would attribute one
    person's confidence to another.
    """
    return rows[index] if 0 <= index < len(rows) and isinstance(rows[index], dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _score(value: object) -> float | None:
    """A score the contract can carry: a float in [0, 1], or None.

    The parser reports `None` for cells it never placed, and both scores are rounded here because
    they are read by people comparing runs, not only by the trust model.
    """
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return round(min(max(float(value), 0.0), 1.0), 4)

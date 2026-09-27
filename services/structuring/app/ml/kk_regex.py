"""The `kk_regex` structurer: the K2Regex-v2 layout parser, reading the boxes ekstraksi produced.

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
it next to itself, so it must stay in `app/vendor`; `test_kk_regex.py` fails if it does not, because
without it the column boundaries fall back to headers alone.

## What it fills in, and what it leaves empty

| part of `StructuredField` | source |
|---|---|
| `value`     | the parser's own output, normalised to the Dukcapil vocabularies |
| `ocr_conf`  | `_meta.conf` -- the recognition score of the tokens the cell was built from |
| `crf_conf`  | `_meta.crf_conf` -- the forward-backward marginal of the column assignment |
| `features`  | **always `None`** |

`features` is the trust model's vector (`kk.MEMBER_CELL_FEATURES`, `kk.DOC_CELL_FEATURES`), and the
parser does not emit it: the values exist inside `assign_columns_viterbi` and are discarded there.
Emitting them means instrumenting the parser, which is the edit this module exists to avoid making
casually. The consequence is visible and deliberate -- `calibrated` scores a field with no vector as
`None`, which the contract renders as `confidence: 0.0, auto: false`, so every field asks for a human
instead of carrying a number computed from nothing.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from ocr_common.kk import DOC_FIELDS, MEMBER_FIELDS
from ocr_common.types import OcrBox, StructuringResult

from app.ml.validity import NO_TEXT, empty, field, reject_reason
from app.vendor import kk_layout_parser

logger = logging.getLogger(__name__)

#: `rt` and `rw` are one printed cell ("016/004") that the parser splits. They share the recognition
#: score of the box they came from, so both read `_meta.conf` under the parser's own key.
DOC_CONF_KEY = {"rt": "rt_rw", "rw": "rt_rw"}


class KKRegexStructurer:
    """Layout-aware structuring of a Kartu Keluarga from the boxes of §7.1."""

    name = "kk_regex"

    def __init__(self) -> None:
        # Loaded eagerly so a missing template is a start-up failure rather than a quiet accuracy
        # loss on every document: the parser's own loader answers `{}` and carries on.
        if not kk_layout_parser.load_template():
            raise RuntimeError(
                "kk_template.json not found next to kk_layout_parser.py; the parser would fall back "
                "to header-only column boundaries and lose accuracy silently"
            )
        logger.info(
            "kk_regex structurer ready (template loaded, column model %s)",
            "trained" if kk_layout_parser.load_model_kolom() else "absent -- geometric emissions",
        )

    def structure(self, texts: list[OcrBox]) -> StructuringResult:
        if not texts:
            return empty(NO_TEXT)

        parsed = kk_layout_parser.structure(_adapt(texts), debug=True)
        if not parsed:
            # The parser answers a bare `{}` when nothing survived `load_boxes` -- boxes arrived but
            # none had usable text or geometry. That is rule one, not rule two: there is no text to
            # judge, so saying "not a Kartu Keluarga" would blame the document for a blank page.
            return empty(NO_TEXT)

        meta = parsed.get("_meta") or {}
        doc_conf: dict[str, Any] = meta.get("conf") or {}
        member_conf: list[dict[str, Any]] = doc_conf.get("anggota_keluarga") or []
        crf_conf: list[dict[str, Any]] = meta.get("crf_conf") or []

        document: dict[str, object] = {
            name: field(_text(parsed.get(name)), _score(doc_conf.get(DOC_CONF_KEY.get(name, name))))
            for name in DOC_FIELDS
        }
        members = [
            {
                name: field(
                    _text(member.get(name)),
                    _score(_at(member_conf, index).get(name)),
                    _score(_at(crf_conf, index).get(name)),
                )
                for name in MEMBER_FIELDS
            }
            for index, member in enumerate(parsed.get("anggota_keluarga") or [])
        ]
        document["anggota_keluarga"] = members
        document["reject_reason"] = reject_reason(_text(parsed.get("nomor_kk")), members)
        _log_repairs(meta)
        return cast(StructuringResult, document)


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

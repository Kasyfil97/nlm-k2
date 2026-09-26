"""The `mock` trust model: plausible per-field probabilities with the real §8.3 shape.

It stands in for the calibrated model until that lands. Two properties are load-bearing and are not
placeholders:

* **Positional alignment.** `anggota_keluarga` comes back with exactly as many rows as structuring
  sent, in the same order. A length mismatch is a defect in this pipeline, not a property of the
  document, and `kk.contract_fields` refuses it rather than truncating -- an off-by-one would
  otherwise produce a 200 that looks right while carrying another person's confidence.
* **Distinct numbers.** Every member scores differently and every field scores differently. Equal
  numbers would make an index shift invisible, which is the one failure this stage exists to expose
  before the real model arrives.

The scores are derived from the structuring result, so a field that was not read scores `None` --
the same rule the real model must follow.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ocr_common.kk import SCORED_DOC_FIELDS, SCORED_MEMBER_FIELDS

NAME = "kk-trust-mock-v1"


class MockTrustModel:
    """Derives a probability per contract field from the two structuring scores."""

    name = "mock"

    def predict(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        structuring = payload.get("structuring") or {}
        members = structuring.get("anggota_keluarga") or []
        return {
            "fields": {name: _score(structuring.get(name), index) for index, name in enumerate(SCORED_DOC_FIELDS)},
            "anggota_keluarga": [
                {
                    name: _score(member.get(name), position + index + 1)
                    for index, name in enumerate(SCORED_MEMBER_FIELDS)
                }
                for position, member in enumerate(members)
            ],
            "model": NAME,
        }


def _score(field: Any, offset: int) -> float | None:
    """P(this value is correct), or `None` when there is no value.

    Fuses the two structuring scores the way the real model eventually will -- a low `ocr_conf`
    means the glyphs were doubtful, a low `crf_conf` means the text was read but its column is
    unclear -- then nudges by `offset` so no two fields share a number.
    """
    if not isinstance(field, Mapping) or not str(field.get("value") or "").strip():
        return None
    ocr_conf = field.get("ocr_conf")
    crf_conf = field.get("crf_conf")
    known = [float(value) for value in (ocr_conf, crf_conf) if isinstance(value, int | float)]
    base = sum(known) / len(known) if known else 0.5
    return round(max(0.0, min(1.0, base - offset * 0.0137)), 4)

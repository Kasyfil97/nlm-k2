"""Clean-up of a structurer's answer, after the structurer and outside the vendored code.

The vendored parsers are copied verbatim (see `kk_model.py` / `kk_regex.py`), so a fix to what they
return goes here, applied by `StructuringService` to every backend, instead of into `app/vendor`.

## The household head's label

The recogniser often reads the label and the value of the household head as one box:
`"Nama Kepala Keluarga : ANTON"`. `kk_model` classifies that box correctly -- it is the field -- and
its `clean_value` only drops a *leading* colon, so the label went out as part of the name (127 of the
128 documents of `raw_ocr_v6` that have such a box). `kk_regex` splits on the colon, which misses a
box without one (`"Nama Kepala Keluarga ANTON"`).

The label is matched fuzzily on its letters, because it is read as badly as anything else on the
card: `Narna Kepala Keluarga`, `Nama Kepala Keluanga`, `Nama Kapala <aluarga`, junk in front of it.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import cast

from ocr_common.types import StructuredField, StructuringResult

from app.ml.validity import field

KEPALA_LABEL = "NAMAKEPALAKELUARGA"
#: Similarity of the box's leading letters to the label. No name comes near it: the label is 18
#: letters, and the misreads in the corpus score 0.89 and above.
LABEL_MIN_RATIO = 0.8
#: Letters of slack around the label: junk read in front of it, letters dropped from it.
LABEL_SLACK = 4
#: What separates the label from the name: `:`, its misreads, and spaces.
SEPARATORS = " \t:;.,=|-_'\"`>"


def _letters(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def strip_label(text: str, label: str = KEPALA_LABEL) -> str:
    """`text` without a leading `label`, matched on letters and fuzzily; `text` itself if it has none.

    The cut is the prefix most similar to the label, preferring one that ends a word, so a misread
    label (`"Nama Kepala Keluanga : X"`) is cut at its end rather than a letter into the name. A box
    that is the label alone comes back empty.
    """
    best: tuple[float, int] | None = None
    for end in range(1, len(text) + 1):
        if not text[end - 1].isalnum():
            continue
        letters = _letters(text[:end])
        if len(letters) < len(label) - LABEL_SLACK:
            continue
        if len(letters) > len(label) + LABEL_SLACK:
            break
        ratio = SequenceMatcher(None, letters, label).ratio()
        if ratio < LABEL_MIN_RATIO:
            continue
        word_end = end == len(text) or not text[end].isalnum()
        score = ratio + (0.05 if word_end else 0.0)
        if best is None or score > best[0]:
            best = (score, end)
    if best is None:
        return text
    return text[best[1] :].lstrip(SEPARATORS).strip()


def postprocess(result: StructuringResult) -> StructuringResult:
    """The structurer's answer with the household head's label removed from its value."""
    head = cast(StructuredField, result.get("nama_kepala_keluarga"))
    if not head or not head["value"]:
        return result
    value = strip_label(head["value"])
    if value == head["value"]:
        return result
    cleaned = dict(result)
    cleaned["nama_kepala_keluarga"] = {**head, "value": value} if value else field("")
    return cast(StructuringResult, cleaned)

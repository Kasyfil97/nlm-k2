"""The KK validity gate of §7.4, shared by every structurer.

The rules used to live in `mock.py`, which was right while `mock` was the only backend. It is wrong
now: the gate is the contract, not a property of one implementation. Two copies of three rules whose
ORDER is the whole specification would drift, and the drift would be invisible -- rules two and three
return the SAME reason string, so a backend that swapped them would answer 400 either way and only a
reader of the stored result could tell.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import cast

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from ocr_common.kk import DOC_FIELDS
from ocr_common.types import OcrBox, StructuredField, StructuringResult

#: §7.4, in priority order. The first condition that holds is the one reported.
NO_TEXT = "Gambar tidak memuat teks yang terbaca, mohon unggah foto Kartu Keluarga"
NOT_A_KK = "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil extraction tidak lengkap"
INCOMPLETE_KK = "Kolom Kartu Keluarga tidak terbaca lengkap (terpotong atau tertutup), mohon unggah foto KK utuh"

#: The column headers of the contract fields that sit on the right half of the card. A card whose right side
#: is cut off or covered still has its No. KK and NIK + nama, so rules two and three pass it, and the cut
#: column comes out truncated ("ARIN" for ARING) with a confident score. A missing header is what gives it away.
LONG_HEADERS = {"pekerjaan": "PEKERJAAN", "pendidikan": "PENDIDIKAN", "hubungan": "HUBUNGAN"}
SHORT_HEADERS = {"ayah": "AYAH", "ibu": "IBU"}


def empty(reason: str) -> StructuringResult:
    """The complete object with nothing in it: every key present, every value `""`, no members.

    §7.3: a parser that found nothing still returns the COMPLETE object, because the shape is frozen
    and a rejection is not an excuse to return a different one.
    """
    document: dict[str, object] = {name: field("") for name in DOC_FIELDS}
    document["anggota_keluarga"] = []
    document["reject_reason"] = reason
    return cast(StructuringResult, document)


def reject_reason(no_kk: str, members: list[dict], texts: Iterable[OcrBox] | None = None) -> str | None:
    """Rules two to four, first match wins.

    Rule one (no readable text at all) is handled by the caller before any field is built, because
    it has nothing to build them from. The order is not cosmetic: an empty document satisfies all
    four conditions at once, so only the ordering makes that case deterministic.

    Rule four (every contract column header present) needs the OCR boxes; `texts=None` skips it, for
    the `mock` structurer whose boxes are levers rather than a card.
    """
    if not no_kk or no_kk == "Not found":
        return NOT_A_KK
    if not any(m["nik"]["value"] and m["nama_lengkap"]["value"] for m in members):
        return NOT_A_KK
    if texts is not None and missing_headers(texts):
        return INCOMPLETE_KK
    return None


def missing_headers(texts: Iterable[OcrBox]) -> list[str]:
    """The contract column headers not found among the OCR boxes, in `LONG_HEADERS`, `SHORT_HEADERS` order.

    OCR misreads headers often (PANDIDIKAN, JENISPOKERJAAN, LBU, AYAN), so the match tolerates it, but
    differently by length. A long header matches inside a longer box ("JENIS PEKERJAAN") at
    `partial_ratio >= 80`. A short one must be a box of its own within one edit: a fuzzy match on three
    letters would find IBU in BURUH or a misread ISLAM, and AYAH in every name ending in -SYAH, which on
    a card cut just left of the Ibu column is exactly the header that is gone.
    """
    boxes = [_letters(box.get("text") or "") for box in texts]
    missing = [
        name
        for name, header in LONG_HEADERS.items()
        if not any(len(text) >= len(header) - 1 and fuzz.partial_ratio(header, text) >= 80 for text in boxes)
    ]
    missing += [
        name
        for name, header in SHORT_HEADERS.items()
        if not any(len(text) <= len(header) + 1 and Levenshtein.distance(header, text) <= 1 for text in boxes)
    ]
    return missing


def _letters(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def field(
    value: str,
    ocr_conf: float | None = None,
    crf_conf: float | None = None,
    features: dict[str, float | None] | None = None,
) -> StructuredField:
    """`value` is never None, and both scores are None when there is no value (§7.3)."""
    if not value:
        return {"value": "", "ocr_conf": None, "crf_conf": None, "features": None}
    return {"value": value, "ocr_conf": ocr_conf, "crf_conf": crf_conf, "features": features}

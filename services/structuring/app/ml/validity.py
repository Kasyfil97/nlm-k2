"""The KK validity gate of §7.4, shared by every structurer.

The rules used to live in `mock.py`, which was right while `mock` was the only backend. It is wrong
now: the gate is the contract, not a property of one implementation. Two copies of three rules whose
ORDER is the whole specification would drift, and the drift would be invisible -- rules two and three
return the SAME reason string, so a backend that swapped them would answer 400 either way and only a
reader of the stored result could tell.
"""

from __future__ import annotations

from typing import cast

from ocr_common.kk import DOC_FIELDS
from ocr_common.types import StructuredField, StructuringResult

#: §7.4, in priority order. The first condition that holds is the one reported.
NO_TEXT = "Gambar tidak memuat teks yang terbaca, mohon unggah foto Kartu Keluarga"
NOT_A_KK = "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil extraction tidak lengkap"


def empty(reason: str) -> StructuringResult:
    """The complete object with nothing in it: every key present, every value `""`, no members.

    §7.3: a parser that found nothing still returns the COMPLETE object, because the shape is frozen
    and a rejection is not an excuse to return a different one.
    """
    document: dict[str, object] = {name: field("") for name in DOC_FIELDS}
    document["anggota_keluarga"] = []
    document["reject_reason"] = reason
    return cast(StructuringResult, document)


def reject_reason(no_kk: str, members: list[dict]) -> str | None:
    """Rules two and three, first match wins.

    Rule one (no readable text at all) is handled by the caller before any field is built, because
    it has nothing to build them from. The order is not cosmetic: an empty document satisfies all
    three conditions at once, so only the ordering makes that case deterministic.
    """
    if not no_kk or no_kk == "Not found":
        return NOT_A_KK
    if not any(m["nik"]["value"] and m["nama_lengkap"]["value"] for m in members):
        return NOT_A_KK
    return None


def field(
    value: str,
    ocr_conf: float | None = None,
    crf_conf: float | None = None,
    features: dict[str, float] | None = None,
) -> StructuredField:
    """`value` is never None, and both scores are None when there is no value (§7.3)."""
    if not value:
        return {"value": "", "ocr_conf": None, "crf_conf": None, "features": None}
    return {"value": value, "ocr_conf": ocr_conf, "crf_conf": crf_conf, "features": features}

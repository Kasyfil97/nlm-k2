"""The `mock` structurer: the real §7.4 validity gate over synthetic fields.

This stands in for the K2Regex-v2 port until that lands. Two things make it worth more than a
constant:

* It applies the **real** three rules of §7.4, in the real order, so the rejection path the
  orchestrator depends on is the same path the eventual parser will use. Nothing new is invented
  and nothing lands in `ocr_common`: an empty `texts` is already a rejecting condition in the
  contract, so the trigger for the most important case costs nothing.
* The household size is **controllable**, because a variable-length member list is the single
  biggest structural difference between a Kartu Keluarga and a single-value document. A fixed stub
  would freeze the one dimension most likely to break, and an off-by-one between this list and
  scoring's would produce a 200 that looks right while carrying another person's confidence.

Test levers ride on a `MOCK:` line in the OCR text. They live here, in the backend that only ever
runs with `STRUCTURING_BACKEND=mock`, rather than in shared code.
"""

from __future__ import annotations

import re

from ocr_common.kk import DOC_FIELDS, MEMBER_FIELDS
from ocr_common.synthetic_kk import household, nomor_kk
from ocr_common.types import OcrBox, StructuredField, StructuringResult

#: §7.4, in priority order. The first condition that holds is the one reported.
NO_TEXT = "Gambar tidak memuat teks yang terbaca, mohon unggah foto Kartu Keluarga"
NOT_A_KK = "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil ekstraksi tidak lengkap"

_LEVER = re.compile(r"MOCK:(\w+)=([^\s]*)")

_DOC_VALUES = {
    "alamat": "JL. MERDEKA NO. 12",
    "desa_kelurahan": "CIHAPIT",
    "rt": "003",
    "rw": "007",
    "kecamatan": "BANDUNG WETAN",
    "kabupaten_kota": "KOTA BANDUNG",
    "provinsi": "JAWA BARAT",
    "kode_pos": "40114",
    "tanggal_dikeluarkan": "12-03-2019",
}


class MockStructurer:
    """Synthetic KK fields, with the real validity gate on top."""

    name = "mock"

    def structure(self, texts: list[OcrBox]) -> StructuringResult:
        if not texts:
            # §7.3: a parser that found no boxes still returns the COMPLETE object -- every value
            # empty, both scores null, no members -- with the reason filled in. Returning invented
            # fields alongside a rejection would contradict the shape the gate freezes.
            return _empty(NO_TEXT)

        levers = _levers(texts)
        members = int(levers.get("members", "2"))
        head = "" if "blank_kk" in levers else levers.get("no_kk") or nomor_kk()

        result = self._document(head, members, levers)
        result["reject_reason"] = _reject_reason(head, result["anggota_keluarga"])
        return result

    def _document(self, head: str, members: int, levers: dict[str, str]) -> StructuringResult:
        people = household(members, seed=int(levers.get("seed", "0")))
        document: dict[str, object] = {
            "nomor_kk": _field(head, 0.9991),
            "nama_kepala_keluarga": _field(people[0].nama_lengkap if people else "", 0.9873),
        }
        for name in DOC_FIELDS:
            document.setdefault(name, _field(_DOC_VALUES.get(name, ""), 0.97))
        # Scores deliberately differ per member: identical numbers would hide an index shift
        # between this list and scoring's, which is exactly what the variable length exists to expose.
        document["anggota_keluarga"] = [
            {
                name: _field(getattr(person, name), round(0.99 - index * 0.031, 4), round(0.97 - index * 0.043, 4))
                for name in MEMBER_FIELDS
            }
            for index, person in enumerate(people)
        ]
        return document  # type: ignore[return-value]


def _levers(texts: list[OcrBox]) -> dict[str, str]:
    return {key: value for box in texts for key, value in _LEVER.findall(box.get("text") or "")}


def _empty(reason: str) -> StructuringResult:
    """The complete object with nothing in it: every key present, every value `""`, no members."""
    document: dict[str, object] = {name: _field("") for name in DOC_FIELDS}
    document["anggota_keluarga"] = []
    document["reject_reason"] = reason
    return document  # type: ignore[return-value]


def _reject_reason(head: str, members: list[dict]) -> str | None:
    """§7.4 rules two and three, first match wins. Rule one (no boxes at all) is handled before any
    field is built, because it has nothing to build them from.

    The order is not cosmetic: an empty `texts` satisfies all three conditions at once, so only the
    ordering makes that case deterministic.
    """
    if not head or head == "Not found":
        return NOT_A_KK
    if not any(m["nik"]["value"] and m["nama_lengkap"]["value"] for m in members):
        return NOT_A_KK
    return None


def _field(value: str, ocr_conf: float | None = None, crf_conf: float | None = None) -> StructuredField:
    """`value` is never None, and both scores are None when there is no value (§7.3)."""
    if not value:
        return {"value": "", "ocr_conf": None, "crf_conf": None}
    return {"value": value, "ocr_conf": ocr_conf, "crf_conf": crf_conf}

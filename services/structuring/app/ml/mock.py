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
from typing import cast

from ocr_common.kk import (
    DOC_CELL_FEATURES,
    DOC_FIELDS,
    MEMBER_CELL_FEATURES,
    MEMBER_FIELDS,
    SCORED_DOC_FIELDS,
    SCORED_MEMBER_FIELDS,
)
from ocr_common.synthetic_kk import household, nomor_kk
from ocr_common.types import OcrBox, StructuringResult

# §7.4 and the frozen empty shape live in `validity` now that a second backend applies them. They
# are re-exported here because the reasons read as part of this module's behaviour to its callers.
from app.ml.validity import NO_TEXT, NOT_A_KK
from app.ml.validity import empty as _empty
from app.ml.validity import field as _field
from app.ml.validity import reject_reason as _reject_reason

__all__ = ["NOT_A_KK", "NO_TEXT", "MockStructurer"]

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
            "nomor_kk": _field(head, 0.9991, features=_features(DOC_CELL_FEATURES, 1)),
            "nama_kepala_keluarga": _field(
                people[0].nama_lengkap if people else "", 0.9873, features=_features(DOC_CELL_FEATURES, 2)
            ),
        }
        for name in DOC_FIELDS:
            document.setdefault(name, _field(_DOC_VALUES.get(name, ""), 0.97))
        # Scores deliberately differ per member: identical numbers would hide an index shift
        # between this list and scoring's, which is exactly what the variable length exists to expose.
        document["anggota_keluarga"] = [
            {
                name: _field(
                    getattr(person, name),
                    round(0.99 - index * 0.031, 4),
                    round(0.97 - index * 0.043, 4),
                    # Only the seven SCORED fields carry a vector; the other eight are extracted and
                    # never scored, and inventing features for them would suggest otherwise.
                    # Langkah per anggota adalah len(MEMBER_FIELDS), bukan jumlah field yang
                    # diskor: dengan langkah yang lebih kecil dari posisi terbesar, offset anggota
                    # ke-n bertabrakan dengan anggota ke-(n+1) -- dan vektor yang identik antar sel
                    # membuat pergeseran indeks tak terlihat, satu-satunya hal yang seluruh berkas
                    # ini ada untuk mengekspos.
                    features=(
                        _features(MEMBER_CELL_FEATURES, index * len(MEMBER_FIELDS) + position + 3)
                        if name in SCORED_MEMBER_FIELDS
                        else None
                    ),
                )
                for position, name in enumerate(MEMBER_FIELDS)
            }
            for index, person in enumerate(people)
        ]
        return cast(StructuringResult, document)


def _levers(texts: list[OcrBox]) -> dict[str, str]:
    return {key: value for box in texts for key, value in _LEVER.findall(box.get("text") or "")}


def _features(names: tuple[str, ...], offset: int) -> dict[str, float]:
    """A complete feature vector with plausible but synthetic values.

    What matters here is **completeness**, not realism: scoring reads every name in
    `kk.MEMBER_CELL_FEATURES` / `kk.DOC_CELL_FEATURES`, and a missing one becomes NaN in a model that
    was never trained to read it as absent. So the mock emits every key, and the assertion that the
    key sets match is a test rather than a hope.

    The numbers are not realistic and are not meant to be. Until the K2Regex-v2 port replaces this
    backend, a confidence computed from them says nothing about a document -- it only proves the
    vector travelled intact. `offset` keeps every cell's vector distinct so an index shift between
    this list and scoring's cannot hide.
    """
    return {name: round((offset * 7 + index * 13) % 100 / 100.0, 4) for index, name in enumerate(names)}


#: Guard against the drift this file exists to prevent: the mock must emit exactly the names the
#: trust model reads. Checked at import so a rename in `ocr_common.kk` fails loudly here.
assert set(_features(MEMBER_CELL_FEATURES, 0)) == set(MEMBER_CELL_FEATURES)
assert set(_features(DOC_CELL_FEATURES, 0)) == set(DOC_CELL_FEATURES)
assert len(SCORED_DOC_FIELDS) == 2 and len(SCORED_MEMBER_FIELDS) == 7

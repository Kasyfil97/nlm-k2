"""The `mock` backend: a plausible Kartu Keluarga read, derived from the bytes it was given.

This is the backend the whole batch is declared finished with (R20a), so it is worth more than a
constant. Three properties matter, and each one is here because something downstream depends on it:

* **Real §7.1 geometry.** Every box carries a genuinely tilted four-point polygon of floats, because
  that is what the spike measured on a real card and what structuring reads columns from. A mock
  that emitted upright integer rectangles would let an `x1/y1/x2/y2` regression pass unnoticed.
* **Deterministic from the bytes.** The same upload always reads the same, so a fixture is stable
  and a diff means the generator changed.
* **Test levers on the file name**, the convention of `ocr_common.simulation`. They are safe here
  and would not be anywhere else: `EKSTRAKSI_BACKEND=mock` is refused outside `ENVIRONMENT=local`
  by `reject_mock_backend_outside_local`, so this module cannot run where a caller could steer the
  pipeline by naming a file. The `delay<N>s` hook is *not* here -- it belongs to the job, not to the
  model, and `EkstraksiJobService` applies it through `ocr_common.simulation` behind
  `simulation_hooks_enabled`.

Every value comes from `ocr_common.synthetic_kk`, i.e. province code `99`, which Indonesia never
assigns. Nothing in this file may be a real family's card.
"""

import hashlib
import re

from ocr_common.errors import InternalError
from ocr_common.synthetic_kk import SyntheticMember, household, nomor_kk
from ocr_common.types import OcrBox, OcrEngineResult

#: `texts: []` -- the first rejecting rule of the KK validity gate (§7.4) fires at *structuring*,
#: not here: §6.3 is explicit that an image with no readable text is a `DONE` job, not a failure.
#: Unit 10 needs a way to reach that rule from the orchestrator's front door, and this is it.
#: Deliberately not `blur` / `invalid` / `notkk`: those are the guardrails mock's rejections, and a
#: document the guardrails model refuses never reaches this stage at all.
BLANK_TRIGGER = "blank"

#: Makes the stage fail (§6.3: a model that breaks is a `FAILED` job and a 422, never a rejection).
ERROR_TRIGGER = "servererror"

#: `MOCK:key=value` in the file name is echoed into the OCR text, which is where the structuring
#: mock reads its own levers from. Without this the two mocks could only be driven separately, and
#: the §7.4 paths that Unit 10 has to walk end to end would have no way in.
#:
#: The value is `\w*`, narrower than the `[^\s]*` the structuring mock parses with, because here
#: the lever sits in a file name: `kk-MOCK:members=0.jpg` would otherwise carry the extension into
#: the value and `int("0.jpg")` would fail one service away from the cause.
_LEVER = re.compile(r"MOCK:\w+=\w*")

#: Rough glyph width and line pitch of a KK photo at the size these polys pretend to be.
_CHAR_WIDTH = 11.0
_LINE_HEIGHT = 34.0
_LEFT = 42.0
_TOP = 56.0
#: The card is photographed at a slight angle, so every line drops a little from left to right.
#: `poly[0]` of the spike ran y 7 -> 6 -> 26 -> 28 for one box; the sign matters less than the fact
#: that it is not zero.
_TILT = 0.014


class MockOcrEngine:
    """Synthetic KK text lines with §7.1 geometry. Synchronous: it is an in-process `OcrRecognizer`,
    so it runs in the threadpool exactly like the real backend and the off-loop path is the tested one."""

    name = "mock"

    def read(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult:
        name = filename or ""
        levers = _LEVER.findall(name)
        # The substring triggers are matched against the name with the `MOCK:` levers REMOVED.
        # A lever's key may legitimately contain a trigger word, and the structuring mock has one
        # that does: `kk-MOCK:blank_kk=1.jpg` contains `blank`, so before this the file meant to
        # exercise §7.4 rule 2 (KK number missing) returned no text at all and hit rule 1 instead --
        # leaving rule 2 unreachable from the orchestrator's front door, and silently so, because
        # both rules answer 400. Found by the Unit 10 smoke test on its first real run.
        plain = _LEVER.sub("", name).lower()
        if ERROR_TRIGGER in plain:
            raise InternalError("Internal server error while processing OCR")
        if BLANK_TRIGGER in plain:
            # A complete, valid result that happens to have nothing in it. Both aggregates become
            # null in `ocr_aggregates`, and the rejection happens one stage later.
            return {"texts": [], "model": None}

        seed = int(hashlib.sha256(content).hexdigest()[:8], 16)
        lines = [*levers, *_card_lines(seed)]
        return {"texts": [_box(index, text, seed) for index, text in enumerate(lines)], "model": "mock-kk-v1"}


def _card_lines(seed: int) -> list[str]:
    """What a KK photo reads as, roughly in card order: the header block, then one line per member."""
    people = household(3, seed=seed)
    return [
        "KARTU KELUARGA",
        f"No. {nomor_kk(seed)}",
        f"Nama Kepala Keluarga : {people[0].nama_lengkap}",
        "Alamat : JL. MERDEKA NO. 12",
        "RT/RW : 003/007",
        "Desa/Kelurahan : CIHAPIT",
        "Kecamatan : BANDUNG WETAN",
        "Kabupaten/Kota : KOTA BANDUNG",
        "Provinsi : JAWA BARAT",
        "Kode Pos : 40114",
        *(_member_line(position, person) for position, person in enumerate(people, start=1)),
        "Dikeluarkan tanggal : 12-03-2019",
    ]


def _member_line(position: int, person: SyntheticMember) -> str:
    return (
        f"{position} {person.nama_lengkap} {person.nik} {person.jenis_kelamin} "
        f"{person.tempat_lahir} {person.tanggal_lahir} {person.status_hubungan_dalam_keluarga}"
    )


def _box(index: int, text: str, seed: int) -> OcrBox:
    """One §7.1 box: a tilted quadrilateral of floats, and a score that varies per line.

    The scores vary on purpose. `avg_doc_score` and `min_doc_score` are trust-model features, and a
    mock where every line scored the same would make a broken aggregate indistinguishable from a
    working one.
    """
    top = _TOP + index * _LINE_HEIGHT
    width = _CHAR_WIDTH * max(len(text), 1)
    drop = width * _TILT
    poly = [
        [_LEFT, round(top, 1)],
        [round(_LEFT + width, 1), round(top + drop, 1)],
        [round(_LEFT + width, 1), round(top + drop + 26.0, 1)],
        [_LEFT, round(top + 26.0, 1)],
    ]
    # 0.74 .. 0.9999, derived from the bytes and the line so the spread is stable but not flat.
    jitter = ((seed >> (index % 16)) % 260) / 1000
    return {"text": text, "score": round(0.9999 - jitter, 4), "poly": poly}

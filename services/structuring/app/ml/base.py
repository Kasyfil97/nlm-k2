from typing import Protocol

from ocr_common.types import OcrBox, StructuringResult


class Structurer(Protocol):
    """Turns OCR boxes (`{text, score, poly}`) into the Kartu Keluarga fields.

    Returns the flat §7.3 shape: the eleven document fields as top-level keys, `anggota_keluarga`
    with fifteen fields per row, and `reject_reason`. Every field is `{value, ocr_conf, crf_conf}`
    and `value` is `""` when not found -- never `None`.

    A structurer **never raises for what it cannot read**. An unreadable card comes back as a
    complete object with empty values and a `reject_reason`; the pipeline then stores the job `DONE`
    (the result stays readable), skips the hand-off, and the client gets a 400 carrying that reason.
    Raising instead would turn a rejected document into a failed stage, which is a different
    outcome with a different error code and no readable result.
    """

    name: str

    def structure(self, texts: list[OcrBox]) -> StructuringResult: ...

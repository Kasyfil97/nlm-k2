from collections.abc import Mapping
from typing import Any

from ocr_common.types import OcrBox, StructuringResult

from app.ml.base import Structurer


class StructuringService:
    """Runs the structurer over the OCR boxes and returns the stage result (`StructuringResult`).

    Note what it does **not** do: it does not refuse an empty input. nilam raised `BadRequest("No
    text lines to structure")` here, which for a Kartu Keluarga is the wrong answer -- §7.4 makes
    "no text boxes at all" the *first rejecting rule*, so an empty image has to reach the rules and
    come back as a `reject_reason`, not as a 400 from this layer. The two look alike to a caller and
    are not: a rejection is a `DONE` job whose result stays readable, a 400 here is a stage failure.

    Blank boxes are still dropped: a box the recogniser produced with no text is noise, not content --
    except for a structurer that says `reads_blank_boxes`. `kk_model` does: it was trained on the OCR
    output as is, blank boxes included, and they move its row and neighbour features (dropping them
    changes 684 of the 10414 field scores of the training corpus).
    """

    def __init__(self, structurer: Structurer):
        self._structurer = structurer

    @staticmethod
    def boxes_from_ocr(ocr: Mapping[str, Any]) -> list[OcrBox]:
        """The OCR stage's result (`texts`, §7.1) as the boxes the structurer reads, defaults filled in."""
        return [
            {
                "text": box.get("text") or "",
                "score": box.get("score", 1.0),
                "poly": box.get("poly") or [],
            }
            for box in ocr.get("texts") or []
        ]

    def structure(self, texts: list[OcrBox]) -> StructuringResult:
        if getattr(self._structurer, "reads_blank_boxes", False):
            return self._structurer.structure(texts)
        return self._structurer.structure([box for box in texts if (box.get("text") or "").strip()])

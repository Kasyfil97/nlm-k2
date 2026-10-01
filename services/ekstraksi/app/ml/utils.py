"""Turning what a PP-OCR engine returns into the frozen §7.1 boxes.

Both engines that read a real card produce the same intermediate shape -- one dict with three
parallel lists, `{"rec_texts": [...], "rec_scores": [...], "rec_polys": [...]}`. The in-process
torch path returns it directly (K2Extractor `_to_paddle_result`, confirmed by the §7.1 spike), and
the ML team's model service returns the same three keys per page over HTTP. So the conversion lives
here once instead of twice, and `{text, score, poly}` is produced in exactly one place.

The conversion is the stage's own job, not something inherited: §7.1 says so, and K2Extractor's
`transform_ocr_result` converts the same lists into the *old* `[(poly, (text, score))]` shape that
§12 records as superseded.
"""

from typing import Any

from ocr_common.types import OcrBox

#: §7.1 / `OcrBoxPayload`: four points of two coordinates. Not a count we may relax -- structuring
#: assigns cells to columns from this geometry, and the detector genuinely returns tilted quads.
POLY_POINTS = 4


def model_name(models: Any) -> str | None:
    """`detection+recognition` as the engine reports it, or whichever single name it has."""
    if not isinstance(models, dict):
        return None
    detection, recognition = models.get("detection"), models.get("recognition")
    if detection and recognition:
        return f"{detection}+{recognition}"
    return detection or recognition or models.get("pipeline") or None


def score_of(value: Any) -> float:
    """A recognition score clamped into [0, 1]. `OcrBoxPayload` bounds it, so a model that reports
    1.0000000001 (float32 rounding does happen) must not become a 422 at the next stage."""
    try:
        return round(min(max(float(value), 0.0), 1.0), 4)
    except (TypeError, ValueError):
        return 0.0


def poly_of(value: Any) -> list[list[float]] | None:
    """Four `[x, y]` points as plain floats, or None when the polygon is missing or malformed.

    Floats, not integers: the spike measured `float32` coordinates on every one of 177 boxes, and
    rounding them to whole pixels would quietly coarsen the geometry structuring reads columns from.
    `float(point[0])` also unwraps numpy scalars, which is what the torch path hands over.
    """
    try:
        points = [[float(point[0]), float(point[1])] for point in value]
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return points if len(points) == POLY_POINTS else None


def box_of(entry: Any) -> OcrBox | None:
    """One `{text, score, poly}` object validated into a §7.1 box, or None when it cannot be one.

    The PP-OCRv6 VM already emits this shape, so there is nothing to convert -- but "already the
    right shape" is a claim about a remote service, so the same two rules still apply as for the
    parallel lists: no empty text, and exactly four polygon points. Passing a three-point polygon on
    would turn this stage's success into structuring's 422.
    """
    if not isinstance(entry, dict):
        return None
    line = str(entry.get("text") or "").strip()
    points = poly_of(entry.get("poly"))
    if not line or points is None:
        return None
    return {"text": line, "score": score_of(entry.get("score")), "poly": points}


def boxes_from_rec_lists(texts: Any, scores: Any, polys: Any) -> list[OcrBox]:
    """`rec_texts` / `rec_scores` / `rec_polys` as one list of §7.1 boxes.

    A box is dropped when its text is empty after stripping, or when its polygon is not four points:
    the next stage's schema requires exactly four, so passing a malformed one on would turn this
    stage's success into structuring's 422. Losing one box is the smaller, visible failure -- the
    count is in `text_regions_count`.
    """
    if not isinstance(texts, list) or not isinstance(scores, list) or not isinstance(polys, list):
        return []
    if not (len(texts) == len(scores) == len(polys)):
        return []

    boxes: list[OcrBox] = []
    for text, score, poly in zip(texts, scores, polys, strict=True):
        line = str(text or "").strip()
        points = poly_of(poly)
        if not line or points is None:
            continue
        boxes.append({"text": line, "score": score_of(score), "poly": points})
    return boxes

"""The `remote` backend: the ML team's OCR model service and its `/v1/predict/json` contract.

Their service answers `{models: {detection, recognition}, pages: [{page_index, rec_texts,
rec_scores, rec_polys}], ...}`; the earlier shapes, a bare list of pages or `{data: [...]}`, are
still accepted. Only `pages` and `models` are read -- the document aggregates of §7.1 are computed
from the boxes by `ocr_common.kk.ocr_aggregates`, never taken from whatever the model reports, so
one definition of `avg_doc_score` exists rather than two.

A Kartu Keluarga is one image, so the pages are concatenated into a single `texts` list in page
order. The stage has no page concept: §7.1 has none, `OcrBox` has none, and PDF is off by default
(`PDF_ENABLED`, R34a).

Kept rather than dropped alongside the in-process backend because R18 ties that choice to the same
choice in guardrails, which still ships its own `remote`. If guardrails drops it, this goes too.
"""

from typing import Any

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.errors import InternalError
from ocr_common.types import OcrBox, OcrEngineResult

from app.ml.utils import boxes_from_rec_lists, model_name


class RemoteOcrEngine:
    """An OCR model behind HTTP. Async, and already off this event loop, so no threadpool hop."""

    name = "remote"
    PREDICT_PATH = "/v1/predict/json"

    def __init__(self, client: RemoteModelClient, params: dict[str, Any] | None = None):
        self._client = client
        # In production one OCR model serves several document types and takes its parameters per
        # call, so EKSTRAKSI_OCR_PARAMS rides along as extra form fields on every request.
        self._params = {key: str(value) for key, value in (params or {}).items()}

    async def extract(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult:
        body = await self._client.post_multipart(
            self.PREDICT_PATH,
            filename=filename or "upload",
            content=content,
            content_type=content_type or "image/jpeg",
            data=self._params or None,
        )
        return {"texts": parse_pages(body, self._client.name), "model": parse_model(body)}

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_model(body: Any) -> str | None:
    """`models.detection+recognition` of the current contract; None for the older shapes."""
    return model_name(body.get("models")) if isinstance(body, dict) else None


def parse_pages(body: Any, name: str) -> list[OcrBox]:
    """Every page's `rec_*` lists as one flat list of §7.1 boxes, in page order."""
    unexpected = InternalError(f"{name} returned an unexpected response")
    if isinstance(body, dict):
        pages = body.get("pages") if "pages" in body else body.get("data")
    else:
        pages = body
    if not isinstance(pages, list):
        raise unexpected

    boxes: list[OcrBox] = []
    for page in pages:
        if not isinstance(page, dict):
            raise unexpected
        texts, scores, polys = page.get("rec_texts"), page.get("rec_scores"), page.get("rec_polys")
        if not isinstance(texts, list) or not isinstance(scores, list) or not isinstance(polys, list):
            raise unexpected
        if not (len(texts) == len(scores) == len(polys)):
            raise unexpected
        boxes.extend(boxes_from_rec_lists(texts, scores, polys))
    return boxes

"""The `remote` backend: an OCR model served by another process.

**Two response shapes, both real, both accepted per page.**

1. Three parallel lists, `rec_texts` / `rec_scores` / `rec_polys` -- what §7.1 records and what the
   in-process torch path produces, so `app/ml/utils.py` is shared.
2. `texts: [{text, score, poly}]` -- what the PP-OCRv6 VM the ML team hosts returns. That is
   *already* the frozen §7.1 box, so nothing is converted; it is validated and passed through.

Sniffing the shape rather than configuring it is deliberate: the two are distinguishable by their
keys with no ambiguity, and a wrong setting here would look like a model that found no text.

A list of documents, `[{models, pages}, ...]`, is read as their pages in order; the PaddleOCR
server the `paddle` backend calls may answer that way.

The earlier shapes -- a bare list of pages, or `{data: [...]}` -- are still accepted. Only `pages`
and `models` are read. In particular **`page.width` / `page.height` are ignored, and must be**: with
`use_doc_orientation_classify=true` the VM returns `poly` in the *orientation-corrected* frame while
still reporting the *submitted* dimensions, so for a sideways photo the two disagree and the polys
do not fit the page it names (measured: a 620x1000 submission came back as `page=620x1000` with a
poly bounding box reaching x=812). §7.1 has no page dimensions, which is what keeps this stage out
of that trap.

The document aggregates of §7.1 are computed from the boxes by `ocr_common.kk.ocr_aggregates`, never
taken from whatever the model reports, so one definition of `avg_doc_score` exists rather than two.

**Only the first page is read.** A Kartu Keluarga is one sheet, and the layout parser in structuring
reads one page frame: boxes from a second page of a PDF would land on top of the first page's
coordinates and break its zoning, so they are dropped rather than concatenated. The stage has no
page concept -- §7.1 has none, `OcrBox` has none -- and guardrails judges the same page 1.

Kept rather than dropped alongside the in-process backend because R18 ties that choice to the same
choice in guardrails, which still ships its own `remote`. If guardrails drops it, this goes too.
"""

from typing import Any

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.errors import InternalError
from ocr_common.types import OcrBox, OcrEngineResult

from app.ml.utils import box_of, boxes_from_rec_lists, model_name

#: The contract's path. The PP-OCRv6 VM serves `/ocr`; hence `EXTRACTION_OCR_PATH`.
DEFAULT_PATH = "/v1/predict/json"


def _wire(values: dict[str, Any] | None) -> dict[str, str]:
    """Settings values as the wire wants them. `True` must go out as `true`, not `True`: the VM
    parses these query parameters as strings and Python's `str(True)` is not one it accepts."""
    return {
        key: ("true" if value is True else "false" if value is False else str(value))
        for key, value in (values or {}).items()
    }


class RemoteOcrEngine:
    """An OCR model behind HTTP. Async, and already off this event loop, so no threadpool hop."""

    name = "remote"
    PREDICT_PATH = DEFAULT_PATH

    def __init__(
        self,
        client: RemoteModelClient,
        params: dict[str, Any] | None = None,
        *,
        path: str | None = None,
        query: dict[str, Any] | None = None,
    ):
        self._client = client
        self._path = path or DEFAULT_PATH
        # In production one OCR model serves several document types and takes its parameters per
        # call. Where those knobs go differs per service, so both are supported: form fields for the
        # contract's service, query parameters for the VM.
        self._params = _wire(params)
        self._query = _wire(query)

    async def extract(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult:
        body = await self._client.post_multipart(
            self._path,
            filename=filename or "upload",
            content=content,
            content_type=content_type or "image/jpeg",
            data=self._params or None,
            params=self._query or None,
        )
        return {"texts": parse_pages(body, self._client.name), "model": parse_model(body)}

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_model(body: Any) -> str | None:
    """`models.detection+recognition` of the current contract; None for the older shapes. For a
    list of documents, the first one that names its models."""
    for document in _documents(body) or [body]:
        if isinstance(document, dict) and (name := model_name(document.get("models"))):
            return name
    return None


def _documents(body: Any) -> list[dict[str, Any]] | None:
    """The body as a list of documents, when it is one: `[{models, pages}, ...]`, which the
    PaddleOCR server may answer instead of a single document. A bare list of *pages* -- the older
    shape -- has no `pages` key in its items and is not this."""
    if isinstance(body, list) and body and all(isinstance(item, dict) and "pages" in item for item in body):
        return body
    return None


def parse_pages(body: Any, name: str) -> list[OcrBox]:
    """The first page's §7.1 boxes, whichever shape it arrived in; later pages are ignored.

    Every page must still be an object -- a malformed body is an error, not silently page 1.
    """
    unexpected = InternalError(f"{name} returned an unexpected response")
    documents = _documents(body)
    if documents is not None:
        pages: Any = []
        for document in documents:
            if not isinstance(document.get("pages"), list):
                raise unexpected
            pages.extend(document["pages"])
    elif isinstance(body, dict):
        pages = body.get("pages") if "pages" in body else body.get("data")
    else:
        pages = body
    if not isinstance(pages, list):
        raise unexpected

    if not all(isinstance(page, dict) for page in pages):
        raise unexpected
    return _page_boxes(pages[0], unexpected) if pages else []


def _page_boxes(page: dict[str, Any], unexpected: InternalError) -> list[OcrBox]:
    """One page, either shape. `texts` as a list of dicts wins when both are present, because that
    is the shape that carries the boxes already assembled."""
    entries = page.get("texts")
    if isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries):
        return [box for box in (box_of(entry) for entry in entries) if box is not None]

    texts, scores, polys = page.get("rec_texts"), page.get("rec_scores"), page.get("rec_polys")
    if not isinstance(texts, list) or not isinstance(scores, list) or not isinstance(polys, list):
        raise unexpected
    if not (len(texts) == len(scores) == len(polys)):
        raise unexpected
    return boxes_from_rec_lists(texts, scores, polys)

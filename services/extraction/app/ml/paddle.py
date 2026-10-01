"""The `paddle` backend: the PaddleOCR model server the ML team hosts, called on `POST /ocr`.

The same server nilam's `paddle` backend calls, and the same name, so one deployment value
(`EXTRACTION_BACKEND=paddle`, as `deploy/helm` already sets) means the same thing in both repos.

It is `remote` with the path fixed to `/ocr` and no form fields, not a second parser. The server
answers `pages[].texts[{text, score, poly}]`, which is already the §7.1 box, and `remote.parse_pages`
already reads that shape -- including the list-of-documents body the server may return. What
nilam's backend does with the answer is deliberately **not** copied: it turns each `poly` into an
upright integer `bbox`, which throws away the tilt the layout parser in structuring measures
(`estimate_shear`). Here the quadrilateral travels on unchanged.

The knobs go in the query string (`EXTRACTION_OCR_QUERY`), as with `remote` against the same server:
`use_doc_orientation_classify=true` is what fixes a sideways photo; see
`docs/decisions/2026-09-27-r18b-orientasi-dan-pelurusan.md`.

Nothing here imports the `paddle` package -- the model runs in the other process.
"""

from typing import Any

from ocr_common.clients.remote import RemoteModelClient

from app.ml.remote import RemoteOcrEngine

#: The server's one OCR route.
PADDLE_PATH = "/ocr"


class PaddleOcrEngine(RemoteOcrEngine):
    name = "paddle"
    PREDICT_PATH = PADDLE_PATH

    def __init__(self, client: RemoteModelClient, *, query: dict[str, Any] | None = None):
        super().__init__(client, path=PADDLE_PATH, query=query)

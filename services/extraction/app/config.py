from functools import lru_cache
from pathlib import Path
from typing import Any, Self

from pydantic import Field, model_validator

from ocr_common.config import PipelineSettings


class Settings(PipelineSettings):
    port: int = 8042

    #: `mock` | `kk_ocr` | `paddle` | `remote`. `kk_ocr` is the in-process PP-OCRv5 det + rec path;
    #: see `app/ml/kk_ocr.py` for why it is the torch backend of K2Extractor's two and not the default
    #: one. `paddle` is the ML team's PaddleOCR server on `POST /ocr` (as in nilam).
    extraction_backend: str = "mock"

    # --- `paddle` and `remote`: an OCR model served by another process ----------------------
    # `paddle` uses the URL, key, timeout and query below; the path is fixed to `/ocr` and it
    # sends no form fields.
    extraction_ocr_url: str | None = None
    extraction_ocr_api_key: str | None = None
    extraction_ocr_timeout_seconds: float = 30.0
    # The path on that service. Default is the contract's `/v1/predict/json`; the PP-OCRv6 VM the
    # ML team actually hosts serves `/ocr`, so this is a setting rather than a constant.
    extraction_ocr_path: str = "/v1/predict/json"
    # Extra FORM fields sent with every call (JSON object). The ML team's OCR model will serve
    # several document types in production and take its parameters per call.
    extraction_ocr_params: dict[str, Any] = {}
    # Extra QUERY parameters (JSON object). Separate from the form fields because the two services
    # differ on where the knobs go: the PP-OCRv6 VM reads
    # `use_doc_orientation_classify` / `use_doc_unwarping` / `use_textline_orientation` from the
    # query string and ignores form fields entirely. Booleans are sent as `true` / `false`.
    extraction_ocr_query: dict[str, Any] = {}

    # --- `kk_ocr` only: where the runtime and the weights are --------------------------------
    # The `.pth` weights are not committed; `scripts/fetch_weights.py` brings them in.
    extraction_ppocr_root: Path = Path("/app/ppocr")
    extraction_det_weights_path: Path = Path("/app/weights/ppocrv5_server_det.pth")
    extraction_rec_weights_path: Path = Path("/app/weights/ppocrv5_server_rec.pth")
    extraction_device: str = "cpu"
    # Four knobs rather than four constants: the §7.1 spike ran this backend on its constructor
    # defaults and got 177 mostly single-character boxes from one card. It validated the payload
    # shape, explicitly not the recognition quality, so whoever brings the weights has to tune these.
    extraction_rec_batch_size: int = 8
    extraction_rec_image_shape: str = "3,48,320"
    extraction_det_limit_side_len: int = 64
    extraction_det_limit_type: str = "min"
    # 0 leaves torch's own default. Pinning it matters on a shared node: the default is one thread
    # per core, and several replicas each taking every core is slower than each taking a few.
    extraction_torch_threads: int = 0
    # Page 1 of a PDF is rendered at this DPI before detection (only page 1 is read).
    extraction_pdf_dpi: int = Field(200, ge=72, le=600)

    structuring_service_url: str = "http://127.0.0.1:8043"
    structuring_api_key: str | None = None
    structuring_timeout_seconds: float = 10.0

    @model_validator(mode="after")
    def _guard_extraction(self) -> Self:
        self.reject_mock_backend_outside_local(extraction_backend=self.extraction_backend)
        self.reject_localhost_outside_local(structuring_service_url=self.structuring_service_url)
        # R18a: this service downloads `file_url` itself, so the allow-list is required here as it
        # is in the orchestrator -- always, not behind a switch. An empty list denies every URL, so
        # starting without one would leave the download path dead; failing at start-up says that
        # once instead of once per job. The policy itself (https only, no redirects, private and
        # loopback addresses refused even for a listed host) is `settings.file_url_policy`, which
        # `read_image` and `fetch` already apply -- this service does not write a second one.
        self.require_file_url_allowlist()
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

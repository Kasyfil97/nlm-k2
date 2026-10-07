from functools import lru_cache
from typing import Self

from pydantic import model_validator

from ocr_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    port: int = 8041

    guardrails_backend: str = "mock"

    guardrails_model_url: str | None = None
    guardrails_model_api_key: str | None = None
    guardrails_model_timeout_seconds: float = 30.0

    # Six artifacts, not one file: blur_cnn_weights.pt, blur_cnn_meta.json, xgb_model.json,
    # calibration.json, feature_names.json and the optional model_hashes.json. The backend owns
    # their names (app/ml/kk_quality.py); what is configured is the directory holding them.
    guardrails_weights_dir: str = "weights"
    guardrails_device: str = "cpu"
    guardrails_torch_threads: int | None = None

    # No configured threshold: only the request's `threshold` field refuses a document; without it the
    # document passes and `probability_bad` is answered (GUARDRAILS_THRESHOLD and GUARDRAILS_THRESHOLD_URL
    # are gone).

    # §13.3, R18a. Off: the orchestrator sends the bytes it already downloaded and a `file_url` here
    # is refused. On: this service downloads too, and therefore needs its own FILE_URL_ALLOWED_HOSTS.
    # There is no URL policy in this service -- `read_image` applies `settings.file_url_policy`, the
    # one hardened in Unit 2. A second SSRF implementation is the failure this switch must not cause.
    guardrails_fetch_url: bool = False

    @model_validator(mode="after")
    def _guard_guardrails(self) -> Self:
        self.reject_mock_backend_outside_local(guardrails_backend=self.guardrails_backend)
        self.reject_localhost_outside_local(guardrails_model_url=self.guardrails_model_url)
        if self.guardrails_fetch_url:
            # Same rule the orchestrator and extraction start under, applied here only when the
            # switch makes this service a downloader: an empty allow-list denies every URL, so
            # starting like this would leave the path dead until the first request said so.
            self.require_file_url_allowlist()
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

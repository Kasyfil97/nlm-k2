from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import model_validator

from ocr_common.config import PipelineSettings


class Settings(PipelineSettings):
    port: int = 8043

    # `mock` (fields invented), `kk_regex` (the vendored K2Regex-v2 layout parser) or `kk_model` (the
    # trained key classifier m04, the pair of scoring's `kk_field`). Only the last two derive anything
    # from the submitted image, so `mock` is refused outside `local`.
    structuring_backend: str = "mock"

    #: `kk_model` only: the artifact exported by `scoring/training/export_nlm_k2.py` (three chained
    #: models + the frozen closed vocabulary). Fetched by `scripts/fetch_weights.py structuring`.
    structuring_model_path: Path = Path("weights/kk_structuring_model.joblib")

    scoring_service_url: str = "http://127.0.0.1:8044"
    scoring_api_key: str | None = None
    scoring_timeout_seconds: float = 10.0

    @model_validator(mode="after")
    def _guard_structuring(self) -> Self:
        self.reject_localhost_outside_local(scoring_service_url=self.scoring_service_url)
        self.reject_mock_backend_outside_local(structuring_backend=self.structuring_backend)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

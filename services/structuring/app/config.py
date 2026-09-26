from functools import lru_cache
from typing import Self

from pydantic import model_validator

from ocr_common.config import PipelineSettings


class Settings(PipelineSettings):
    port: int = 8043

    # Satu-satunya backend di batch ini. Port K2Regex-v2 menambahkannya di sini.
    structuring_backend: str = "mock"

    scoring_service_url: str = "http://127.0.0.1:8044"
    scoring_api_key: str | None = None
    scoring_timeout_seconds: float = 10.0

    @model_validator(mode="after")
    def _guard_structuring(self) -> Self:
        self.reject_localhost_outside_local(scoring_service_url=self.scoring_service_url)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

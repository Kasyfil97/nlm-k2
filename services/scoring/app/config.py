from functools import lru_cache

from ocr_common.config import PipelineSettings


class Settings(PipelineSettings):
    port: int = 8044

    # Satu-satunya backend di batch ini. Model terkalibrasi menambahkannya di sini.
    scoring_backend: str = "mock"

    scoring_approve_threshold: float = 0.8
    scoring_review_threshold: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()

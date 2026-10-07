from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import model_validator

from ocr_common.config import PipelineSettings


class Settings(PipelineSettings):
    port: int = 8044

    # Bawaannya `kk_field`: trust model s11 di atas structuring `kk_model` -- model terlatih adalah
    # jalur yang sesungguhnya, jadi ia tidak boleh bergantung pada seseorang mengingat untuk
    # menyalakannya. `calibrated` adalah pasangan lama `kk_regex` (artefak format lama). `mock`
    # mengarang angka, ditolak di luar ENVIRONMENT=local, dan opt-in -- uji yang mengujinya menyebutnya.
    scoring_backend: str = "kk_field"

    #: Artefak joblib dari repo pelatihan (`scoring/training/export_nlm_k2.py` untuk `kk_field`):
    #: model, kolom yang dipakai saat fit, dan ambang per field-nya sendiri. Semuanya BUKAN
    #: konfigurasi -- berpindah bersama bobotnya, kalau tidak angkanya diam-diam berubah arti.
    scoring_model_path: Path = Path("weights/kk_trust_model.joblib")

    scoring_approve_threshold: float = 0.8
    scoring_review_threshold: float = 0.5

    @model_validator(mode="after")
    def _guard_scoring(self) -> Self:
        self.reject_mock_backend_outside_local(scoring_backend=self.scoring_backend)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

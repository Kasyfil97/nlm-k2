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

    #: Gate klaim: sebuah field hanya boleh `confidence` 1 bila dokumennya lolos KEDUANYA -- `probability_bad`
    #: guardrails di bawah `scoring_claim_gate_p_bad` dan `probability_crop` di bawah `scoring_claim_gate_p_crop`.
    #: Konfigurasi service, sengaja TIDAK bisa diubah dari request: `column_confidence_threshold` hanya
    #: mengatur ambang per field di dalam gate. Nilai dari kk_eval (28 Okt 2026): 0,777 = ambang calib model crop
    #: mnv3s_v1 (ok salah tolak <= 1%); 0,0704 = batas p_bad tertinggi dengan 0 klaim salah bersama syarat crop
    #: itu (dipilih di test, jadi deskriptif). Dokumen tanpa salah satu angka (guardrails dilewati, unassessable,
    #: backend tanpa model crop) tidak lolos gate.
    scoring_claim_gate_p_bad: float = 0.0704
    scoring_claim_gate_p_crop: float = 0.777

    @model_validator(mode="after")
    def _guard_scoring(self) -> Self:
        self.reject_mock_backend_outside_local(scoring_backend=self.scoring_backend)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

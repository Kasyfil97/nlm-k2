from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator

from ocr_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    port: int = 8040

    # The guardrails model service: every document is judged there before it enters the pipeline.
    guardrails_service_url: str = "http://127.0.0.1:8041"
    guardrails_api_key: str | None = None
    guardrails_timeout_seconds: float = 20.0

    # A PDF with more pages is refused with 400 here (a Kartu Keluarga upload is at most 2 pages, ML
    # team 23 Sep 2026; only the first is read). MAX_UPLOAD_BYTES (413) is checked here too.
    max_document_pages: int = Field(2, ge=1)

    ekstraksi_service_url: str = "http://127.0.0.1:8042"
    ekstraksi_api_key: str | None = None
    ekstraksi_timeout_seconds: float = 10.0
    structuring_service_url: str = "http://127.0.0.1:8043"
    structuring_api_key: str | None = None
    structuring_timeout_seconds: float = 10.0
    scoring_service_url: str = "http://127.0.0.1:8044"
    scoring_api_key: str | None = None
    scoring_timeout_seconds: float = 10.0
    pipeline_retry_attempts: int = 3
    pipeline_retry_delay_seconds: float = 0.5
    # 30, bukan 15: itu yang didaftarkan kontrak §13, dan angka 15 hanya terbawa dari struktur
    # yang disalin. Anggarannya dihitung sejak request tiba dan tidak pernah di-reset, jadi
    # pemeriksaan guardrails yang lambat memakan bagian dari angka ini, bukan menambah waktu.
    pipeline_wait_seconds: float = Field(30.0, ge=0)
    pipeline_poll_interval_seconds: float = Field(0.5, gt=0)

    # Rate limit and CORS. Kontrak §12 mendaftarkan keduanya "dipertahankan apa adanya", tetapi struktur
    # yang disalin tidak punya keduanya -- ditemukan saat Unit 4. Orchestrator satu-satunya service yang
    # terekspos ke luar jaringan, jadi keduanya hidup di sini saja.
    #
    # Batasnya per (API key, klien): kunci lebih dulu, karena itu yang membedakan pemanggil; alamat hanya
    # dipakai kalau kunci tidak ada, dan yang itu hanya /health dan /ready yang tidak dibatasi sama sekali.
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(60, gt=0)
    rate_limit_window_seconds: float = Field(60.0, gt=0)

    # Kosong = tidak ada CORSMiddleware sama sekali. Ini bawaan yang benar: pemanggil kontrak adalah
    # Orkestrasi pusat server-ke-server, bukan browser, dan CORS terbuka pada service ber-API-key adalah
    # cara memberi halaman mana pun jalan memakai kunci yang bocor.
    cors_allow_origins: str = ""

    @property
    def cors_origins(self) -> list[str]:
        """`CORS_ALLOW_ORIGINS` sebagai daftar. `*` sengaja TIDAK diperlakukan istimewa: ia hanya asal
        biasa di sini, dan `allow_credentials` tidak pernah dinyalakan, jadi tidak ada kombinasi yang
        membuat browser mana pun mengirim kredensial ke service ini."""
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _guard_orchestrator(self) -> Self:
        # R18a: service ini mengunduh `file_url`, jadi daftar host wajib ada di luar local. Tanpa ini
        # kebijakannya tetap menolak semua -- gagal tertutup, tetapi diam-diam, saat request pertama.
        self.require_file_url_allowlist()
        # All four always: GET /v1/extract-ocr/{request_id} reads the stages even when POST does not wait.
        self.reject_localhost_outside_local(
            guardrails_service_url=self.guardrails_service_url,
            ekstraksi_service_url=self.ekstraksi_service_url,
            structuring_service_url=self.structuring_service_url,
            scoring_service_url=self.scoring_service_url,
        )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

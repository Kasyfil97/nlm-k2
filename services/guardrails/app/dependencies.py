"""Composition root: the one place that decides which implementation of each part runs.

Every `get_*` here is what the routes take through `Depends(...)` and what tests replace through
`app.dependency_overrides[...]`. Nothing else in the service builds these objects."""

from functools import lru_cache

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.registry import Factory, build_backend

from app.config import Settings, get_settings
from app.ml.base import QualityBackend
from app.ml.kk_quality import KKQualityModel
from app.ml.mock import MockQualityModel
from app.ml.remote import RemoteGuardrailsModel
from app.services.guardrails_service import GuardrailsService


def _build_remote(settings: Settings) -> RemoteGuardrailsModel:
    if not settings.guardrails_model_url:
        raise RuntimeError("GUARDRAILS_MODEL_URL is required when GUARDRAILS_BACKEND=remote")
    headers = {"X-API-Key": settings.guardrails_model_api_key} if settings.guardrails_model_api_key else None
    client = RemoteModelClient(
        settings.guardrails_model_url,
        settings.guardrails_model_timeout_seconds,
        name="guardrails model",
        headers=headers,
    )
    return RemoteGuardrailsModel(client)


# GUARDRAILS_BACKEND -> how to build it. Add a backend here and, if it needs settings, in config.py.
# `kk_quality` keeps torch, cv2, xgboost and scipy behind its own __init__, so importing this module
# costs nothing in a process running `mock`.
QUALITY_BACKENDS: dict[str, Factory[QualityBackend]] = {
    "mock": lambda settings: MockQualityModel(),
    "kk_quality": lambda settings: KKQualityModel(
        settings.guardrails_weights_dir,
        settings.guardrails_device,
        settings.guardrails_torch_threads,
    ),
    "remote": _build_remote,
}


# --- ML ---------------------------------------------------------------------------


@lru_cache
def get_quality_model() -> QualityBackend:
    settings: Settings = get_settings()
    return build_backend(QUALITY_BACKENDS, settings.guardrails_backend, settings, "guardrails")


# --- services (cheap to build: one per request) ------------------------------------------


def get_guardrails_service() -> GuardrailsService:
    return GuardrailsService(get_quality_model(), get_settings())

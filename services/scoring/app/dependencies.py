"""Composition root: the one place that decides which implementation of each part runs.

Every `get_*` here is what the routes take through `Depends(...)` and what tests replace through
`app.dependency_overrides[...]`. Nothing else in the service builds these objects."""

from functools import lru_cache

from ocr_common.pipeline import (
    STAGE_SCORING,
    OutboxRelay,
    StagePipeline,
    StageResults,
    StaleJobReaper,
    build_outbox_relay,
    build_stage_pipeline,
    build_stage_results,
    build_stale_job_reaper,
)
from ocr_common.registry import Factory, build_backend

from app.config import Settings, get_settings
from app.ml.base import TrustModel
from app.ml.calibrated import CalibratedTrustModel
from app.ml.kk_field import FieldTrustModel
from app.ml.mock import MockTrustModel
from app.services.confidence_service import ConfidenceService
from app.services.job_service import ScoringJobService

DB_TABLE_PREFIX = "scoring"

# SCORING_BACKEND -> how to build the trust model. `mock` fabricates numbers and is refused outside
# ENVIRONMENT=local by `Settings._guard_scoring`; `kk_field` (default, pair of structuring `kk_model`) and
# `calibrated` (pair of `kk_regex`, old artifact format) load the trained artifact at SCORING_MODEL_PATH.
TRUST_MODEL_BACKENDS: dict[str, Factory[TrustModel]] = {
    "mock": lambda settings: MockTrustModel(),
    "calibrated": lambda settings: CalibratedTrustModel(settings.scoring_model_path),
    "kk_field": lambda settings: FieldTrustModel(settings.scoring_model_path),
}


# --- ML ---------------------------------------------------------------------------


@lru_cache
def get_trust_model() -> TrustModel:
    settings: Settings = get_settings()
    return build_backend(TRUST_MODEL_BACKENDS, settings.scoring_backend, settings, "scoring")


# --- pipeline -----------------------------------------------------------------------


@lru_cache
def get_pipeline() -> StagePipeline:
    return build_stage_pipeline(get_settings(), stage=STAGE_SCORING, table_prefix=DB_TABLE_PREFIX)


@lru_cache
def get_relay() -> OutboxRelay | None:
    return build_outbox_relay(get_settings(), get_pipeline())


@lru_cache
def get_results() -> StageResults | None:
    return build_stage_results(get_settings())


# The testing endpoints (TESTING_ENDPOINTS): the same pipeline on the testing_* tables, without callbacks
# (see ocr_common.testing_endpoints).


@lru_cache
def get_testing_pipeline() -> StagePipeline:
    return build_stage_pipeline(get_settings(), stage=STAGE_SCORING, table_prefix=DB_TABLE_PREFIX, testing=True)


@lru_cache
def get_testing_relay() -> OutboxRelay | None:
    return build_outbox_relay(get_settings(), get_testing_pipeline())


@lru_cache
def get_testing_results() -> StageResults | None:
    return build_stage_results(get_settings(), testing=True)


# --- services (cheap to build: one per request) ------------------------------------------


def get_confidence_service() -> ConfidenceService:
    return ConfidenceService(get_trust_model())


def get_job_service() -> ScoringJobService:
    return ScoringJobService(
        get_pipeline(),
        get_confidence_service(),
        results=get_results(),
    )


def get_testing_job_service() -> ScoringJobService:
    return ScoringJobService(
        get_testing_pipeline(),
        get_confidence_service(),
        results=get_testing_results(),
    )


@lru_cache
def get_reaper() -> StaleJobReaper | None:
    return build_stale_job_reaper(get_settings(), get_pipeline(), get_job_service().resume)


@lru_cache
def get_testing_reaper() -> StaleJobReaper | None:
    return build_stale_job_reaper(get_settings(), get_testing_pipeline(), get_testing_job_service().resume)

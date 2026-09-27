"""Composition root: the one place that decides which implementation of each part runs.

Every `get_*` here is what the routes take through `Depends(...)` and what tests replace through
`app.dependency_overrides[...]`. Nothing else in the service builds these objects."""

import logging
from functools import lru_cache

from ocr_common.pipeline import (
    STAGE_STRUCTURING,
    NextStageClient,
    OutboxRelay,
    StagePipeline,
    StageResults,
    StaleJobReaper,
    build_next_stage_client,
    build_outbox_relay,
    build_stage_pipeline,
    build_stage_results,
    build_stale_job_reaper,
)
from ocr_common.registry import Factory, build_backend
from ocr_common.testing_endpoints import testing_path

from app.config import Settings, get_settings
from app.ml.base import Structurer
from app.ml.kk_regex import KKRegexStructurer
from app.ml.mock import MockStructurer
from app.services.job_service import StructuringJobService
from app.services.structuring_service import StructuringService

logger = logging.getLogger(__name__)

DB_TABLE_PREFIX = "structuring"

# STRUCTURING_BACKEND -> how to build it. Add a backend here and, if it needs settings, in config.py.
# `kk_regex` takes no settings: the parser reads its template from beside its own source, so there is
# no weights directory to point at and nothing to misconfigure.
STRUCTURER_BACKENDS: dict[str, Factory[Structurer]] = {
    "mock": lambda settings: MockStructurer(),
    "kk_regex": lambda settings: KKRegexStructurer(),
}


# --- ML ---------------------------------------------------------------------------


@lru_cache
def get_structurer() -> Structurer:
    settings: Settings = get_settings()
    return build_backend(STRUCTURER_BACKENDS, settings.structuring_backend, settings, "structuring")


# --- pipeline -----------------------------------------------------------------------


SCORING_JOBS_PATH = "/v1/scoring/jobs"


def _next_stage(path: str, name: str) -> NextStageClient:
    settings = get_settings()
    return build_next_stage_client(
        settings,
        base_url=settings.scoring_service_url,
        api_key=settings.scoring_api_key,
        timeout=settings.scoring_timeout_seconds,
        path=path,
        name=name,
    )


@lru_cache
def get_next_stage() -> NextStageClient:
    return _next_stage(SCORING_JOBS_PATH, "scoring service")


@lru_cache
def get_pipeline() -> StagePipeline:
    return build_stage_pipeline(
        get_settings(), stage=STAGE_STRUCTURING, table_prefix=DB_TABLE_PREFIX, next_stage=get_next_stage()
    )


@lru_cache
def get_relay() -> OutboxRelay | None:
    return build_outbox_relay(get_settings(), get_pipeline())


@lru_cache
def get_results() -> StageResults | None:
    return build_stage_results(get_settings())


# The testing endpoints (TESTING_ENDPOINTS): the same pipeline on the testing_* tables, handing off to
# scoring's `-test` endpoint, without callbacks (see ocr_common.testing_endpoints).


@lru_cache
def get_testing_next_stage() -> NextStageClient:
    return _next_stage(testing_path(SCORING_JOBS_PATH), "scoring service (testing)")


@lru_cache
def get_testing_pipeline() -> StagePipeline:
    return build_stage_pipeline(
        get_settings(),
        stage=STAGE_STRUCTURING,
        table_prefix=DB_TABLE_PREFIX,
        next_stage=get_testing_next_stage(),
        testing=True,
    )


@lru_cache
def get_testing_relay() -> OutboxRelay | None:
    return build_outbox_relay(get_settings(), get_testing_pipeline())


@lru_cache
def get_testing_results() -> StageResults | None:
    return build_stage_results(get_settings(), testing=True)


# --- services (cheap to build: one per request) ------------------------------------------


def get_structuring_service() -> StructuringService:
    return StructuringService(get_structurer())


def get_job_service() -> StructuringJobService:
    return StructuringJobService(
        get_pipeline(),
        get_structuring_service(),
        results=get_results(),
        handoff_by_reference=get_settings().pipeline_handoff_by_reference,
    )


def get_testing_job_service() -> StructuringJobService:
    return StructuringJobService(
        get_testing_pipeline(),
        get_structuring_service(),
        results=get_testing_results(),
        handoff_by_reference=get_settings().pipeline_handoff_by_reference,
    )


@lru_cache
def get_reaper() -> StaleJobReaper | None:
    return build_stale_job_reaper(get_settings(), get_pipeline(), get_job_service().resume)


@lru_cache
def get_testing_reaper() -> StaleJobReaper | None:
    return build_stale_job_reaper(get_settings(), get_testing_pipeline(), get_testing_job_service().resume)

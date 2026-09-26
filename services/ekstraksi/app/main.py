from contextlib import asynccontextmanager

from fastapi import FastAPI

from ocr_common.pipeline.database import check_connection, dispose_engines
from ocr_common.pipeline.schemas import StageCallback
from ocr_common.web.app import add_stage_callback_webhook, create_app, database_readiness

from app.api import jobs, testing
from app.config import get_settings
from app.dependencies import (
    get_next_stage,
    get_ocr_engine,
    get_pipeline,
    get_reaper,
    get_relay,
    get_testing_next_stage,
    get_testing_pipeline,
    get_testing_reaper,
    get_testing_relay,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.database_url:
        await check_connection(settings.database_url)
    # Built here rather than on the first request: an in-process backend loads weights, and a
    # missing runtime should stop the process instead of failing one job at a time.
    ocr_engine = get_ocr_engine()
    pipeline = get_pipeline()
    next_stage = get_next_stage()
    relay = get_relay()
    if relay is not None:
        relay.start()
    reaper = get_reaper()
    if reaper is not None:
        reaper.start()
    testing_relay = testing_reaper = None
    if settings.testing_endpoints:
        testing_relay, testing_reaper = get_testing_relay(), get_testing_reaper()
        if testing_relay is not None:
            testing_relay.start()
        if testing_reaper is not None:
            testing_reaper.start()
    yield
    if reaper is not None:
        await reaper.stop()
    await pipeline.aclose(settings.pipeline_drain_timeout_seconds, relay=relay)
    await next_stage.aclose()
    if settings.testing_endpoints:
        if testing_reaper is not None:
            await testing_reaper.stop()
        await get_testing_pipeline().aclose(settings.pipeline_drain_timeout_seconds, relay=testing_relay)
        await get_testing_next_stage().aclose()
    close = getattr(ocr_engine, "aclose", None)  # only the HTTP-backed model holds a connection
    if close is not None:
        await close()
    await dispose_engines()


app = create_app(
    settings=settings,
    title="OCR Kartu Keluarga Ekstraksi API",
    service_name="ekstraksi",
    description=(
        "Pipeline step 2 for Indonesian Kartu Keluarga: reads the card photo and produces one "
        "`{text, score, poly}` box per recognised line, plus the three document aggregates the trust model "
        "uses. It does not name fields and it does not judge the document.\n\n"
        "**Async only.** The orchestrator POSTs /v1/ekstraksi/jobs once the guardrails model passed the "
        "document and gets 202; this service reads the image in the background, stores the result "
        "(`ocr_results`) and hands the job to the structuring service in the same transaction. There is no "
        "synchronous OCR endpoint. All endpoints except /health require an X-API-Key header."
    ),
    tags=[
        {"name": "Pipeline", "description": "Asynchronous pipeline stage: 202, background work, callback, hand-off"},
        {"name": "Callbacks", "description": "Requests this service SENDS to the orchestrator (see Webhooks)"},
    ],
    routers=[jobs.router, *([testing.router] if settings.testing_endpoints else [])],
    backends={
        "ekstraksi": settings.ekstraksi_backend,
        "storage": "postgres" if settings.database_url else "memory",
    },
    readiness=database_readiness(settings.database_url),
    backends_example={"ekstraksi": "kk_ocr", "storage": "postgres"},
    readiness_example={"database": "ok"},
    lifespan=lifespan,
)

add_stage_callback_webhook(
    app,
    body_model=StageCallback,
    sent=(
        "Once per job of `POST /v1/ekstraksi/jobs`: `stage: OCR` with `status: DONE` once the OCR result is "
        "stored, or `status: FAILED` with `error_message` when the document could not be read (the chain stops "
        "there). An image with no readable text is `DONE`, not `FAILED`: it is rejected by the structuring "
        "rules. Additionally `stage: STRUCTURING`, `status: FAILED` when OCR succeeded but the structuring "
        "service could not be reached after retries. Without `PIPELINE_OUTBOX` the `OCR` callback is sent "
        "before the hand-off to structuring; with it the hand-off goes first, so the `STRUCTURING` callback "
        "may arrive before this one."
    ),
)

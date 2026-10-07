from contextlib import asynccontextmanager

from fastapi import FastAPI

from ocr_common.web.app import create_app

from app.api import guardrails
from app.config import get_settings
from app.dependencies import get_quality_model

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = get_quality_model()
    yield
    close = getattr(model, "aclose", None)  # only the HTTP-backed models hold a connection
    if close is not None:
        await close()


app = create_app(
    settings=settings,
    title="OCR Kartu Keluarga Guardrails API",
    service_name="guardrails",
    description=(
        "The image-quality judge of the nlm-k2 Kartu Keluarga pipeline, internal: the orchestrator calls "
        "`POST /v1/guardrails/check` for every document it receives, and hands one that passes to the OCR "
        "stage. A Kartu Keluarga is a single image, so the check produces one verdict -- `accepted`, "
        "`reject`, or `unassessable` when the image could not be judged at all -- and never an HTTP error "
        "for the document itself. The model runs in this process (`kk_quality`: patch blur CNN + XGBoost + "
        "isotonic calibration) or in the ML team's quality service (`remote`). This is the only image-quality "
        "gate in the pipeline, and only the request's `threshold` refuses a bad photo: without one the "
        "document passes and `probability_bad` is answered. All "
        "endpoints except /health, /ready and /metrics require an X-API-Key header."
    ),
    tags=[
        {"name": "Guardrails", "description": "The guardrails check (internal: called by the orchestrator)"},
    ],
    routers=[guardrails.router],
    backends={"guardrails": settings.guardrails_backend},
    # No database and nothing to warm: /ready is always 200 for this service.
    readiness={},
    backends_example={"guardrails": "kk_quality"},
    lifespan=lifespan,
)

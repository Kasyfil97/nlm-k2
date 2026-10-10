from typing import Any

import pytest

from ocr_common.kk import DOC_FIELDS, MEMBER_FIELDS, SCORED_DOC_FIELDS, SCORED_MEMBER_FIELDS, contract_fields
from ocr_common.synthetic_kk import household, nomor_kk
from ocr_common.testing import auth_headers, make_client, set_test_env

# RATE_LIMIT_REQUESTS: the whole suite shares one API key, so every request lands in the same bucket
# and the production default (60/minute) would turn test number 61 into a 429. The limiter itself is
# tested in test_rate_limit.py against its own small app, which is also the only place where the
# number under test is the number that matters.
set_test_env(AUTH_DISABLED="false", RATE_LIMIT_REQUESTS="100000")

from app.config import get_settings  # noqa: E402
from app.dependencies import (  # noqa: E402
    get_extraction_client,
    get_final_results,
    get_guardrails_client,
    get_guardrails_log,
    get_pipeline_waiter,
    get_testing_final_results,
    get_testing_guardrails_log,
)
from app.main import app  # noqa: E402
from app.services.pipeline_waiter import WaitOutcome  # noqa: E402

# Only the content type and the size of a document are checked here; the bytes never reach a model.
JPEG = b"\xff\xd8fake-jpeg-bytes"

# A Kartu Keluarga is one image, so the report has no page list and no counts across pages.
ACCEPTED_REPORT: dict[str, Any] = {
    "passed": True,
    "reason": None,
    "document": {"verdict": "accepted", "confidence": 0.9821, "probability_bad": 0.0179, "threshold_used": 0.5},
}
REJECTED_REPORT: dict[str, Any] = {
    "passed": False,
    # Kalimat ini persis yang dikembalikan guardrails (`REASON_REJECT`, §3.5). Sebelumnya di sini
    # ada kalimat karangan yang tidak pernah diucapkan service mana pun -- tidak merusak apa-apa,
    # karena orchestrator hanya meneruskannya, tetapi menyesatkan pembaca berikutnya.
    "reason": "Kualitas gambar terlalu rendah, mohon unggah foto yang lebih jelas",
    "document": {"verdict": "reject", "confidence": 0.8821, "probability_bad": 0.8821, "threshold_used": 0.5},
}

# The stage payloads are BUILT from the same generators the stage mocks use, not typed out. Two
# members, because one member cannot expose an index shift and the second is where the projection
# stops being a rename and starts being a zip. Every score differs, for the same reason.
HOUSEHOLD = household(2)
NOMOR_KK = nomor_kk()

_DOC_VALUES = {
    "nomor_kk": NOMOR_KK,
    "nama_kepala_keluarga": HOUSEHOLD[0].nama_lengkap,
}


def structured(value: str, ocr_conf: float | None = 0.99, crf_conf: float | None = 0.97) -> dict:
    """One structured field: `value` is never null, and both scores are null without a value (§7.3)."""
    if not value:
        return {"value": "", "ocr_conf": None, "crf_conf": None}
    return {"value": value, "ocr_conf": ocr_conf, "crf_conf": crf_conf}


STRUCTURING_RESULT: dict[str, Any] = {
    **{
        name: structured(_DOC_VALUES[name], round(0.9991 - index * 0.0017, 4), round(0.9873 - index * 0.0021, 4))
        for index, name in enumerate(DOC_FIELDS)
    },
    "anggota_keluarga": [
        {
            name: structured(
                getattr(person, name), round(0.99 - position * 0.031, 4), round(0.97 - position * 0.043, 4)
            )
            for name in MEMBER_FIELDS
        }
        for position, person in enumerate(HOUSEHOLD)
    ],
    "reject_reason": None,
}

# Scoring returns the nine contract fields under their INTERNAL names. The numbers are spread so that
# member 2's `jenis_pekerjaan` lands below the 0.5 threshold: the fixture carries one confidence 0 on
# purpose, because a fixture where everything passes cannot tell a working threshold from a constant.
LOW_SCORED_FIELD = "jenis_pekerjaan"
SCORING_RESULT: dict[str, Any] = {
    "document_type": "kk",
    "fields": {name: round(0.94 - index * 0.031, 4) for index, name in enumerate(SCORED_DOC_FIELDS)},
    "anggota_keluarga": [
        {
            name: (
                0.4118
                if position == 1 and name == LOW_SCORED_FIELD
                else round(0.93 - position * 0.05 - index * 0.011, 4)
            )
            for index, name in enumerate(SCORED_MEMBER_FIELDS)
        }
        for position in range(len(HOUSEHOLD))
    ],
    "model": "kk-trust-mock-v1",
}
OCR_RESULT: dict[str, Any] = {
    "texts": [{"text": NOMOR_KK, "score": 0.9991, "poly": [[291.0, 7.0], [306.0, 6.0], [309.0, 26.0], [294.0, 28.0]]}],
    "model": "mock",
    "text_regions_count": 1,
    "avg_doc_score": 0.9991,
    "min_doc_score": 0.9991,
}
# What the endpoint must answer for the fixtures above, through the one function the scoring stage
# also calls (§8.5): without column_confidence_threshold, the probabilities themselves. Pinned against
# literal values in test_extract_contract.py.
EXPECTED_DATA = contract_fields(STRUCTURING_RESULT, SCORING_RESULT)
DONE = WaitOutcome(
    "SCORING",
    "DONE",
    results={"OCR": OCR_RESULT, "STRUCTURING": STRUCTURING_RESULT, "SCORING": SCORING_RESULT},
)


class StubGuardrails:
    """The guardrails service, answering like its mock model: a file name containing `blur`, `invalid` or
    `notkk` is rejected, anything else accepted. Set `error` to make it fail instead."""

    def __init__(self) -> None:
        self.checked: list[dict] = []
        self.error: Exception | None = None
        self.thresholds: list = []

    async def check(self, request_id, filename, content_type, content, threshold=None) -> dict:
        self.checked.append({"request_id": request_id, "filename": filename, "content_type": content_type})
        self.thresholds.append(threshold)
        if self.error is not None:
            raise self.error
        rejected = any(word in filename for word in ("blur", "invalid", "notkk"))
        return REJECTED_REPORT if rejected else ACCEPTED_REPORT

    async def aclose(self) -> None:
        pass


class StubExtraction:
    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self.error: Exception | None = None

    async def submit(
        self,
        request_id,
        document_type,
        guardrails,
        filename,
        content_type,
        content,
        *,
        file_url=None,
        sequence=None,
        column_thresholds=None,
    ) -> dict:
        if self.error is not None:
            raise self.error
        self.submitted.append(
            {
                "request_id": request_id,
                "document_type": document_type,
                "guardrails": guardrails,
                "file_url": file_url,
                "sequence": list(sequence) if sequence is not None else None,
                "column_thresholds": column_thresholds,
            }
        )
        return {"request_id": request_id, "stage": "OCR", "status": "PROCESSING", "duplicate": False}

    async def aclose(self) -> None:
        pass


class RecordingGuardrailsLog:
    """Keeps the verdicts the service asks to record, instead of writing guardrails_results."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    async def record(self, request_id, report, *, threshold_from_request=False, n_pages=None, sequence=None) -> None:
        self.records.append(
            {
                "request_id": request_id,
                "report": report,
                "threshold_from_request": threshold_from_request,
                "n_pages": n_pages,
                "sequence": list(sequence) if sequence else None,
            }
        )

    async def latest(self, request_id):
        mine = [record for record in self.records if record["request_id"] == request_id]
        return {"report": mine[-1]["report"], "sequence": mine[-1]["sequence"]} if mine else None


class RecordingFinalResults:
    """Keeps the answers the service asks to save, instead of inserting them in ocr_results, in order."""

    def __init__(self) -> None:
        self.saved: dict[str, list[dict]] = {}

    async def save(self, request_id, body) -> None:
        self.saved.setdefault(request_id, []).append(body)


class StubWaiter:
    def __init__(self) -> None:
        self.outcome = DONE
        self.snapshot_outcome: WaitOutcome | None = DONE
        self.snapshot_error: Exception | None = None
        self.calls: list[tuple[str, float]] = []
        self.last_stages: list[str | None] = []
        self.snapshots: list[str] = []

    async def wait(self, request_id: str, timeout: float, *, last_stage: str | None = None) -> WaitOutcome:
        self.calls.append((request_id, timeout))
        self.last_stages.append(last_stage)
        return self.outcome

    async def snapshot(self, request_id: str) -> WaitOutcome | None:
        self.snapshots.append(request_id)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.snapshot_outcome


@pytest.fixture(scope="session")
def client():
    return make_client(app)


@pytest.fixture
def auth() -> dict[str, str]:
    return auth_headers()


@pytest.fixture(autouse=True)
def stub_guardrails():
    stub = StubGuardrails()
    app.dependency_overrides[get_guardrails_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_guardrails_client, None)


@pytest.fixture(autouse=True)
def stub_extraction():
    stub = StubExtraction()
    app.dependency_overrides[get_extraction_client] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_extraction_client, None)


@pytest.fixture(autouse=True)
def guardrails_log():
    log = RecordingGuardrailsLog()
    app.dependency_overrides[get_guardrails_log] = lambda: log
    app.dependency_overrides[get_testing_guardrails_log] = lambda: log
    yield log
    app.dependency_overrides.pop(get_guardrails_log, None)
    app.dependency_overrides.pop(get_testing_guardrails_log, None)


@pytest.fixture(autouse=True)
def final_results():
    results = RecordingFinalResults()
    app.dependency_overrides[get_final_results] = lambda: results
    app.dependency_overrides[get_testing_final_results] = lambda: results
    yield results
    app.dependency_overrides.pop(get_final_results, None)
    app.dependency_overrides.pop(get_testing_final_results, None)


@pytest.fixture(autouse=True)
def stub_waiter():
    stub = StubWaiter()
    app.dependency_overrides[get_pipeline_waiter] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_pipeline_waiter, None)


@pytest.fixture
def settings_override():
    """Change settings for one test. The app reads them through a dependency, so this is an
    override rather than an env variable -- the module was imported once, at collection."""

    def install(**update):
        app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update=update)

    yield install
    app.dependency_overrides.pop(get_settings, None)

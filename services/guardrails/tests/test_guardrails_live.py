"""R20a: the real backends, run only where they can be.

Neither of these is a completion requirement for this unit -- the batch finishes on `mock` -- but
the mock is built from the same contract as the assertions, so it cannot tell anyone whether a real
model answers in the contract's shape. These can, the moment the weights or the model service
exist. Both skip silently otherwise.

Run the in-process one by putting the six K2Quality artifacts in a directory and pointing
`GUARDRAILS_WEIGHTS_DIR` at it; run the remote one with `GUARDRAILS_MODEL_URL`.
"""

import io
import os

import pytest
from PIL import Image, ImageDraw

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.synthetic_kk import household, nomor_kk

from app.config import Settings
from app.ml.kk_quality import REQUIRED_ARTIFACTS
from app.ml.remote import RemoteGuardrailsModel
from app.services.guardrails_service import GuardrailsService

MODEL_URL = os.environ.get("GUARDRAILS_MODEL_URL")
WEIGHTS_DIR = os.environ.get("GUARDRAILS_WEIGHTS_DIR", "weights")
VERDICTS = {"accepted", "reject", "unassessable"}


def synthetic_kk_jpeg(width: int = 1200, height: int = 760) -> bytes:
    """A card-shaped image with synthetic Kartu Keluarga text on it.

    Every value comes from `ocr_common.synthetic_kk` (province `99`): a real NIK must never be
    typed into a fixture, and a plausible one typed by hand can collide with a living person.
    """
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    person = household(1)[0]
    lines = [
        "KARTU KELUARGA",
        f"No. {nomor_kk()}",
        f"{person.nama_lengkap}   {person.nik}",
        f"{person.tempat_lahir}, {person.tanggal_lahir}",
    ]
    for index, text in enumerate(lines):
        draw.text((40, 40 + index * 70), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _weights_ready() -> bool:
    from pathlib import Path

    base = Path(WEIGHTS_DIR)
    return all((base / name).is_file() for name in REQUIRED_ARTIFACTS)


# --- the in-process backend --------------------------------------------------------------------

requires_weights = pytest.mark.skipif(
    not _weights_ready(), reason=f"artefak K2Quality tidak lengkap di {WEIGHTS_DIR}; uji backend asli dilewati"
)


@pytest.fixture(scope="module")
def kk_model():
    pytest.importorskip("torch", reason="torch tidak terpasang di lingkungan ini")
    pytest.importorskip("cv2", reason="opencv tidak terpasang di lingkungan ini")
    pytest.importorskip("xgboost", reason="xgboost tidak terpasang di lingkungan ini")
    from app.ml.kk_quality import KKQualityModel

    return KKQualityModel(WEIGHTS_DIR)


@requires_weights
async def test_the_real_model_answers_in_contract_shape(kk_model):
    report = await GuardrailsService(kk_model, Settings(api_key="x", _env_file=None)).check(
        "kk.jpg", "image/jpeg", synthetic_kk_jpeg()
    )
    document = report["document"]
    assert document["verdict"] in VERDICTS
    assert report["passed"] is (document["verdict"] == "accepted")
    assert 0 <= document["probability_bad"] <= 1
    assert document["confidence"] == pytest.approx(
        document["probability_bad"] if document["verdict"] == "reject" else 1 - document["probability_bad"], abs=1e-4
    )


@requires_weights
async def test_the_real_model_calls_an_undecodable_file_unassessable(kk_model):
    """The R14a path through the real core, where the twenty-odd raise sites actually live."""
    report = await GuardrailsService(kk_model, Settings(api_key="x", _env_file=None)).check(
        "kk.jpg", "image/jpeg", b"\xff\xd8\xff" + b"not an image" * 100
    )
    assert report["document"]["verdict"] == "unassessable"
    assert report["document"]["probability_bad"] is None


@requires_weights
def test_the_feature_vector_is_built_in_the_order_the_artifact_gives(kk_model):
    """Not a fixed list written here: a permuted vector scores a plausible number for an unrelated
    computation, and nothing downstream can tell."""
    assert kk_model.feature_names[0] == "laplacian_var"
    assert len(kk_model.feature_names) == kk_model.metadata["n_features"]


# --- the remote backend -------------------------------------------------------------------------

requires_model_service = pytest.mark.skipif(
    not MODEL_URL, reason="GUARDRAILS_MODEL_URL tidak di-set; uji live dilewati"
)


@pytest.fixture
async def remote_model():
    assert MODEL_URL
    api_key = os.environ.get("GUARDRAILS_MODEL_API_KEY")
    headers = {"X-API-Key": api_key} if api_key else None
    instance = RemoteGuardrailsModel(RemoteModelClient(MODEL_URL, 60.0, name="guardrails model", headers=headers))
    yield instance
    await instance.aclose()


@requires_model_service
async def test_live_model_answers_in_contract_shape(remote_model):
    report = await GuardrailsService(remote_model, Settings(api_key="x", _env_file=None)).check(
        "kk.jpg", "image/jpeg", synthetic_kk_jpeg()
    )
    document = report["document"]
    assert document["verdict"] in VERDICTS
    assert report["passed"] is (document["verdict"] == "accepted")
    assert (document["probability_bad"] is None) is (document["verdict"] == "unassessable")

"""The parts of the ported K2Quality core that need no scientific stack.

Worth having separately from `test_guardrails_live.py`: these run everywhere, and they cover the
gate that decides `unassessable` before a single pixel is decoded -- which is where most of the
twenty-odd raise sites in the original actually are. The feature extraction and the booster need
torch, cv2, scipy and xgboost and are exercised in `test_guardrails_live.py` when the artifacts and
the wheels are both present.
"""

import io

import pytest
from PIL import Image

from app.ml.base import UnassessableImage
from app.ml.kk_quality import (
    BLUR_CNN_META,
    MAX_DIMENSION,
    MAX_IMAGE_BYTES,
    MIN_DIMENSION,
    REQUIRED_ARTIFACTS,
    KKQualityModel,
    _check_declared_dimensions,
    _optional_threshold,
    _validate_bytes,
    render_pdf_first_page,
)


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


# --- the artifacts ------------------------------------------------------------------------------


def test_the_six_artifacts_are_the_ones_the_migration_script_produces():
    assert REQUIRED_ARTIFACTS == (
        "blur_cnn_weights.pt",
        "blur_cnn_meta.json",
        "xgb_model.json",
        "calibration.json",
        "feature_names.json",
    )


def test_a_missing_artifact_refuses_to_build_and_names_what_is_missing(tmp_path):
    """The file this replaced needed one file; a half-populated directory is the new failure mode,
    and 'model not found' would send someone looking for the wrong thing."""
    (tmp_path / "blur_cnn_weights.pt").write_bytes(b"")
    with pytest.raises(RuntimeError) as exc:
        KKQualityModel(str(tmp_path))
    message = str(exc.value)
    assert "GUARDRAILS_WEIGHTS_DIR" in message
    assert "xgb_model.json" in message
    assert "blur_cnn_weights.pt" not in message.split("missing")[1], "the one that IS there is not listed as missing"


# --- R14a: what is refused before anything is decoded ---------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not an image at all",
        b"GIF89a" + b"\x00" * 64,  # a real image format, but not one the model was trained on
        b"<html><body>404 Not Found</body></html>",  # what a misconfigured presigned URL returns
    ],
)
def test_content_that_is_not_jpeg_png_or_pdf_cannot_be_assessed(content):
    with pytest.raises(UnassessableImage):
        _validate_bytes(content)


@pytest.mark.parametrize("magic", [b"\xff\xd8\xff", b"\x89PNG", b"%PDF"])
def test_the_three_accepted_signatures_pass_the_gate(magic):
    _validate_bytes(magic + b"\x00" * 128)


def test_a_file_above_the_ceiling_cannot_be_assessed():
    with pytest.raises(UnassessableImage, match="too large"):
        _validate_bytes(b"\xff\xd8\xff" + b"\x00" * MAX_IMAGE_BYTES)


def test_a_header_declaring_more_than_the_maximum_side_is_refused_before_decoding():
    """The point of reading the header first: a small file can declare a huge raster, and the
    refusal has to happen before anything allocates it."""
    with pytest.raises(UnassessableImage, match="too large"):
        _check_declared_dimensions(_png(MAX_DIMENSION + 2000, 100))


def test_a_normal_card_sized_header_passes():
    _check_declared_dimensions(_png(1200, 760))


def test_an_unparseable_header_is_left_to_the_decoder():
    """Deliberate: reporting this as a size problem would mask a genuine decode failure, and the
    decoder reports the same verdict with the right reason a moment later."""
    _check_declared_dimensions(b"\xff\xd8\xffdefinitely not a jpeg")


def test_the_minimum_side_is_smaller_than_one_cnn_patch():
    """A sanity bound on the two constants together: below MIN_DIMENSION nothing is assessed at
    all, and between it and a patch the CNN features fall back to NaN rather than failing."""
    assert 0 < MIN_DIMENSION < MAX_DIMENSION


# --- R15 rung 4: the threshold stored with the weights ---------------------------------------------


def test_the_stored_threshold_is_read_from_the_metadata():
    assert _optional_threshold({"decision_threshold": 0.62}) == 0.62
    assert _optional_threshold({"threshold": 0.85}) == 0.85
    assert _optional_threshold({"decision_threshold": 0.62, "threshold": 0.85}) == 0.62


def test_todays_export_has_no_stored_threshold_so_the_rung_is_absent():
    """The real `blur_cnn_meta.json` carries aggregation_type, use_v2_arch, best_epoch,
    best_val_auc and source_checkpoint -- and no threshold: K2Quality keeps its operating point in
    config.yaml. `None` is the honest answer, and the chain then falls to 0.5."""
    real_meta = {
        "aggregation_type": "attention",
        "use_v2_arch": False,
        "best_epoch": 16,
        "best_val_auc": 0.0,
        "source_checkpoint": "internal",
    }
    assert _optional_threshold(real_meta) is None
    assert BLUR_CNN_META == "blur_cnn_meta.json"


@pytest.mark.parametrize("value", [0, 1, 1.5, -0.2, True, "0.6", None])
def test_an_unusable_stored_threshold_is_ignored_rather_than_honoured(value):
    """0 would reject every card and 1 almost none, so a bad key must not be able to disable the
    gate silently -- it drops to the rung below instead."""
    assert _optional_threshold({"decision_threshold": value}) is None


# --- PDF: page 1 only ------------------------------------------------------------------------------


def _pdf(sizes: list[tuple[int, int]]) -> bytes:
    import fitz  # ty: ignore[unresolved-import]

    document = fitz.open()
    for width, height in sizes:
        document.new_page(width=width, height=height)
    return document.tobytes()


def test_a_pdf_is_judged_by_its_first_page_only():
    """Page 1 at the training DPI (150): a 288x144 pt first page becomes 600x300 px, whatever the
    second page looks like -- the same page ekstraksi reads."""
    png = render_pdf_first_page(_pdf([(288, 144), (600, 800)]))
    assert Image.open(io.BytesIO(png)).size == (600, 300)


def test_a_pdf_that_cannot_be_rendered_is_unassessable_not_a_crash():
    with pytest.raises(UnassessableImage):
        render_pdf_first_page(b"%PDF-1.4 garbage")

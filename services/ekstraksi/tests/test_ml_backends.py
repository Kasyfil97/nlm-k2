"""The three backends and the conversion they share, without going through HTTP.

`app/ml/utils.py` is the one place `{text, score, poly}` is produced, so it carries the cases that
would otherwise be found one service downstream: a polygon that is not four points, a score just
outside [0, 1], a line that recognised as whitespace.
"""

import pytest

from ocr_common.errors import InternalError

from app.ml.kk_ocr import MODEL_NAME, KkOcrConfig, KkOcrEngine
from app.ml.mock import BLANK_TRIGGER, ERROR_TRIGGER, MockOcrEngine
from app.ml.remote import RemoteOcrEngine, parse_model, parse_pages
from app.ml.utils import POLY_POINTS, boxes_from_rec_lists, model_name, poly_of, score_of

QUAD = [[10.0, 4.0], [90.0, 6.0], [90.0, 30.0], [10.0, 28.0]]


# --- the shared conversion ------------------------------------------------------------------


def test_a_polygon_of_four_points_becomes_floats():
    assert poly_of([[10, 4], [90, 6], [90, 30], [10, 28]]) == QUAD


def test_a_polygon_that_is_not_four_points_is_refused():
    """`OcrBoxPayload` pins the length at exactly four, so a three-point polygon passed on would
    turn this stage's success into a 422 at structuring. Dropping the box is the visible failure."""
    assert poly_of([[0, 0], [1, 0], [1, 1]]) is None
    assert poly_of([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]) is None
    assert poly_of(None) is None
    assert poly_of([[0], [1], [2], [3]]) is None


def test_scores_are_clamped_and_rounded():
    """float32 accumulation really does produce 1.0000001, and `OcrBoxPayload` bounds the field."""
    assert score_of(1.0000001) == 1.0
    assert score_of(-0.2) == 0.0
    assert score_of(0.81403) == 0.814
    assert score_of(None) == 0.0


def test_empty_and_malformed_boxes_are_dropped_not_passed_on():
    boxes = boxes_from_rec_lists(
        ["KARTU KELUARGA", "   ", "DROPPED"],
        [0.99, 0.5, 0.4],
        [QUAD, QUAD, [[0, 0], [1, 1]]],
    )
    assert [box["text"] for box in boxes] == ["KARTU KELUARGA"]


def test_mismatched_rec_lists_produce_nothing_rather_than_a_misaligned_box():
    """An off-by-one between the three lists would pair a text with another line's polygon, which
    reads as a plausible result and is the worst possible failure mode here."""
    assert boxes_from_rec_lists(["A", "B"], [0.9], [QUAD, QUAD]) == []


def test_the_model_name_joins_detector_and_recogniser():
    assert model_name({"detection": "det_v5", "recognition": "rec_v5"}) == "det_v5+rec_v5"
    assert model_name({"pipeline": "PP-OCRv5"}) == "PP-OCRv5"
    assert model_name(None) is None


# --- mock ------------------------------------------------------------------------------------


def test_the_mock_reads_a_synthetic_card_with_tilted_polygons():
    result = MockOcrEngine().read("kk.jpg", b"bytes-of-a-card")
    assert result["model"] == "mock-kk-v1"
    assert len(result["texts"]) > 5
    assert all(len(box["poly"]) == POLY_POINTS for box in result["texts"])
    assert any(box["poly"][0][1] != box["poly"][1][1] for box in result["texts"])


def test_the_mock_never_emits_a_real_province_code():
    """Province `99` is unassigned, which is the whole defence: a sixteen-digit number that looks
    like a NIK to a parser but cannot belong to anyone."""
    texts = " ".join(box["text"] for box in MockOcrEngine().read("kk.jpg", b"bytes")["texts"])
    digits = [word for word in texts.replace(".", " ").split() if word.isdigit() and len(word) == 16]
    assert digits, "the card does carry sixteen-digit numbers"
    assert all(number.startswith("99") for number in digits)


def test_the_mock_is_deterministic_in_the_bytes_and_not_in_the_name():
    first = MockOcrEngine().read("a.jpg", b"same-bytes")["texts"]
    second = MockOcrEngine().read("b.jpg", b"same-bytes")["texts"]
    other = MockOcrEngine().read("a.jpg", b"other-bytes")["texts"]
    assert first == second
    assert [box["text"] for box in first] != [box["text"] for box in other]


def test_the_blank_trigger_is_an_empty_read_and_the_error_trigger_raises():
    assert MockOcrEngine().read(f"{BLANK_TRIGGER}.jpg", b"bytes") == {"texts": [], "model": None}
    with pytest.raises(InternalError):
        MockOcrEngine().read(f"{ERROR_TRIGGER}.jpg", b"bytes")


def test_the_mock_triggers_do_not_collide_with_the_guardrails_ones():
    """A document the guardrails model refuses never reaches this stage, so a trigger word shared
    with `blur` / `invalid` / `notkk` would be unreachable from the front door."""
    assert BLANK_TRIGGER not in ("blur", "invalid", "notkk")
    for word in ("blur", "invalid", "notkk"):
        assert word not in BLANK_TRIGGER and BLANK_TRIGGER not in word


def test_the_mock_is_synchronous_so_it_goes_through_the_threadpool():
    """The in-process contract (`OcrRecognizer`) is synchronous on purpose: there is no `await` to
    reach for, so the off-loop hop in `EkstraksiService` cannot be skipped by accident."""
    import inspect

    assert not inspect.iscoroutinefunction(MockOcrEngine.read)
    assert not hasattr(MockOcrEngine, "extract"), "the duck-typed branch picks the remote path on `extract`"


# --- remote ------------------------------------------------------------------------------------


def _page(texts, scores, polys, index=0):
    return {"page_index": index, "rec_texts": texts, "rec_scores": scores, "rec_polys": polys}


def test_the_remote_pages_are_flattened_into_one_texts_list():
    """A Kartu Keluarga is one image and §7.1 has no page concept, so pages concatenate in order."""
    body = {
        "models": {"detection": "det", "recognition": "rec"},
        "pages": [_page(["A"], [0.9], [QUAD]), _page(["B"], [0.8], [QUAD], 1)],
    }

    boxes = parse_pages(body, "ekstraksi OCR model")
    assert [box["text"] for box in boxes] == ["A", "B"]
    assert parse_model(body) == "det+rec"


def test_the_older_remote_shapes_are_still_accepted():
    assert [box["text"] for box in parse_pages([_page(["A"], [0.9], [QUAD])], "model")] == ["A"]
    assert [box["text"] for box in parse_pages({"data": [_page(["A"], [0.9], [QUAD])]}, "model")] == ["A"]


def test_an_unexpected_remote_shape_is_an_internal_error():
    for body in (
        {"pages": "nope"},
        [42],
        {"pages": [{"rec_texts": ["A"]}]},
        {"pages": [_page(["A", "B"], [0.9], [QUAD])]},
    ):
        with pytest.raises(InternalError):
            parse_pages(body, "ekstraksi OCR model")


def test_the_remote_backend_is_the_async_one():
    import inspect

    assert inspect.iscoroutinefunction(RemoteOcrEngine.extract)
    assert not hasattr(RemoteOcrEngine, "read")


# --- kk_ocr (the in-process backend; no weights needed for these) -------------------------------


class FakePredictor:
    """K2Extractor's `_to_paddle_result` shape: one dict with three parallel lists."""

    def __init__(self, page: dict):
        self.page = page
        self.calls: list[object] = []

    def predict(self, image_rgb):
        self.calls.append(image_rgb)
        return [self.page]


def _engine(page: dict) -> tuple[KkOcrEngine, FakePredictor]:
    predictor = FakePredictor(page)
    config = KkOcrConfig(
        ppocr_root=__import__("pathlib").Path("/nonexistent"),
        det_weights=__import__("pathlib").Path("/nonexistent/det.pth"),
        rec_weights=__import__("pathlib").Path("/nonexistent/rec.pth"),
    )
    return KkOcrEngine(config, predictor=predictor), predictor


def _jpeg() -> bytes:
    """A real 40x20 JPEG. `numpy` is skipped rather than vendored: it is a dependency of the real
    backend's runtime, which the dev environment does not carry while the weights do not exist
    either (R20a). Everything that does not need a *successful* decode is tested unconditionally."""
    pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), (255, 255, 255)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_kk_ocr_converts_the_torch_backend_shape_into_71_boxes():
    engine, _ = _engine({"rec_texts": ["KARTU KELUARGA"], "rec_scores": [0.9999], "rec_polys": [QUAD]})

    result = engine.read("kk.jpg", _jpeg(), "image/jpeg")
    assert result == {"texts": [{"text": "KARTU KELUARGA", "score": 0.9999, "poly": QUAD}], "model": MODEL_NAME}


def test_kk_ocr_reports_an_image_with_no_boxes_as_an_empty_read():
    """The detector finding nothing is a valid answer, not an error: §6.3 sends it on to be
    rejected by the structuring rules."""
    engine, _ = _engine({"rec_texts": [], "rec_scores": [], "rec_polys": []})

    assert engine.read("kk.jpg", _jpeg())["texts"] == []


def test_kk_ocr_hands_the_predictor_an_rgb_array():
    engine, predictor = _engine({"rec_texts": [], "rec_scores": [], "rec_polys": []})

    engine.read("kk.jpg", _jpeg())
    [image] = predictor.calls
    assert image.shape == (20, 40, 3)  # ty: ignore[unresolved-attribute]


def test_kk_ocr_turns_an_undecodable_upload_into_a_failed_job_not_a_crash():
    """§6.3: an image that cannot be opened is a `FAILED` job and a 422, with a message a caller
    can act on -- not an unhandled exception logged as `crashed`."""
    engine, _ = _engine({"rec_texts": [], "rec_scores": [], "rec_polys": []})

    with pytest.raises(InternalError, match="could not be decoded"):
        engine.read("kk.jpg", b"not an image at all")


def test_kk_ocr_says_which_file_is_missing_rather_than_failing_deep_in_torch():
    """Building the predictor is a start-up step, so the message has to name the thing to fetch."""
    from app.ml.kk_ocr import build_predictor

    config = KkOcrConfig(
        ppocr_root=__import__("pathlib").Path("/nonexistent/ppocr"),
        det_weights=__import__("pathlib").Path("/nonexistent/det.pth"),
        rec_weights=__import__("pathlib").Path("/nonexistent/rec.pth"),
    )
    with pytest.raises(InternalError, match="nonexistent"):
        build_predictor(config)

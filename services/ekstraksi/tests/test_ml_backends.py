"""The three backends and the conversion they share, without going through HTTP.

`app/ml/utils.py` is the one place `{text, score, poly}` is produced, so it carries the cases that
would otherwise be found one service downstream: a polygon that is not four points, a score just
outside [0, 1], a line that recognised as whitespace.
"""

from typing import Any

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


def test_only_the_first_page_is_read():
    """A Kartu Keluarga is one sheet and the layout parser reads one page frame: page 2 of a PDF
    would land on page 1's coordinates, so it is dropped rather than concatenated."""
    body = {
        "models": {"detection": "det", "recognition": "rec"},
        "pages": [_page(["A"], [0.9], [QUAD]), _page(["B"], [0.8], [QUAD], 1)],
    }

    boxes = parse_pages(body, "ekstraksi OCR model")
    assert [box["text"] for box in boxes] == ["A"]
    assert parse_model(body) == "det+rec"


def test_a_body_with_no_pages_is_an_empty_read():
    assert parse_pages({"pages": []}, "ekstraksi OCR model") == []


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


def _pdf(sizes: list[tuple[int, int]]) -> bytes:
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    for width, height in sizes:
        document.new_page(width=width, height=height)
    return document.tobytes()


def test_kk_ocr_reads_page_one_of_a_pdf_at_the_configured_dpi():
    """As in nilam a PDF is accepted; here only page 1 is read, because the layout parser reads one
    page frame. 144x72 pt at the default 200 DPI is 400x200 px, whatever page 2 is."""
    pytest.importorskip("numpy")
    engine, predictor = _engine({"rec_texts": [], "rec_scores": [], "rec_polys": []})

    engine.read("kk.pdf", _pdf([(144, 72), (600, 800)]), "application/pdf")
    [image] = predictor.calls
    assert image.shape == (200, 400, 3)  # ty: ignore[unresolved-attribute]


def test_kk_ocr_turns_an_unreadable_pdf_into_a_failed_job():
    pytest.importorskip("fitz")
    engine, _ = _engine({"rec_texts": [], "rec_scores": [], "rec_polys": []})

    with pytest.raises(InternalError, match="could not be decoded as a PDF"):
        engine.read("kk.pdf", b"%PDF-1.4 garbage", "application/pdf")


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


def test_a_lever_key_that_contains_a_trigger_word_does_not_fire_it():
    """`kk-MOCK:blank_kk=1.jpg` carries the structuring mock's lever for "the KK number is
    missing", and that key contains `blank`. Before the triggers were matched against the name
    with the levers removed, this file returned no text at all — so §7.4 rule 2 was unreachable
    from the orchestrator's front door, and unreachable *silently*, because rule 1 answers 400 too.

    The Unit 10 smoke test found it on its first real run against compose. This is the regression
    test, and the collision it guards is between the two MOCKS, which is a dimension the existing
    collision test (against the guardrails words) does not cover."""
    result = MockOcrEngine().read(f"kk-MOCK:{BLANK_TRIGGER}_kk=1.jpg", b"bytes")
    assert result["texts"], "the lever key swallowed the document"
    assert any(f"MOCK:{BLANK_TRIGGER}_kk=1" in (box["text"] or "") for box in result["texts"]), (
        "the lever must still reach structuring, which is the stage that reads it"
    )


def test_a_trigger_outside_a_lever_still_fires():
    assert MockOcrEngine().read(f"kk-{BLANK_TRIGGER}.jpg", b"bytes")["texts"] == []


# --- the PP-OCRv6 VM shape -----------------------------------------------------------------

#: A response captured from the VM the ML team hosts (`GET /health` reports PP-OCRv6_medium_det +
#: PP-OCRv6_medium_rec). Trimmed to two boxes; the numbers are verbatim, including the score with
#: more precision than §7.1 keeps and the integer polygon points.
VM_BODY: dict[str, Any] = {
    "models": {
        "detection": "PP-OCRv6_medium_det",
        "recognition": "PP-OCRv6_medium_rec",
        "pipeline": "PaddleOCR",
        "device": "gpu",
    },
    "num_pages": 1,
    "pages": [
        {
            "page_index": 0,
            "width": 1000,
            "height": 620,
            "texts": [
                {
                    "text": "KARTU KELUARGA",
                    "score": 0.9991727471351624,
                    "poly": [[38, 43], [353, 43], [353, 73], [38, 73]],
                },
                {
                    "text": "No.9924187486671285",
                    "score": 0.9999954700469971,
                    "poly": [[38, 110], [406, 110], [406, 142], [38, 142]],
                },
            ],
        }
    ],
    "filename": "kk.jpg",
}


def test_the_vm_shape_is_read_as_section_71_boxes():
    boxes = parse_pages(VM_BODY, "ekstraksi OCR model")
    assert boxes == [
        {"text": "KARTU KELUARGA", "score": 0.9992, "poly": [[38.0, 43.0], [353.0, 43.0], [353.0, 73.0], [38.0, 73.0]]},
        {
            "text": "No.9924187486671285",
            "score": 1.0,
            "poly": [[38.0, 110.0], [406.0, 110.0], [406.0, 142.0], [38.0, 142.0]],
        },
    ]


def test_the_vm_model_name_is_reported_as_detection_plus_recognition():
    assert parse_model(VM_BODY) == "PP-OCRv6_medium_det+PP-OCRv6_medium_rec"


def test_page_dimensions_are_ignored():
    """They must be. With `use_doc_orientation_classify=true` the VM returns `poly` in the
    orientation-corrected frame while still reporting the submitted dimensions, so a sideways photo
    comes back with polys that do not fit the page it names — measured: a 620x1000 submission
    answered `page=620x1000` with a poly bounding box reaching x=812. §7.1 carries no page
    dimensions, and this pins that the stage does not start carrying them."""
    sideways = {**VM_BODY, "pages": [{**VM_BODY["pages"][0], "width": 620, "height": 1000}]}
    assert parse_pages(sideways, "ekstraksi OCR model") == parse_pages(VM_BODY, "ekstraksi OCR model")


def test_a_malformed_box_in_the_vm_shape_is_dropped_not_passed_on():
    """Three polygon points would be a 422 at structuring. Losing the box is the smaller, visible
    failure, and `text_regions_count` records it."""
    page = {
        "texts": [
            {"text": "OK", "score": 0.9, "poly": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            {"text": "three points", "score": 0.9, "poly": [[0, 0], [1, 0], [1, 1]]},
            {"text": "   ", "score": 0.9, "poly": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        ]
    }
    boxes = parse_pages({"pages": [page]}, "ekstraksi OCR model")
    assert [box["text"] for box in boxes] == ["OK"]


def test_a_page_with_neither_shape_is_an_internal_error():
    with pytest.raises(InternalError):
        parse_pages({"pages": [{"page_index": 0}]}, "ekstraksi OCR model")


def test_booleans_go_on_the_wire_lowercase():
    """The VM parses these query parameters as strings; Python's `str(True)` is `"True"`, which it
    does not accept, so a silently ignored parameter would look like a model that does not correct
    orientation."""
    engine = RemoteOcrEngine(
        object(),  # ty: ignore[invalid-argument-type]
        path="/ocr",
        query={"use_doc_orientation_classify": True, "use_doc_unwarping": False, "split": 0},
    )
    assert engine._query == {
        "use_doc_orientation_classify": "true",
        "use_doc_unwarping": "false",
        "split": "0",
    }

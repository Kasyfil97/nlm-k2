"""The crop model's input and loading. The preprocessing needs only Pillow; loading and scoring need torch and
skip without it, and the score of the real checkpoint skips without `crop_model.pt` in the weights directory."""

import io
import os
from pathlib import Path

import pytest
from PIL import Image

from app.ml.crop import INPUT_SIZE, _model_input
from app.ml.kk_quality import CROP_MODEL

WEIGHTS_DIR = Path(os.environ.get("GUARDRAILS_WEIGHTS_DIR", "weights"))


def _jpeg(width: int, height: int, colour: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_the_input_is_letterboxed_not_cropped():
    """The edges of the photo are the signal: a wide card keeps its full width and gets black bars above and
    below, rather than being center-cropped to a square that would cut it."""
    square = _model_input(_jpeg(1600, 1000))
    assert square.size == (INPUT_SIZE, INPUT_SIZE)
    assert square.getpixel((INPUT_SIZE // 2, 2)) == (0, 0, 0)
    assert square.getpixel((INPUT_SIZE // 2, INPUT_SIZE // 2)) != (0, 0, 0)
    assert square.getpixel((1, INPUT_SIZE // 2)) != (0, 0, 0)


def test_a_checkpoint_of_another_architecture_is_refused(tmp_path):
    torch = pytest.importorskip("torch")
    from app.ml.crop import CropScorer

    path = tmp_path / CROP_MODEL
    torch.save({"arch": "efficientnet_b0", "input": INPUT_SIZE, "state_dict": {}}, path)
    with pytest.raises(RuntimeError, match="mobilenet_v3_small"):
        CropScorer(path, torch.device("cpu"))


@pytest.mark.skipif(not (WEIGHTS_DIR / CROP_MODEL).is_file(), reason=f"{CROP_MODEL} absent in {WEIGHTS_DIR}")
def test_the_real_checkpoint_scores_deterministically_in_range():
    torch = pytest.importorskip("torch")
    from app.ml.crop import CropScorer

    scorer = CropScorer(WEIGHTS_DIR / CROP_MODEL, torch.device("cpu"))
    content = _jpeg(1200, 800, "lightgrey")
    first, second = scorer.score(content), scorer.score(content)
    assert 0.0 <= first <= 1.0
    assert first == second

"""The crop model: is the field area of the card (No. KK header and member table) whole in the photo?

A second, separate model beside the blur stack in `kk_quality.py`, trained in `quality/crop_model/` (run
`mnv3s_v1`). The blur stack catches blur and non-KK documents well but scores a sharp photo of a card cut at
the edge, or covered by an ID card or a hand, as good (3 of 18 crops rejected in kk_eval). This model answers
`probability_crop` for exactly that case.

It never rejects a document: as a rejector it also refused readable cards whose layout only looked cut. Its
use is the claim gate in scoring, which withholds a confident `confidence` from every field of a document
that is likely cut (`ocr_common.kk.scored_fields`).

The preprocessing reproduces training step for step, because a different resize produces a plausible number
for a different image: decode at reduced size, longest side 640 and re-encoded as JPEG (the training cache),
then letterboxed onto a black 384x384 square -- never center-cropped, since the edges are the signal.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

#: The training cache (`quality/crop_model/common.py`): longest side, and the JPEG quality it was saved at.
CACHE_SIDE = 640
CACHE_JPEG_QUALITY = 88
#: The model input: a letterboxed square.
INPUT_SIZE = 384
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

SUPPORTED_ARCH = "mobilenet_v3_small"


class CropScorer:
    """`probability_crop` for an image already validated and decoded by `kk_quality` (PDFs already rendered)."""

    def __init__(self, checkpoint: Path, device: Any):
        import torch  # ty: ignore[unresolved-import]
        from torch import nn  # ty: ignore[unresolved-import]
        from torchvision import models  # ty: ignore[unresolved-import]

        # R32: `weights_only=True` so a checkpoint cannot execute code while being loaded.
        saved = torch.load(checkpoint, map_location=device, weights_only=True)
        arch = saved.get("arch") if isinstance(saved, dict) else None
        if arch != SUPPORTED_ARCH or "state_dict" not in saved:
            raise RuntimeError(
                f"{checkpoint.name}: expected a {SUPPORTED_ARCH} checkpoint with a state_dict, got arch={arch!r}"
            )
        input_size = saved.get("input", INPUT_SIZE)
        if input_size != INPUT_SIZE:
            raise RuntimeError(f"{checkpoint.name}: trained at input {input_size}, this scorer feeds {INPUT_SIZE}")

        model = models.mobilenet_v3_small(weights=None)
        model.classifier = nn.Sequential(nn.Dropout(0.2), nn.Linear(model.classifier[0].in_features, 1))
        model.load_state_dict(saved["state_dict"])
        model.eval()
        self._torch = torch
        self._model = model.to(device)
        self._device = device
        self.metadata: dict[str, Any] = {
            "architecture": arch,
            "input": INPUT_SIZE,
            "best_epoch": saved.get("epoch"),
            "val_auc": saved.get("val_auc"),
        }

    def score(self, image_bytes: bytes) -> float:
        """`probability_crop` in [0, 1] for JPEG/PNG bytes."""
        import numpy as np  # ty: ignore[unresolved-import]

        torch = self._torch
        array = (np.asarray(_model_input(image_bytes), dtype=np.float32) / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
        tensor = torch.from_numpy(np.ascontiguousarray(array.transpose(2, 0, 1), dtype=np.float32))
        with torch.inference_mode():
            logit = self._model(tensor.unsqueeze(0).to(self._device))
        return float(torch.sigmoid(logit.float()).cpu().item())


def _model_input(image_bytes: bytes) -> Any:
    """The 384x384 RGB letterbox, built the way `quality/crop_model` built its training inputs."""
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    image.draft("RGB", (CACHE_SIDE * 2, CACHE_SIDE * 2))
    image = image.convert("RGB")
    image.thumbnail((CACHE_SIDE, CACHE_SIDE))
    cached = io.BytesIO()
    image.save(cached, format="JPEG", quality=CACHE_JPEG_QUALITY)
    image = Image.open(io.BytesIO(cached.getvalue())).convert("RGB")

    image.thumbnail((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR)
    square = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (0, 0, 0))
    square.paste(image, ((INPUT_SIZE - image.width) // 2, (INPUT_SIZE - image.height) // 2))
    return square

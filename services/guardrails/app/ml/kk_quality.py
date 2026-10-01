"""The `kk_quality` backend: the K2Quality model core, run in this process.

Only the **model core** came across: the patch-based blur CNN, the 19 classical CV features, the
XGBoost booster over the 23-feature vector, and the isotonic calibration of the CNN probability.
What stayed behind, and why it could:

* `src/core/config.py` -- `quality_service.py` imported `settings` and used it zero times; the two
  values it really needed (the artifact file names, the NR-IQA switch) are constants here.
* `src/services/database_service.py` -- only ever reached from `routes.py` / `main.py`.
* `src/services/threshold_provider.py` -- the database-backed threshold and its `PUT /config`.
  R15 replaces the whole thing with the chain in `app/clients/reject_threshold.py`.
* NR-IQA (PyIQA TOPIQ-NR + QualiCLIP) -- off by default in K2Quality (`quality.enable_nriqa:
  false`), because XGBoost handles NaN natively. The two feature slots are still filled with NaN so
  the vector keeps its canonical length and ordering; only the two optional models are gone.
* The three-track `asyncio.gather` -- `GuardrailsService` already runs the whole assessment in
  `run_in_threadpool`, so the tracks are sequential here. Splitting one document across three
  threads inside a thread that the event loop is already off is complexity without a payoff.

**Imports.** Every heavy import (torch, cv2, numpy, scipy, xgboost) happens in `__init__`, never at
module scope, so `app/dependencies.py` can import this module in a process that has none of them --
which is what the test suite and `GUARDRAILS_BACKEND=mock` do.

**Artifacts.** Six files, all produced by K2Quality's `KK/migrate_checkpoint.py`:
`blur_cnn_weights.pt`, `blur_cnn_meta.json`, `xgb_model.json`, `calibration.json`,
`feature_names.json` and the optional `model_hashes.json`.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.ml.base import UnassessableImage

logger = logging.getLogger(__name__)

# Filled by `_load_runtime()` at construction time. Declared here so the functions below can name
# them and so the linter can see them; None until a backend is actually built.
cv2: Any = None
np: Any = None
torch: Any = None
xgb: Any = None
_F: Any = None
_transforms: Any = None
_tv_models: Any = None
_kurtosis: Any = None
_skew: Any = None

# --- artifact names (K2Quality's own, unchanged) -------------------------------------------

BLUR_CNN_WEIGHTS = "blur_cnn_weights.pt"
BLUR_CNN_META = "blur_cnn_meta.json"
XGB_MODEL = "xgb_model.json"
CALIBRATION = "calibration.json"
FEATURE_NAMES = "feature_names.json"
MODEL_HASHES = "model_hashes.json"

REQUIRED_ARTIFACTS = (BLUR_CNN_WEIGHTS, BLUR_CNN_META, XGB_MODEL, CALIBRATION, FEATURE_NAMES)

# --- geometry of the blur CNN (must match KK/train_cnn.py) ---------------------------------

IMAGE_SIZE = 1024
PATCH_SIZE = 256
PATCH_STRIDE = PATCH_SIZE
GRID_SIZE = IMAGE_SIZE // PATCH_SIZE
NUM_PATCHES = GRID_SIZE * GRID_SIZE
FEATURE_DIM = 1280  # EfficientNet-B0 output channels

# --- input bounds ---------------------------------------------------------------------------

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_DIMENSION = 64
MAX_DIMENSION = 10_000
# Bounds the total pixel count for the Pillow paths, on top of the per-side MAX_DIMENSION check.
MAX_PIXELS = 4 * MAX_DIMENSION * MAX_DIMENSION

MAGIC_JPEG = b"\xff\xd8\xff"
MAGIC_PNG = b"\x89PNG"
MAGIC_PDF = b"%PDF"

PDF_RENDER_DPI = 150  # must match the training pipeline

CV_NORM_SHORT_SIDE = 512
NOISE_CROP_SIZE = 512

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

CLASSICAL_FEATURE_NAMES = (
    "laplacian_var",
    "tenengrad",
    "rms_contrast",
    "mean_brightness",
    "text_density",
    "edge_density",
    "fft_moire_energy",
    "fft_radial_kurtosis",
    "immerkaer_noise",
    "brightness_bimodality",
    "file_kb",
    "aspect_ratio",
    "skew_angle",
    "binarization_quality",
    "stroke_width_consistency",
    "dct_high_low_ratio",
    "dct_mid_energy",
    "sharpness_per_brightness",
    "edge_per_contrast",
)
NRIQA_FEATURE_NAMES = ("nriqa_topiq", "nriqa_qualiclip")


def _load_runtime() -> None:
    """Import the scientific stack once, into this module's globals.

    This is the whole reason the module is importable without torch: nothing above this line needs
    any of it, and `app/dependencies.py` imports the module unconditionally so the backend registry
    can list `kk_quality` whatever backend is actually configured.
    """
    global cv2, np, torch, xgb, _F, _transforms, _tv_models, _kurtosis, _skew
    if np is not None:
        return
    import cv2  # ty: ignore[unresolved-import]
    import numpy as np  # ty: ignore[unresolved-import]
    import torch  # ty: ignore[unresolved-import]
    import torch.nn.functional as _F  # ty: ignore[unresolved-import]
    import xgboost as xgb  # ty: ignore[unresolved-import]
    from scipy.stats import kurtosis as _kurtosis  # ty: ignore[unresolved-import]
    from scipy.stats import skew as _skew  # ty: ignore[unresolved-import]
    from torchvision import models as _tv_models  # ty: ignore[unresolved-import]
    from torchvision import transforms as _transforms  # ty: ignore[unresolved-import]


# --- decoding and validation ----------------------------------------------------------------
#
# Every failure below is an `UnassessableImage`, which becomes verdict `unassessable` and HTTP 200
# (R14a). K2Quality raised `ImageValidationError` / `ImageLoadError` here, and the service this one
# replaces turned the same conditions into a 400, which §5.2 forbids.


def _validate_bytes(content: bytes) -> None:
    """Size and magic bytes. The content decides, not the declared `Content-Type`."""
    if len(content) > MAX_IMAGE_BYTES:
        raise UnassessableImage(f"file too large to assess: {len(content)} bytes (max {MAX_IMAGE_BYTES})")
    if not content.startswith((MAGIC_JPEG, MAGIC_PNG, MAGIC_PDF)):
        raise UnassessableImage("unsupported file content: the bytes are not JPEG, PNG or PDF")


def _check_declared_dimensions(content: bytes) -> None:
    """Reject a decompression bomb from the header, before anything allocates the raster.

    `Image.open` parses only the header, so the declared size is available without decoding. A
    header this cannot parse is left to the decoder, so a genuine decode failure is still reported
    as one rather than being masked as a size problem.
    """
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(content)) as probe:
            width, height = probe.size
    except Image.DecompressionBombError as exc:
        raise UnassessableImage(f"image too large: {exc}") from exc
    except Exception:
        return

    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise UnassessableImage(f"image too large: {width}x{height} (max {MAX_DIMENSION}px per side)")
    if width * height > MAX_PIXELS:
        raise UnassessableImage(f"image too large: {width}x{height} = {width * height} pixels (max {MAX_PIXELS})")


def _decode(content: bytes) -> tuple[Any, Any]:
    """`(gray_float, img_rgb)` at the original resolution: grayscale in [0, 1] and RGB uint8."""
    try:
        _check_declared_dimensions(content)
        array = np.frombuffer(content, dtype=np.uint8)
        image_bgr = cv2.imdecode(array, cv2.IMREAD_COLOR)
    except UnassessableImage:
        raise
    except Exception as exc:
        raise UnassessableImage(f"failed to decode image: {exc}") from exc

    if image_bgr is None:
        raise UnassessableImage("cv2.imdecode returned None: corrupt or unsupported image")

    height, width = image_bgr.shape[:2]
    if height < MIN_DIMENSION or width < MIN_DIMENSION:
        raise UnassessableImage(f"image too small: {width}x{height} (min {MIN_DIMENSION}px per side)")
    if height > MAX_DIMENSION or width > MAX_DIMENSION:
        raise UnassessableImage(f"image too large: {width}x{height} (max {MAX_DIMENSION}px per side)")

    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    del image_bgr
    return gray.astype(np.float32) / 255.0, image_rgb


def render_pdf_first_page(content: bytes, dpi: int = PDF_RENDER_DPI) -> bytes:
    """Rasterise page 1 of a PDF to PNG bytes, at the DPI the CNN was trained on.

    A Kartu Keluarga is one page, so only the first one is ever looked at -- the same page
    extraction reads. The orchestrator has already refused a PDF above `MAX_DOCUMENT_PAGES`.
    """
    import fitz  # ty: ignore[unresolved-import]

    try:
        with fitz.open(stream=content, filetype="pdf") as document:
            if document.is_encrypted:
                raise UnassessableImage("PDF is encrypted")
            if len(document) == 0:
                raise UnassessableImage("PDF has no pages")
            zoom = dpi / 72.0
            pixmap = document[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            if pixmap.width > MAX_DIMENSION or pixmap.height > MAX_DIMENSION:
                raise UnassessableImage(
                    f"rendered PDF page too large: {pixmap.width}x{pixmap.height} (max {MAX_DIMENSION}px per side)"
                )
            return pixmap.tobytes("png")
    except UnassessableImage:
        raise
    except Exception as exc:
        raise UnassessableImage(f"failed to render PDF: {exc}") from exc


# --- classical CV features (19) ---------------------------------------------------------------


def _resize_short_side(gray: Any, target_short: int) -> Any:
    height, width = gray.shape[:2]
    short = min(height, width)
    if short <= target_short:
        return gray
    scale = target_short / short
    return cv2.resize(gray, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)


def _center_crop(gray: Any, crop_size: int) -> Any:
    height, width = gray.shape[:2]
    crop_h, crop_w = min(height, crop_size), min(width, crop_size)
    top, left = (height - crop_h) // 2, (width - crop_w) // 2
    return gray[top : top + crop_h, left : left + crop_w]


def _immerkaer_noise(gray_crop: Any) -> float:
    """Immerkaer Laplacian-kernel noise estimate on a grayscale [0, 1] patch."""
    height, width = gray_crop.shape
    if height < 3 or width < 3:
        return float("nan")
    kernel = np.array([1, -2, 1], dtype=np.float32)
    horizontal = cv2.filter2D(gray_crop, cv2.CV_32F, kernel.reshape(1, -1))
    laplacian = cv2.filter2D(horizontal, cv2.CV_32F, kernel.reshape(-1, 1))
    return float(np.sqrt(np.pi / 2.0) / (6.0 * height * width) * np.abs(laplacian).sum())


def compute_skew_angle(gray_uint8: Any, target_short: int = 512) -> float:
    """Document skew from the projection profile, with a Hough fallback. Downsampled for speed."""
    height, width = gray_uint8.shape
    if height < 100 or width < 100:
        return float("nan")

    if min(height, width) > target_short:
        scale = target_short / min(height, width)
        small = cv2.resize(gray_uint8, (int(width * scale), int(height * scale)))
    else:
        small = gray_uint8

    _, binary = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    foreground_ratio = np.mean(binary > 0)
    if 0.01 < foreground_ratio < 0.95:
        rows, columns = binary.shape
        center = (columns / 2, rows / 2)

        def variance_at(angle: float) -> float:
            matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
            rotated = cv2.warpAffine(binary, matrix, (columns, rows), flags=cv2.INTER_NEAREST, borderValue=0)
            return float(np.var(np.sum(rotated, axis=1, dtype=np.float64)))

        best_variance, best_angle = -1.0, 0.0
        for angle in np.arange(-15.0, 15.1, 3.0):  # coarse sweep
            variance = variance_at(float(angle))
            if variance > best_variance:
                best_variance, best_angle = variance, float(angle)
        for angle in np.arange(best_angle - 1.5, best_angle + 1.6, 0.5):  # fine sweep
            if angle < -15.0 or angle > 15.0:
                continue
            variance = variance_at(float(angle))
            if variance > best_variance:
                best_variance, best_angle = variance, float(angle)
        return abs(best_angle)

    filtered = cv2.bilateralFilter(small, 9, 75, 75)
    edges = cv2.Canny(filtered, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=80, minLineLength=max(small.shape[1] // 8, 50), maxLineGap=20
    )
    if lines is None or len(lines) < 3:
        return float("nan")
    angles, weights = [], []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(angle) < 45:
            angles.append(angle)
            weights.append(np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2))
    if len(angles) < 3:
        return float("nan")
    order = np.argsort(angles)
    cumulative = np.cumsum(np.array(weights)[order])
    median = np.searchsorted(cumulative, cumulative[-1] / 2)
    return float(abs(np.array(angles)[order][median]))


def compute_binarization_features(gray_uint8: Any, target_short: int = 512) -> dict[str, float]:
    """How much of the ink is text rather than speckle, and how even the stroke widths are."""
    nan = {"binarization_quality": float("nan"), "stroke_width_consistency": float("nan")}
    height, width = gray_uint8.shape
    if height < 64 or width < 64:
        return nan

    if min(height, width) > target_short:
        scale = target_short / min(height, width)
        small = cv2.resize(gray_uint8, (int(width * scale), int(height * scale)))
    else:
        small = gray_uint8

    threshold = cv2.adaptiveThreshold(
        small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, blockSize=51, C=10
    )
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(threshold, connectivity=8)
    if n_labels <= 1:
        return nan

    areas = stats[1:, cv2.CC_STAT_AREA]
    min_text = max(20, int(small.shape[0] * small.shape[1] * 0.00001))
    text_total = int(np.sum(areas[areas >= min_text])) if np.any(areas >= min_text) else 0
    noise_total = int(np.sum(areas[areas < min_text])) if np.any(areas < min_text) else 0
    foreground = text_total + noise_total

    features = {
        "binarization_quality": float(text_total / foreground) if foreground > 0 else float("nan"),
        "stroke_width_consistency": float("nan"),
    }
    if text_total > 0:
        distance = cv2.distanceTransform(threshold, cv2.DIST_L2, 3)
        strokes = distance[threshold > 0]
        if len(strokes) > 100:
            features["stroke_width_consistency"] = float(np.std(strokes) / max(np.mean(strokes), 0.1))
    return features


def compute_dct_features(gray_float: Any) -> dict[str, float]:
    """DCT blur features with the DC term excluded and the energy split into frequency bands."""
    nan = {"dct_high_low_ratio": float("nan"), "dct_mid_energy": float("nan")}
    height, width = gray_float.shape
    short = min(height, width)
    crop_size = 512 if short >= 512 else (256 if short >= 256 else 0)
    if crop_size == 0:
        return nan

    half = crop_size // 2
    center_y, center_x = height // 2, width // 2
    crop = np.ascontiguousarray(gray_float[center_y - half : center_y + half, center_x - half : center_x + half])
    dct = cv2.dct(crop)

    frequency = np.arange(crop_size, dtype=np.float32) / crop_size
    rows, columns = np.meshgrid(frequency, frequency, indexing="ij")
    distance = np.sqrt(rows**2 + columns**2)

    energy = dct**2
    energy[0, 0] = 0  # the DC term is the mean brightness, not detail
    total = np.sum(energy)
    if total < 1e-10:
        return nan

    low = np.sum(energy[distance < 0.15])
    mid = np.sum(energy[(distance >= 0.15) & (distance < 0.40)])
    high = np.sum(energy[distance >= 0.40])
    return {"dct_high_low_ratio": float(high / max(low, 1e-10)), "dct_mid_energy": float(mid / total)}


def extract_classical_features(gray_float: Any, file_kb: float) -> dict[str, float]:
    """The 19 classical CV features, on the grayscale [0, 1] image at its original resolution."""
    height, width = gray_float.shape
    features: dict[str, float] = {}

    gray_uint8 = (gray_float * 255).astype(np.uint8)
    # Sharpness is resolution-dependent, so it is measured on a normalised short side; everything
    # that is a ratio or a distribution is measured on the full image.
    normalised = _resize_short_side(gray_float, CV_NORM_SHORT_SIDE)
    normalised_64 = normalised.astype(np.float64)

    features["laplacian_var"] = float(np.var(cv2.Laplacian(normalised_64, cv2.CV_64F)))
    sobel_x = cv2.Sobel(normalised_64, cv2.CV_64F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(normalised_64, cv2.CV_64F, 0, 1, ksize=3)
    features["tenengrad"] = float(np.mean(sobel_x**2 + sobel_y**2))
    features["rms_contrast"] = float(np.std(gray_float))
    features["mean_brightness"] = float(np.mean(gray_float))

    if np.std(gray_float) < 2.0 / 255.0:
        features["text_density"] = float("nan")
    else:
        _, binarized = cv2.threshold(gray_uint8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        features["text_density"] = float(np.mean(binarized < 128))

    gradient_y, gradient_x = np.gradient(normalised)
    features["edge_density"] = float(np.mean(np.sqrt(gradient_x**2 + gradient_y**2) > 0.08))

    magnitude = np.abs(np.fft.rfft2(normalised))
    frequency_y = np.fft.fftfreq(normalised.shape[0])
    frequency_x = np.fft.rfftfreq(normalised.shape[1])
    grid_y, grid_x = np.meshgrid(frequency_y, frequency_x, indexing="ij")
    radial = np.sqrt(grid_x**2 + grid_y**2)
    mid_band = (radial >= 0.05) & (radial <= 0.45)

    total_energy = np.sum(magnitude**2)
    if total_energy < 1e-12:
        features["fft_moire_energy"] = float("nan")
        features["fft_radial_kurtosis"] = float("nan")
    else:
        features["fft_moire_energy"] = float(np.sum(magnitude[mid_band] ** 2) / total_energy)
        mid_values = magnitude[mid_band].flatten()
        features["fft_radial_kurtosis"] = (
            float(_kurtosis(mid_values, fisher=True)) if len(mid_values) > 10 else float("nan")
        )

    features["immerkaer_noise"] = _immerkaer_noise(_center_crop(gray_float, NOISE_CROP_SIZE))

    brightness = gray_float.ravel()
    kurtosis = _kurtosis(brightness, fisher=False)
    if kurtosis < 1e-8:
        features["brightness_bimodality"] = float("nan")
    else:
        features["brightness_bimodality"] = float((_skew(brightness) ** 2 + 1) / kurtosis)

    features["file_kb"] = file_kb
    features["aspect_ratio"] = float(max(height, width) / max(min(height, width), 1))

    features["skew_angle"] = compute_skew_angle(gray_uint8)
    features.update(compute_binarization_features(gray_uint8))
    features.update(compute_dct_features(gray_float))

    features["sharpness_per_brightness"] = float(features["laplacian_var"] / max(features["mean_brightness"], 0.01))
    features["edge_per_contrast"] = float(features["edge_density"] / max(features["rms_contrast"], 0.01))
    return features


# --- the model ---------------------------------------------------------------------------------


def _build_blur_cnn(aggregation: str) -> Any:
    """The `PatchBlurNet` architecture. Defined here rather than at module scope because
    `nn.Module` cannot be subclassed before torch is imported, and torch is imported in `__init__`.
    """
    nn = torch.nn

    class AttentionAggregation(nn.Module):
        """Learnable attention weights over the patch features."""

        def __init__(self, feat_dim: int, hidden_dim: int = 128):
            super().__init__()
            self.attention = nn.Sequential(nn.Linear(feat_dim, hidden_dim), nn.Tanh(), nn.Linear(hidden_dim, 1))

        def forward(self, features):
            weights = torch.softmax(self.attention(features), dim=1)
            return (features * weights).sum(dim=1)

    class PatchBlurNet(nn.Module):
        """EfficientNet-B0 over a 4x4 grid of 256px patches, pooled by attention.

        The backbone is built with `weights=None` on purpose: asking for IMAGENET1K_V1 would
        download ~21 MB from download.pytorch.org at start-up, which is unreachable behind the
        on-prem proxy, and it would be overwritten a moment later by `load_state_dict(strict=True)`
        anyway. Only the architecture matters here.
        """

        def __init__(self, aggregation: str = "attention"):
            super().__init__()
            self.aggregation_mode = aggregation
            backbone = _tv_models.efficientnet_b0(weights=None)
            self.features = backbone.features
            self.pool = nn.AdaptiveAvgPool2d(1)
            self.feat_dim = FEATURE_DIM
            for parameter in list(self.features.parameters())[:-30]:
                parameter.requires_grad = False
            self.head = nn.Sequential(
                nn.Dropout(0.3),
                nn.Linear(self.feat_dim, 256),
                nn.ReLU(inplace=True),
                nn.Dropout(0.2),
                nn.Linear(256, 1),
            )
            self.attention_agg = AttentionAggregation(self.feat_dim, hidden_dim=128)
            self.attn_head = nn.Sequential(
                nn.Dropout(0.3),
                nn.Linear(self.feat_dim, 256),
                nn.ReLU(inplace=True),
                nn.Dropout(0.2),
                nn.Linear(256, 1),
            )

        def forward(self, patches):
            batch, count, channels, height, width = patches.shape
            flat = patches.reshape(batch * count, channels, height, width)
            features = self.pool(self.features(flat)).flatten(1).reshape(batch, count, -1)
            patch_logits = self.head(features)
            image_logit = self.attn_head(self.attention_agg(features))
            return image_logit, patch_logits

    return PatchBlurNet(aggregation=aggregation)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class KKQualityModel:
    """Blur CNN + XGBoost + isotonic calibration over one Kartu Keluarga image.

    `assess()` answers `probability_bad`; the threshold that turns it into a verdict lives outside,
    in the R15 chain, because it can be changed at the central orchestrator without a deploy here.
    """

    name = "kk_quality"

    def __init__(self, weights_dir: str, device: str = "cpu", threads: int | None = None):
        # Before `_load_runtime()`, deliberately: a weights directory that is not there is a
        # configuration error, and it should say so in milliseconds rather than after several
        # seconds of importing torch -- which is also what makes this branch testable anywhere.
        base = Path(weights_dir)
        missing = [name for name in REQUIRED_ARTIFACTS if not (base / name).is_file()]
        if missing:
            raise RuntimeError(
                f"Guardrails weights incomplete in {base} (GUARDRAILS_WEIGHTS_DIR): missing {', '.join(missing)}. "
                f"Fetch all of {', '.join(REQUIRED_ARTIFACTS)}"
            )

        _load_runtime()
        from PIL import Image

        # K2Quality raises Pillow's own bomb ceiling because MAX_DIMENSION alone allows a
        # 10000x10000 scan (1e8 pixels), well above Pillow's ~89M default. The per-side and
        # per-pixel checks in `_check_declared_dimensions` are what actually bound the input.
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS

        self._verify_hashes(base)

        if device != "cpu" and not (device.startswith("cuda") and torch.cuda.is_available()):
            logger.warning("guardrails device %r not available in this build; falling back to cpu", device)
            device = "cpu"
        if threads:
            torch.set_num_threads(threads)
        self._device = torch.device(device)

        meta = _read_json(base / BLUR_CNN_META)
        aggregation = str(meta.get("aggregation_type", "attention"))
        # The same class of guard the inherited checkpoint needed for its `class_names`: read what
        # the artifact says it is, refuse anything else, and never assume a default. A silently wrong
        # head here does not crash -- it produces a plausible number for the wrong computation.
        if aggregation != "attention":
            raise RuntimeError(
                f"Unsupported aggregation_type {aggregation!r} in {BLUR_CNN_META}: only 'attention' is "
                "implemented, and guessing would score the image with the wrong head"
            )

        blur_cnn = _build_blur_cnn(aggregation)
        # R32: `weights_only=True` so a checkpoint cannot execute code while being loaded.
        state_dict = torch.load(base / BLUR_CNN_WEIGHTS, map_location=self._device, weights_only=True)
        blur_cnn.load_state_dict(state_dict)
        blur_cnn.eval()
        self._blur_cnn = blur_cnn.to(self._device)

        self._xgb = xgb.Booster()
        self._xgb.load_model(str(base / XGB_MODEL))

        calibration = _read_json(base / CALIBRATION)
        self._iso_x = np.array(calibration["isotonic_x_thresholds"], dtype=np.float64)
        self._iso_y = np.array(calibration["isotonic_y_thresholds"], dtype=np.float64)
        if len(self._iso_x) != len(self._iso_y) or len(self._iso_x) < 2:
            raise RuntimeError(f"{CALIBRATION}: isotonic_x_thresholds and isotonic_y_thresholds must pair up")
        if not np.all(np.diff(self._iso_x) >= 0):
            # np.interp silently returns nonsense for a non-monotonic x, and the nonsense looks
            # like a probability.
            raise RuntimeError(f"{CALIBRATION}: isotonic_x_thresholds must be non-decreasing")

        self.feature_names: list[str] = [str(name) for name in _read_json(base / FEATURE_NAMES)]
        # The vector is assembled in the order this file gives, never in a fixed order written here:
        # a permuted vector is the KK equivalent of reading `class_names` the wrong way round --
        # every number is in range and the verdict is unrelated to the image. The booster's own
        # width is the one cross-check available, so a mismatch refuses to start.
        booster_features = self._booster_feature_count()
        if booster_features is not None and booster_features != len(self.feature_names):
            raise RuntimeError(
                f"{XGB_MODEL} expects {booster_features} features but {FEATURE_NAMES} lists "
                f"{len(self.feature_names)}; these artifacts are not from the same export"
            )

        #: R15, fourth rung. K2Quality keeps its operating point in `config.yaml` (`quality.threshold`),
        #: NOT in any of the six artifacts, so this key does not exist in today's export and the rung
        #: is inert until the ML team writes it into `blur_cnn_meta.json`. None -- not 0.5 -- so that
        #: an absent rung stays visible instead of merging into the floor below it.
        self.reject_threshold: float | None = _optional_threshold(meta)

        self.metadata: dict[str, Any] = {
            "architecture": "patch_blur_net+xgboost",
            "device": device,
            "threads": torch.get_num_threads(),
            "aggregation": aggregation,
            "best_epoch": meta.get("best_epoch"),
            "n_features": len(self.feature_names),
            "n_calibration_points": len(self._iso_x),
            "reject_threshold": self.reject_threshold,
        }
        logger.info("guardrails model loaded from %s: %s", base, self.metadata)

    # --- loading helpers ---------------------------------------------------------------

    @staticmethod
    def _verify_hashes(base: Path) -> None:
        """Check the artifacts against `model_hashes.json` when it is there.

        A missing hash file is a warning, not a failure: it is produced by the migration script and
        an operator who copied the weights by hand will not have it. A *mismatch* is fatal.
        """
        hashes_path = base / MODEL_HASHES
        if not hashes_path.is_file():
            logger.warning("%s not found in %s; skipping the integrity check", MODEL_HASHES, base)
            return
        for filename, expected in _read_json(hashes_path).items():
            candidate = base / filename
            if not candidate.is_file():
                logger.warning("cannot verify %s: not in %s", filename, base)
                continue
            actual = _sha256(candidate)
            if actual != expected:
                raise RuntimeError(
                    f"SHA-256 mismatch for {filename}: expected {expected[:16]}..., got {actual[:16]}... "
                    "-- the artifact is corrupt or was replaced"
                )

    def _booster_feature_count(self) -> int | None:
        """The booster's own feature width, or None when this XGBoost build does not expose it."""
        try:
            return int(self._xgb.num_features())
        except Exception:  # pragma: no cover - depends on the xgboost build
            return None

    # --- inference ---------------------------------------------------------------------

    def assess(self, filename: str, content_type: str | None, content: bytes) -> float:
        """`probability_bad` for one document. Raises `UnassessableImage` when it cannot be judged."""
        _validate_bytes(content)
        file_kb = len(content) / 1024.0

        if content.startswith(MAGIC_PDF):
            content = render_pdf_first_page(content)

        gray_float, image_rgb = _decode(content)
        features = extract_classical_features(gray_float, file_kb)
        features.update(self._blur_cnn_features(image_rgb))
        features.update(dict.fromkeys(NRIQA_FEATURE_NAMES, float("nan")))

        missing = [name for name in self.feature_names if name not in features]
        if missing:
            logger.warning("guardrails features missing, using NaN: %s", missing)
            features.update(dict.fromkeys(missing, float("nan")))

        return self._predict(features)

    def _blur_cnn_features(self, image_rgb: Any) -> dict[str, float]:
        """The raw CNN probability and its isotonic calibration."""
        patches = self._prepare_patches(image_rgb)
        if patches is None:
            logger.warning("image too small for the blur CNN; its two features are NaN")
            return {"blur_cnn_raw": float("nan"), "blur_cnn_calibrated": float("nan")}

        patches = patches.to(self._device)
        try:
            with torch.inference_mode():
                with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=self._device.type == "cuda"):
                    logit, _ = self._blur_cnn(patches)
            raw = float(torch.sigmoid(logit).float().cpu().item())
            return {"blur_cnn_raw": raw, "blur_cnn_calibrated": float(np.interp(raw, self._iso_x, self._iso_y))}
        finally:
            del patches
            if self._device.type == "cuda":
                torch.cuda.empty_cache()

    @staticmethod
    def _prepare_patches(image_rgb: Any) -> Any:
        """`[1, 16, 3, 256, 256]`, or None when the image is smaller than one patch."""
        from PIL import Image

        height, width = image_rgb.shape[:2]
        if height < PATCH_SIZE or width < PATCH_SIZE:
            return None

        image = Image.fromarray(image_rgb)
        if width > IMAGE_SIZE or height > IMAGE_SIZE:
            scale = IMAGE_SIZE / min(width, height)
            image = image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)
            width, height = image.size
            left, top = (width - IMAGE_SIZE) // 2, (height - IMAGE_SIZE) // 2
            image = image.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))
        else:
            image = image.resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.LANCZOS)

        tensor = _transforms.functional.to_tensor(image)
        tensor = _transforms.Normalize(mean=list(IMAGENET_MEAN), std=list(IMAGENET_STD))(tensor)

        channels, rows, columns = tensor.shape
        needed = PATCH_SIZE + (GRID_SIZE - 1) * PATCH_STRIDE
        pad_rows, pad_columns = max(0, needed - rows), max(0, needed - columns)
        if pad_rows or pad_columns:
            tensor = _F.pad(tensor, (0, pad_columns, 0, pad_rows), mode="reflect")

        patches = tensor.unfold(1, PATCH_SIZE, PATCH_STRIDE).unfold(2, PATCH_SIZE, PATCH_STRIDE)
        patches = patches.contiguous().permute(1, 2, 0, 3, 4).reshape(-1, channels, PATCH_SIZE, PATCH_SIZE)
        del tensor
        return patches.unsqueeze(0)

    def _predict(self, features: dict[str, float]) -> float:
        """The booster over the canonical vector. NaN is passed through: XGBoost handles it."""
        vector = np.array([[features.get(name, float("nan")) for name in self.feature_names]], dtype=np.float32)
        probability = float(self._xgb.inplace_predict(vector)[0])
        if not 0.0 <= probability <= 1.0:
            raise UnassessableImage(f"model returned {probability}, which is not a probability")
        return probability


def _optional_threshold(meta: dict[str, Any]) -> float | None:
    """`decision_threshold` (or `threshold`) from `blur_cnn_meta.json`, when it is a usable one.

    Anything outside (0, 1) is ignored with a warning rather than honoured: 0 would reject every
    image and 1 almost none, and an artifact should not be able to disable the gate by accident.
    """
    for key in ("decision_threshold", "threshold"):
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        if 0 < value < 1:
            return float(value)
        logger.warning("%s: %s=%r is outside (0, 1); ignoring it", BLUR_CNN_META, key, value)
    return None

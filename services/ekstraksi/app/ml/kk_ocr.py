"""The `kk_ocr` backend: PP-OCRv5 server detection + recognition, in PyTorch, in this process.

**Which of K2Extractor's two backends this is, and why.** `ocr_backends.py` ships two and the
*default* is the wrong one for us: `PaddleOCRBackend` wraps native PaddleOCR (`from paddleocr import
PaddleOCR`, `paddlex_config`, oneDNN kernel workarounds) and would put paddlepaddle in this image.
`FullPyTorchBackend` -- selected, never defaulted to, with `OCR_BACKEND=fullpytorch` -- runs the same
PP-OCRv5 server weights as `.pth` under torch. This port takes the torch one, for three reasons that
are all checkable rather than aesthetic:

1. Phase 0 already decided it. `Makefile`'s `lock-ekstraksi` was split out of the shared pattern
   rule specifically to carry `--extra-index-url .../whl/cpu`, the PyTorch CPU index. There is no
   Paddle index anywhere in the repository, and guardrails already pins `torch==2.14.0`.
2. The §7.1 spike was run against this path, not the other. The frozen payload shape was verified
   against what `FullPyTorchBackend.predict` actually returns.
3. The two return **different Python types** -- Paddle's `predict` returns its own result objects,
   the torch one returns `[{"rec_texts": [...], "rec_scores": [...], "rec_polys": [ndarray]}]` --
   so this is a choice, not a detail. `app/ml/utils.py` converts the torch shape.

`ppocrv5_conversion.py` is deliberately absent: it calls `import paddle` because its job is turning
Paddle checkpoints into `.pth`. It is an offline tool and does not belong in a service image, so
this backend loads already-converted weights and never converts (`auto_convert_weights` is the
K2Extractor switch we do not carry).

**R18b -- orientation and deskewing, an open gap, named here rather than papered over.**
§7.1 promises `poly` "in pixels of the image PaddleOCR has already straightened". This path does not
straighten anything: `FullPyTorchBackend._build_system` sets `args.use_angle_cls = False` and
carries no document rectifier, and the spike confirmed it by measurement -- all 177 boxes came back
tilted, in the frame of the photo as uploaded. So the coordinates this backend produces are in the
**submitted** frame, and §7.1 as written is not satisfied by it.

The owner of the gap is this module: deskewing, if it is to happen at all, happens here, before
detection, because this is the only place that still holds the image. It is **not** implemented, and
no no-op hook pretends otherwise -- a pass-through named `deskew()` would read as "handled" in every
future review. Two ways to close it, and the choice is not this unit's to make alone:

* rectify here (angle from the detected boxes' dominant tilt, rotate, re-detect), which honours
  §7.1 and costs a second detection pass; or
* amend §7.1 to say the frame is the submitted image, which costs nothing here and moves the
  problem to structuring's column assignment.

Until one is chosen, this backend is honest about which frame it reports and the mock -- which the
batch is declared finished with (R20a) -- emits tilted polygons so nothing downstream may assume
upright ones. The failure is systematic and only shows up in the next batch, which is exactly why it
is written down at the point where it would be introduced.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ocr_common.errors import InternalError
from ocr_common.types import OcrEngineResult

from app.ml.utils import boxes_from_rec_lists

#: What the weights are, for `OcrPayload.model`. K2Extractor is PP-OCRv5 while §7.1's example says
#: PP-OCRv6; the example is an example, and reporting the version that actually ran is the point of
#: the field. R18 lists the version mismatch as something to settle with the ML team.
MODEL_NAME = "PP-OCRv5_server_det+PP-OCRv5_server_rec"


@dataclass(frozen=True)
class KkOcrConfig:
    """Where the runtime and the weights are, and how the detector and recogniser are sized.

    The four numeric knobs are not decoration: the spike ran the backend on **constructor defaults**
    rather than K2Extractor's configuration and got 177 mostly single-character boxes out of one
    card. It validated the payload *shape*, explicitly not the recognition *quality*. Whoever brings
    the weights has to tune these against the 1184-document baseline, so they are settings rather
    than constants.
    """

    ppocr_root: Path
    """The vendored PaddleOCR2Pytorch tree (`tools.infer.predict_system`, `models.ppocrv5_server`)."""
    det_weights: Path
    rec_weights: Path
    device: str = "cpu"
    rec_batch_size: int = 8
    rec_image_shape: str = "3,48,320"
    det_limit_side_len: int = 64
    det_limit_type: str = "min"
    torch_threads: int = 0
    pdf_dpi: int = 200
    """The DPI page 1 of a PDF is rendered at. Not guardrails' 150, which is a property of *its*
    training: KK table text at 150 DPI is a few pixels tall, and the layout parser's offsets are
    in pixels. Tune with the other knobs against the corpus baseline."""


class Predictor(Protocol):
    """The engine underneath: one image in, K2Extractor's `_to_paddle_result` shape out.

    A Protocol rather than the concrete `TextSystem` so the conversion, the empty-page case and the
    off-loop behaviour are all testable without the weights, which may not be available (R20a).
    """

    def predict(self, image_rgb: Any) -> list[dict[str, Any]]: ...


class KkOcrEngine:
    """PP-OCRv5 det + rec in this process. **Synchronous** (`OcrRecognizer`): `EkstraksiService` runs
    it in the threadpool, which is what lets the Unit 3 heartbeat keep beating through a slow read."""

    name = "kk_ocr"

    def __init__(self, config: KkOcrConfig, predictor: Predictor | None = None):
        """`predictor` is injected by tests; in the service it is built here, at start-up, so a
        missing runtime or missing weights is a start-up failure rather than a failed job per request."""
        self._config = config
        self._predictor = predictor if predictor is not None else build_predictor(config)

    def read(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult:
        image = _decode(content, pdf_dpi=self._config.pdf_dpi)
        results = self._predictor.predict(image)
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise InternalError("the OCR model returned an unexpected result shape")
        page = results[0]
        # An image the detector found nothing in is `texts: []`, a DONE job, and a rejection one
        # stage later (§6.3, §7.4). It is never an error here.
        boxes = boxes_from_rec_lists(page.get("rec_texts"), page.get("rec_scores"), page.get("rec_polys"))
        return {"texts": boxes, "model": MODEL_NAME}


def _decode(content: bytes, *, pdf_dpi: int = 200) -> Any:
    """The upload as a contiguous RGB `HxWx3` array, which is what the detector expects.

    Pillow rather than OpenCV: it is already in the image, it honours the EXIF orientation tag that
    phone cameras set (a 90-degree rotation that no amount of deskewing downstream would recover),
    and it raises one exception type for anything it cannot open.

    A PDF is rendered first, page 1 only: a Kartu Keluarga is one sheet and the layout parser reads
    one page frame, so boxes from a second page could only be misplaced into the first.
    """
    import io

    from PIL import Image, ImageOps, UnidentifiedImageError

    if content.startswith(PDF_MAGIC):
        content = render_pdf_first_page(content, pdf_dpi)

    try:
        with Image.open(io.BytesIO(content)) as handle:
            image = ImageOps.exif_transpose(handle).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        # §6.3: an image that cannot be opened is a FAILED job and a 422, not a rejection.
        raise InternalError("the uploaded document could not be decoded as an image") from exc

    # Imported after the decode, not before: a bad upload must reach the message above rather than
    # an ImportError, and the array is the only thing numpy is needed for.
    #
    # Unresolved on purpose: numpy is not in this service's `requirements.txt`, because the batch
    # that brings the PP-OCRv5 weights is the one that adds the runtime (torch, numpy, Pillow) and
    # re-runs `make lock-ekstraksi`. Until then `EKSTRAKSI_BACKEND=kk_ocr` cannot start, which is
    # what R20a says it may do.
    import numpy as np  # ty: ignore[unresolved-import]

    return np.ascontiguousarray(np.asarray(image, dtype=np.uint8))


PDF_MAGIC = b"%PDF"


def render_pdf_first_page(content: bytes, dpi: int) -> bytes:
    """Page 1 of a PDF as PNG bytes. An encrypted, empty or unreadable PDF is a FAILED job (§6.3),
    like an image that cannot be opened -- never a rejection, which is structuring's to make."""
    import fitz

    try:
        with fitz.open(stream=content, filetype="pdf") as document:
            if document.is_encrypted:
                raise InternalError("the uploaded PDF is encrypted")
            if document.page_count == 0:
                raise InternalError("the uploaded PDF has no pages")
            zoom = dpi / 72.0
            return document[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
    except InternalError:
        raise
    except Exception as exc:
        raise InternalError("the uploaded document could not be decoded as a PDF") from exc


def build_predictor(config: KkOcrConfig) -> Predictor:
    """The torch PP-OCRv5 text system, built from the vendored PaddleOCR2Pytorch tree.

    Every heavy import is inside this function so that the module stays importable -- and type
    checkable, and testable with an injected predictor -- on a machine with neither torch nor the
    weights. That is `app/ml/efficientnet.py`'s pattern in guardrails, and the reason it exists
    there applies here for the same reason: the batch is finished with the mock (R20a).
    """
    import importlib
    import sys

    for path in (config.ppocr_root, config.det_weights, config.rec_weights):
        if not path.exists():
            raise InternalError(
                f"EKSTRAKSI_BACKEND=kk_ocr needs {path}, which does not exist. Fetch the PP-OCRv5 "
                "weights and the PaddleOCR2Pytorch tree before starting with this backend"
            )
    if str(config.ppocr_root) not in sys.path:
        sys.path.insert(0, str(config.ppocr_root))

    try:
        torch = importlib.import_module("torch")
        ppocr_model = importlib.import_module("models.ppocrv5_server")
        predict_system = importlib.import_module("tools.infer.predict_system")
        utility = importlib.import_module("tools.infer.pytorchocr_utility")
    except ImportError as exc:
        raise InternalError(f"EKSTRAKSI_BACKEND=kk_ocr could not load its runtime: {exc}") from exc

    if config.torch_threads:
        torch.set_num_threads(config.torch_threads)

    args = utility.init_args().parse_args([])
    args.use_gpu = config.device != "cpu"
    # Left False, as in K2Extractor: the angle classifier is a per-crop 0/180 flip, not a document
    # rectifier, so turning it on would not make §7.1's promise true. See R18b in the module docstring.
    args.use_angle_cls = False
    args.det_algorithm = "DB"
    args.det_yaml_path = str(config.ppocr_root / ppocr_model.DET_YAML_RELATIVE)
    args.det_model_path = str(config.det_weights)
    args.det_limit_side_len = config.det_limit_side_len
    args.det_limit_type = config.det_limit_type
    args.rec_yaml_path = str(config.ppocr_root / ppocr_model.REC_YAML_RELATIVE)
    args.rec_model_path = str(config.rec_weights)
    args.rec_image_shape = config.rec_image_shape
    args.rec_char_dict_path = str(config.ppocr_root / "pytorchocr/utils/dict/ppocrv5_dict.txt")
    args.rec_batch_num = config.rec_batch_size
    args.image_dir = ""

    system = predict_system.TextSystem(args)
    system.text_detector.net = ppocr_model.PPOCRv5ServerDetModel().net.to(config.device).float().eval()
    system.text_recognizer.net = ppocr_model.PPOCRv5ServerRecModel().net.to(config.device).float().eval()
    return _TextSystemPredictor(system)


class _TextSystemPredictor:
    """`TextSystem` in the shape `KkOcrEngine` reads: K2Extractor's `_to_paddle_result` dict."""

    def __init__(self, system: Any):
        self._system = system

    def predict(self, image_rgb: Any) -> list[dict[str, Any]]:
        boxes, rec_res = self._system(image_rgb)
        if boxes is None or len(boxes) == 0:
            return [{"rec_texts": [], "rec_scores": [], "rec_polys": []}]
        texts = [str(text) for text, _ in rec_res]
        scores = [float(score) for _, score in rec_res]
        return [{"rec_texts": texts, "rec_scores": scores, "rec_polys": list(boxes)}]

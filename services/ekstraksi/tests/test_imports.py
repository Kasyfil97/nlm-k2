"""The app must import without the OCR runtime.

`app/ml/kk_ocr.py` is imported unconditionally by the composition root, and it names torch, numpy
and Pillow. If any of those moved to module level the whole service would stop starting with
`EKSTRAKSI_BACKEND=mock` or `remote` -- a service that does not carry the weights would be unable
to boot because of a backend it never selects. Keeping them inside the functions is what makes the
image, the dev environment and the type checker all work without a 2 GB dependency, so it is worth
a test rather than a comment.
"""

import os
import subprocess
import sys
from pathlib import Path

from ocr_common.testing import TEST_API_KEY

SERVICE = Path(__file__).resolve().parents[1]
BLOCKED = ("torch", "numpy", "PIL", "cv2", "paddle", "paddleocr")

CODE = f"""
import importlib.abc, sys

BLOCKED = {BLOCKED!r}

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(name + " is not installed in this service")
        return None

sys.meta_path.insert(0, Block())
import app.main
import app.ml.kk_ocr
"""


def test_the_app_imports_without_the_ocr_runtime():
    env = {
        **os.environ,
        "API_KEY": TEST_API_KEY,
        "ENVIRONMENT": "local",
        "EKSTRAKSI_BACKEND": "mock",
        "DATABASE_URL": "",
        "ORCHESTRATION_URL": "",
        "PYTHONPATH": os.pathsep.join([str(SERVICE), str(SERVICE.parents[1] / "libs" / "ocr_common")]),
    }
    result = subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, cwd=SERVICE, env=env)
    assert result.returncode == 0, result.stderr

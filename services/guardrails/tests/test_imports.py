"""What the app must be able to import without.

Two separate guarantees, both of which have already failed once somewhere:

* **SQLAlchemy.** This service has no database. The 0.2.0 deploy of 2026-09-23 crashed at startup
  because a shared module pulled SQLAlchemy in.
* **torch, cv2, xgboost, scipy.** `app/dependencies.py` imports `app.ml.kk_quality` unconditionally
  so the backend registry can name it, and `kk_quality` keeps every heavy import inside
  `KKQualityModel.__init__`. If one of them ever drifts to module scope, a process running
  `GUARDRAILS_BACKEND=mock` -- every test run, and every laptop -- starts paying a multi-second
  import and needing a wheel it has no reason to have. That is exactly how the scientific stack
  becomes a hard dependency of the mock path without anyone deciding it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ocr_common.testing import TEST_API_KEY

SERVICE = Path(__file__).resolve().parents[1]

CODE = """
import importlib.abc, sys

BLOCKED = {blocked!r}

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in BLOCKED:
            raise ModuleNotFoundError(f"{{root}} is not available in this check")
        return None

sys.meta_path.insert(0, Block())
import app.main
import app.ml.kk_quality
"""


def _import_app_without(*blocked: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "API_KEY": TEST_API_KEY,
        "ENVIRONMENT": "local",
        "GUARDRAILS_BACKEND": "mock",
        "PYTHONPATH": str(SERVICE),
    }
    code = CODE.format(blocked=set(blocked))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=SERVICE, env=env)


def test_the_app_imports_without_sqlalchemy():
    result = _import_app_without("sqlalchemy")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("heavy", ["torch", "torchvision", "cv2", "xgboost", "scipy", "fitz"])
def test_the_app_imports_without_the_scientific_stack(heavy):
    result = _import_app_without(heavy)
    assert result.returncode == 0, result.stderr


def test_the_app_imports_without_any_of_them_at_once():
    result = _import_app_without("sqlalchemy", "torch", "torchvision", "cv2", "xgboost", "scipy", "fitz")
    assert result.returncode == 0, result.stderr

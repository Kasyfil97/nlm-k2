"""Writes a service's OpenAPI document (`openapi.yaml`): `python -m ocr_common.web.openapi` in the service folder."""

import importlib
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI


def spec_text(app: FastAPI) -> str:
    """The app's OpenAPI schema as YAML, in a stable key order."""
    return yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True, width=100)


def main(module: str = "app.main", target: str = "openapi.yaml") -> None:
    """Import `module`, render its `app`, and write `target` in the current directory."""
    sys.path.insert(0, str(Path.cwd()))
    app = importlib.import_module(module).app
    path = Path.cwd() / target
    # `newline` eksplisit: di Windows `write_text` menerjemahkan LF jadi CRLF, dan berkas ini ikut ke
    # build context Docker. `.gitattributes` menormalkan yang MASUK git, bukan yang ada di pohon kerja,
    # jadi tanpa ini setiap regenerasi menulis CRLF ke pohon kerja kelima service.
    path.write_text(spec_text(app), encoding="utf-8", newline="\n")
    print(f"ditulis: {path}")


if __name__ == "__main__":
    main()

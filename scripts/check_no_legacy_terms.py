"""Gerbang R6: tidak ada sisa istilah pipeline lama di lingkup yang sedang diperiksa.

Kenapa ini skrip dan bukan satu baris `grep`: gerbangnya butuh **lingkup** dan **pengecualian**,
dan keduanya harus tertulis di satu tempat yang bisa dijalankan, bukan tersebar di prosa.

Lingkupnya penting. Gerbang R6 memisahkan fase 0 dari fase 1, jadi ia hanya bisa menuntut kebersihan
atas apa yang fase 0 miliki: lapis akar, `libs/`, `db/`, `deploy/`, `.github/`, `api/`, dan kedua
service stub. Ketiga service agen dibersihkan agennya masing-masing di fase 1 dan diperiksa oleh
definisi selesai R24; `scripts/smoke_e2e.py` dan pembangun gateway milik Unit 10. Menuntut semuanya
bersih sebelum fase 1 adalah syarat yang tidak mungkin dipenuhi.

    python scripts/check_no_legacy_terms.py               # lingkup fondasi (gerbang R6)
    python scripts/check_no_legacy_terms.py orchestrator  # satu service (gerbang R24)
    python scripts/check_no_legacy_terms.py all           # seluruh repo (gerbang fase 2)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"npwp", re.IGNORECASE)

FOUNDATION = (
    "Makefile",
    "pyproject.toml",
    "docker-compose.yml",
    "docker-compose.db.yml",
    ".dockerignore",
    ".gitignore",
    ".gitattributes",
    "README.md",
    "libs/",
    "db/",
    "deploy/",
    ".github/",
    "api/",
    # scripts/ ada di sini sejak temuan agen B: `smoke_e2e.py` mengirim `notnpwp.jpg` selama
    # enam unit dan gerbang ini tidak pernah melihatnya, karena berkas gerbangnya sendiri
    # tinggal di direktori yang tidak dicakupnya.
    "scripts/",
    "services/structuring/",
    "services/scoring/",
)

SERVICES = ("orchestrator", "guardrails", "ekstraksi", "structuring", "scoring")

# Sebutan yang disengaja, masing-masing dengan alasannya. Menyebut nama repo asal atau menamai uji
# menurut apa yang dicegahnya bukan sisa yang terlewat.
ALLOWED: dict[str, str] = {
    "README.md": "menyebut repo asal yang strukturnya disalin",
    "libs/ocr_common/tests/test_kk_shapes.py": "uji yang justru memastikan bentuk lama tidak lolos",
    "libs/ocr_common/tests/test_gateway_spec.py": "mendeteksi spec yang belum dikonversi; ia harus menyebut istilahnya",
    "scripts/check_no_legacy_terms.py": "berkas gerbangnya sendiri: ia memuat polanya dan pesannya",
}

# `docs/` memuat kontrak, requirements, rencana, dan catatan keputusan; semuanya membandingkan
# dengan pipeline lama secara sengaja.
SKIP_PREFIXES = ("docs/",)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def in_scope(path: str, scope: str) -> bool:
    if path.startswith(SKIP_PREFIXES) or path in ALLOWED:
        return False
    if scope == "all":
        return True
    if scope == "foundation":
        return path.startswith(FOUNDATION)
    return path.startswith(f"services/{scope}/")


def main() -> int:
    scope = sys.argv[1] if len(sys.argv) > 1 else "foundation"
    if scope not in ("foundation", "all", *SERVICES):
        print(f"lingkup tidak dikenal: {scope}. Pilih foundation, all, atau salah satu dari {', '.join(SERVICES)}")
        return 2

    hits: list[tuple[str, int, str]] = []
    for path in tracked_files():
        if not in_scope(path, scope):
            continue
        file = ROOT / path
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            if PATTERN.search(path):
                hits.append((path, 0, "<nama berkas>"))
            continue
        if PATTERN.search(path):
            hits.append((path, 0, "<nama berkas>"))
        for number, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line):
                hits.append((path, number, line.strip()[:100]))

    if not hits:
        print(f"lingkup '{scope}': bersih ({len(ALLOWED)} sebutan yang disengaja dikecualikan)")
        return 0

    print(f"lingkup '{scope}': {len(hits)} sisa NPWP\n")
    for path, number, line in hits:
        where = f"{path}:{number}" if number else path
        print(f"  {where}\n      {line}")
    print(
        "\nKalau salah satunya memang disengaja, tambahkan ke ALLOWED di skrip ini beserta alasannya "
        "-- jangan longgarkan polanya."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

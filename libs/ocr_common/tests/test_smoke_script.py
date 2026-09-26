"""Guards on `scripts/smoke_e2e.py` that do not need a running stack.

The smoke test is the only thing that proves the five services work together, and it is also the
only thing nothing else checks: it runs by hand, against compose, and a path that stops being
called fails silently by not failing. These tests are cheap and they catch the two ways that
happens — a check function that exists but is never run, and the R25a guard being weakened.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "smoke_e2e.py"

#: Every path the plan's Unit 10 names, by the function that walks it. Eight, not seven: §7.4 has
#: three rejecting rules and rules 2 and 3 share a `reject_reason`, so only the stored result tells
#: them apart — testing two of the three leaves whichever one broke undetected.
PATHS = {
    "async_pipeline",
    "member_count",
    "wait_timeout",
    "guardrails_reject",
    "validity_gate",
    "stage_failure",
    "dead_letter",
    "outcome_table",
}


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_e2e", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_path_is_defined(smoke):
    missing = sorted(name for name in PATHS if not callable(getattr(smoke, name, None)))
    assert not missing, f"jalur hilang dari skrip: {missing}"


def test_every_path_is_actually_run(smoke):
    """A function that is defined but never reached the `checks` list is a path nobody walks, and
    it looks exactly like a passing one from the outside."""
    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split("        checks = [", 1)
    assert len(body) == 2, "daftar `checks` di main() tidak ketemu; uji ini perlu diperbarui"
    listed = body[1].split("]", 1)[0]
    unwired = sorted(name for name in PATHS if f", {name})" not in listed)
    assert not unwired, f"jalur ada tetapi tidak dipanggil main(): {unwired}"


def test_the_local_only_guard_refuses_a_remote_target(smoke, monkeypatch):
    """R25a. The script writes rows and deletes them again, including in a table that belongs to
    another team in production. Pointing it at anything but local compose is how you delete
    somebody else's rows."""
    monkeypatch.setitem(smoke.URLS, "orchestrator", "https://ocr.example.internal")
    assert smoke._all_local(None) is False


def test_the_local_only_guard_refuses_a_non_local_environment(smoke, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert smoke._all_local(None) is False


def test_the_local_only_guard_passes_for_compose(smoke, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    assert smoke._all_local(None) is True


def test_every_minted_request_id_is_recorded_for_cleanup(smoke):
    """`cleanup()` can only delete what `_rid()` recorded, so a path that mints its own id leaves
    rows behind — in the outcome table most of all, which this repo does not own."""
    source = SCRIPT.read_text(encoding="utf-8")
    after_helper = source.split("def _rid() -> str:", 1)[1]
    assert 'f"REQ_{uuid.uuid4()}"' not in after_helper.split("return request_id", 1)[1], (
        "ada request_id yang dibuat di luar _rid(); barisnya tidak akan ikut terhapus"
    )


def test_no_tracked_file_carries_crlf():
    """`.gitattributes` says `* text=auto eol=lf`, but that normalises what goes INTO git, not the
    working tree — and the working tree is the Docker build context.

    Two generators have written CRLF here already (`ocr_common.web.openapi`, fixed in freeze-2, and
    `scripts/build_gateway_openapi.py`, fixed after it did the same thing to the gateway spec).
    Both were invisible in diffs, because git normalises on read too. This is the check that makes
    the third one visible on the day it lands.
    """
    import subprocess

    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    offenders = [name for name in listed if name and (ROOT / name).is_file() and b"\r\n" in (ROOT / name).read_bytes()]
    assert not offenders, f"CRLF di pohon kerja: {offenders[:10]}"

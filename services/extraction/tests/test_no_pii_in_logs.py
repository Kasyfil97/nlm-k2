"""R30: the success path must not put an OCR line or a field value in the log.

This matters more here than anywhere else in the pipeline. Structuring holds the *named* fields;
this stage holds the **raw recognised text of the whole card** -- every NIK, every name, the
parents, the dates, the address, all of it, before anything has been selected or dropped. Logs
travel further than any of that should: they are shipped, indexed, retained and read by people with
no reason to see a family's details. The one rule is that a log line may carry the `request_id` and
never the content behind it.

The test walks what the read actually produced rather than a list of key names, because the failure
it guards against is a new `logger.info(result)` somewhere -- not a field anyone thought about.
"""

import logging
from collections.abc import Mapping
from typing import Any

from ocr_common.testing import image_upload
from ocr_common.web.logging import RequestIdFilter

from app.ml.mock import BLANK_TRIGGER, ERROR_TRIGGER
from tests.conftest import JPEG
from tests.test_kk_ocr_stage import result_of, submit

RID = "REQ_ocr_pii"


def _logged(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _secrets(result: Mapping[str, Any]) -> set[str]:
    """Every recognised string this read produced, plus the words inside them.

    Whole lines and their parts both: a line dumped verbatim and a name pulled out of one are the
    same leak, and only the second survives a `logger.info("read %s", box["text"].split()[1])`.
    """
    values: set[str] = set()
    for box in result["texts"]:
        values.add(box["text"])
        values.update(word for word in box["text"].replace(":", " ").split() if len(word) > 3)
    return values


def test_the_success_path_logs_no_ocr_line_and_no_field_value(client, auth, caplog):
    with caplog.at_level(logging.DEBUG):
        assert submit(client, auth, RID).status_code == 202
        job = result_of(client, RID)

    assert job["status"] == "DONE"
    assert job["result"]["texts"], "the text did travel; it just may not be logged"

    logged = _logged(caplog)
    leaked = sorted(value for value in _secrets(job["result"]) if value in logged)
    assert not leaked, f"OCR text in the log: {leaked}"


def test_the_failure_path_logs_no_ocr_line_either(client, auth, caplog):
    """The path most likely to log "what went wrong", and therefore the one where a payload dump
    gets added. A failure here has nothing to say about the card's content."""
    with caplog.at_level(logging.DEBUG):
        submit(client, auth, f"{RID}_fail", filename=f"{ERROR_TRIGGER}-kk.jpg")
        job = result_of(client, f"{RID}_fail")

    assert job["status"] == "FAILED"
    # The model never got to produce text here, so the comparison is against what the same bytes
    # *would* read as -- the point being that nothing of the sort may appear even on this path.
    from app.ml.mock import MockOcrEngine

    would_have_read = MockOcrEngine().read("kk.jpg", JPEG)
    logged = _logged(caplog)
    assert not [value for value in _secrets(would_have_read) if value in logged]


def test_an_empty_read_logs_no_count_confusion_and_still_says_nothing(client, auth, caplog):
    with caplog.at_level(logging.DEBUG):
        submit(client, auth, f"{RID}_blank", filename=f"{BLANK_TRIGGER}-kk.jpg")
        job = result_of(client, f"{RID}_blank")

    assert job["result"]["texts"] == []
    assert "KARTU KELUARGA" not in _logged(caplog)


class CrashingEngine:
    """A backend that raises something the stage does not expect, so the job is logged as crashed."""

    name = "crashing"

    def read(self, filename: str, content: bytes, content_type: str | None = None) -> dict:
        raise RuntimeError("the detector segfaulted")


def test_a_crashed_job_logs_a_traceback_and_still_no_card_content(client, auth, caplog, use_engine):
    """`logger.exception` prints the traceback, frames and all. That is the one place a payload
    reaches a log without anybody writing a log statement for it -- a local variable holding the
    read, or an exception message built from a line. A KK's text must not be in there."""
    use_engine(CrashingEngine())
    with caplog.at_level(logging.DEBUG):
        submit(client, auth, f"{RID}_crash")
        job = result_of(client, f"{RID}_crash")

    assert job["status"] == "FAILED"
    crashed = [record for record in caplog.records if "crashed" in record.getMessage()]
    assert crashed, "the crash was logged; the point is what the line carries"

    from app.ml.mock import MockOcrEngine

    logged = "\n".join(record.getMessage() + (record.exc_text or "") for record in caplog.records)
    assert not [value for value in _secrets(MockOcrEngine().read("kk.jpg", JPEG)) if value in logged]


def test_the_request_id_is_the_one_thing_that_may_be_logged(client, auth, caplog, use_engine):
    """The rule is not "log nothing". Without a correlation id a log is useless, and an operator
    who cannot correlate will reach for the payload instead.

    `bind_request_id` puts the id in a contextvar for the whole background job and
    `RequestIdFilter` fills the field from it, so a line written anywhere inside the job has it.
    The crash path is used because it is the one this stage logs on its own and synchronously.
    """
    use_engine(CrashingEngine())
    with caplog.at_level(logging.DEBUG):
        submit(client, auth, f"{RID}_corr")
        result_of(client, f"{RID}_corr")

    [line] = [record for record in caplog.records if "crashed" in record.getMessage()]
    assert f"{RID}_corr" in line.getMessage(), "the id is in the message itself, not only in the field"
    RequestIdFilter().filter(line)
    assert line.request_id == f"{RID}_corr"


def test_a_refused_upload_does_not_log_the_bytes(client, auth, caplog):
    """A 400 at intake has the document in hand and no result to talk about, which is exactly when
    "here is what I got" gets logged."""
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/v1/extraction/jobs",
            headers=auth,
            data={"request_id": f"{RID}_bad", "guardrails": "[1,2,3]"},
            files=image_upload("kk.jpg", JPEG),
        )

    assert "fake-jpeg-bytes" not in _logged(caplog)

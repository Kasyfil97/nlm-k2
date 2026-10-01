"""R30: the success path must not put a field value or an OCR line in the log.

This service handles a whole Kartu Keluarga -- a KK number, up to fifteen people with their NIK,
names, parents and dates. Logs travel further than any of them should: they are shipped, indexed,
retained and read by people with no reason to see a family's details. The one rule is that a log
line may carry the `request_id` and never the content behind it.

The test walks the fixture household rather than checking a list of key names, because the failure
this guards against is a new `logger.info(result)` somewhere, not a field anyone thought about.
"""

import logging

from ocr_common.testing import image_upload
from ocr_common.web.logging import RequestIdFilter

from tests.conftest import HOUSEHOLD, JPEG, NOMOR_KK, STRUCTURING_RESULT

RID = "OCR_pii"


def _secrets() -> set[str]:
    """Every value that appears in the fixture result and must never appear in a log line."""
    values = {NOMOR_KK}
    for field in STRUCTURING_RESULT.values():
        if isinstance(field, dict) and field.get("value"):
            values.add(field["value"])
    for person in HOUSEHOLD:
        values.update(value for value in vars(person).values() if isinstance(value, str) and len(value) > 2)
    return values


def test_the_success_path_logs_no_field_value(client, auth, caplog):
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/v1/extract-ocr",
            headers=auth,
            data={"request_id": RID},
            files=image_upload("kk.jpg", JPEG),
        )
    assert response.status_code == 200
    assert response.json()["data"]["no_kk"]["value"] == NOMOR_KK, "the value did travel; it just may not be logged"

    logged = "\n".join(record.getMessage() for record in caplog.records)
    leaked = sorted(value for value in _secrets() if value in logged)
    assert not leaked, f"field values in the log: {leaked}"


def test_the_request_id_is_the_one_thing_that_may_be_logged(client, auth, caplog, settings_override):
    """The rule is not 'log nothing'. Without a correlation id a log is useless, and an operator
    who cannot correlate will reach for the payload instead.

    The success path writes nothing at all, so this takes a path that does write -- guardrails left
    out by the `pipeline_name_sequence` -- and checks what that line carries. `RequestIdFilter` fills
    the field from the contextvar, so a line written anywhere inside the request already has it."""
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/v1/extract-ocr",
            headers=auth,
            data={"request_id": RID, "pipeline_name_sequence": ["ekstraksi", "structuring", "scoring"]},
            files=image_upload("kk.jpg", JPEG),
        )

    [warning] = [record for record in caplog.records if "guardrails left out" in record.getMessage()]
    RequestIdFilter().filter(warning)
    assert warning.request_id == RID
    assert RID in warning.getMessage(), "and the id is in the message itself, not only in the field"


def test_a_rejected_document_logs_no_field_value_either(client, auth, caplog):
    """The rejection path is the one most likely to log 'what went wrong', which is exactly where
    a payload dump gets added."""
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/v1/extract-ocr",
            headers=auth,
            data={"request_id": RID},
            files=image_upload("notkk.jpg", JPEG),
        )
    assert response.status_code == 400

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert not [value for value in _secrets() if value in logged]

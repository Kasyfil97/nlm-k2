"""R30: nothing about the document may reach a log line.

Guardrails never parses a Kartu Keluarga, so it holds no field values -- but it holds the two
things that arrive with them, and both are easy to log by accident: the **file name**, which the
caller chooses and which in a bank's storage is routinely built from a customer identifier, and the
**image bytes** themselves. Logs are shipped, indexed, retained and read by people with no reason
to see either. The one thing a line may carry is the `request_id`.

The failure this guards against is the natural next commit on the error path: `logger.warning("could
not assess %s", filename)` reads like good operability and quietly publishes an account number.

Every value here comes from `ocr_common.synthetic_kk` (province `99`, which Indonesia never
assigns), so a leak in a test log is not a leak of anyone's data.
"""

import logging

from ocr_common.synthetic_kk import household, nomor_kk
from ocr_common.testing import image_upload
from ocr_common.web.logging import RequestIdFilter

from tests.conftest import JPEG, unassessable_model

RID = "OCR_pii"

PERSON = household(1)[0]
NOMOR_KK = nomor_kk()

#: Everything that must never appear in a log line. The NIK and the KK number are in the file name
#: and in the bytes at once, because both routes into a log are real.
SECRETS = (NOMOR_KK, PERSON.nik, PERSON.nama_lengkap)

FILENAME = f"{NOMOR_KK}-{PERSON.nik}.jpg"
CONTENT = JPEG + PERSON.nama_lengkap.encode() + PERSON.nik.encode()


def _leaked(caplog) -> list[str]:
    logged = "\n".join(record.getMessage() for record in caplog.records)
    return sorted(value for value in SECRETS if value in logged)


def test_the_success_path_logs_nothing_about_the_document(client, auth, caplog):
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/v1/guardrails/check",
            headers=auth,
            data={"request_id": RID},
            files=image_upload(FILENAME, CONTENT),
        )
    assert response.status_code == 200
    assert response.json()["data"]["passed"] is True
    assert not _leaked(caplog), f"document data in the log: {_leaked(caplog)}"


def test_the_rejection_path_logs_nothing_either(client, auth, caplog):
    """The path most likely to grow a "what went wrong" line, which is where a dump gets added."""
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/v1/guardrails/check",
            headers=auth,
            data={"request_id": RID},
            files=image_upload(f"blur-{FILENAME}", CONTENT),
        )
    assert response.json()["data"]["passed"] is False
    assert not _leaked(caplog)


def test_the_unassessable_path_logs_the_reason_but_not_the_document(client, auth, caplog, use_model):
    """R14a *does* log: an operator has to be able to see that images are arriving unreadable. The
    line carries the model's own description of the failure and nothing from the file."""
    use_model(unassessable_model())
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/v1/guardrails/check",
            headers={**auth, "X-Request-ID": RID},
            data={"request_id": RID},
            files=image_upload(FILENAME, CONTENT),
        )
    assert response.json()["data"]["document"]["verdict"] == "unassessable"

    [line] = [record for record in caplog.records if "could not assess" in record.getMessage()]
    assert not _leaked(caplog)
    # And the id is there to correlate with, because an operator who cannot correlate reaches for
    # the payload instead. It comes from the `X-Request-ID` header through the contextvar that
    # `RequestIdFilter` reads, not from the form field, which is only echoed in the response.
    RequestIdFilter().filter(line)
    assert line.request_id == RID

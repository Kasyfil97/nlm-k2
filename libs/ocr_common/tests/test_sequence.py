"""`pipeline_name_sequence`, ported from nilam with this repo's name for the OCR stage (`ekstraksi`)."""

import pytest

from ocr_common.pipeline import (
    DEFAULT_SEQUENCE,
    InMemoryJobRepository,
    InvalidSequence,
    chain,
    checked_sequence,
    next_service,
    validate_sequence,
)
from ocr_common.pipeline.callbacks import result_callback_body, stage_callback_body

from tests.test_result_callback import RID, _final


@pytest.mark.parametrize(
    "sequence",
    [
        ["guardrails", "ekstraksi", "structuring", "scoring"],
        ["guardrails", "ekstraksi", "structuring"],
        ["guardrails", "ekstraksi"],
        ["guardrails"],
        ["ekstraksi", "structuring", "scoring"],
        ["ekstraksi", "structuring"],
        ["ekstraksi"],
    ],
)
def test_guardrails_may_be_left_out_and_the_end_cut_off(sequence):
    assert validate_sequence(sequence) == tuple(sequence)


@pytest.mark.parametrize(
    ("sequence", "reason"),
    [
        (["ekstraksi", "scoring"], "without skipping one in the middle"),
        (["guardrails", "ekstraksi", "scoring"], "without skipping one in the middle"),
        (["guardrails", "structuring"], "without skipping one in the middle"),
        (["structuring", "scoring"], "structuring cannot come first"),
        (["scoring"], "scoring cannot come first"),
        (["ekstraksi", "guardrails"], "without skipping one in the middle"),
        (["guardrails", "guardrails", "ekstraksi"], "listed twice"),
        # nilam's name for the stage is not this repo's: refused rather than silently mapped.
        (["guardrails", "extraction"], "unknown service 'extraction'"),
    ],
)
def test_a_skipped_middle_a_wrong_order_or_an_unknown_name_is_refused(sequence, reason):
    with pytest.raises(InvalidSequence, match=reason):
        validate_sequence(sequence)


def test_no_sequence_is_the_full_pipeline():
    assert validate_sequence(None) == validate_sequence([]) == DEFAULT_SEQUENCE
    assert DEFAULT_SEQUENCE == ("guardrails", "ekstraksi", "structuring", "scoring")
    assert next_service(None, "structuring") == "scoring"


def test_next_service_is_none_for_the_last_one():
    assert next_service(["guardrails", "ekstraksi", "structuring"], "ekstraksi") == "structuring"
    assert next_service(["guardrails", "ekstraksi", "structuring"], "structuring") is None
    with pytest.raises(InvalidSequence, match="does not include scoring"):
        next_service(["ekstraksi"], "scoring")


def test_a_stage_checks_that_it_is_part_of_the_sequence():
    assert checked_sequence(None, "scoring") is None
    assert checked_sequence(("ekstraksi", "structuring"), "structuring") == ["ekstraksi", "structuring"]
    with pytest.raises(InvalidSequence):
        checked_sequence(["guardrails", "ekstraksi"], "structuring")


def test_chain_hands_on_or_ends_the_request_with_the_result_as_it_is():
    def handoff(result):
        return {"handed": result}

    assert chain(None, "ekstraksi", handoff) == {"handoff_payload": handoff, "next_stage": "STRUCTURING"}
    assert chain(["ekstraksi", "structuring", "scoring"], "structuring", handoff)["next_stage"] == "SCORING"
    last = chain(["guardrails", "ekstraksi"], "ekstraksi", handoff)
    assert "next_stage" not in last and "handoff_payload" not in last
    assert last["callback_result"]({"texts": []}) == last["outcome_data"]({"texts": []}) == {"texts": []}


def test_the_result_callback_completes_on_the_final_stage_with_its_result_as_it_is():
    ocr = {"texts": [], "text_regions_count": 0}
    final = stage_callback_body(RID, "OCR", "DONE", result=ocr, final=True)
    not_final = stage_callback_body(RID, "OCR", "DONE", result=ocr)

    assert final["final"] is True and "final" not in not_final
    assert result_callback_body(final) == {"request_id": RID, "status": "completed", "result": ocr, "guardrails": {}}
    assert result_callback_body(not_final) is None


def test_a_scoring_callback_queued_before_the_final_flag_still_completes():
    """Outbox rows written before the deploy carry no `final`; SCORING still ends the request, with
    the KK projection rather than the raw result."""
    completed = result_callback_body(stage_callback_body(RID, "SCORING", "DONE", result=_final()))

    assert completed is not None and completed["status"] == "completed"
    assert completed["result"]["no_kk"]["value"] == "3273012345678901"


async def test_a_job_record_carries_the_sequence_it_was_submitted_with():
    repository = InMemoryJobRepository()
    await repository.claim("REQ_1", input={"pipeline_name_sequence": ["ekstraksi"]})
    await repository.claim("REQ_2", input={"document_type": "kk"})

    first, second = await repository.get("REQ_1"), await repository.get("REQ_2")

    assert first is not None and first["pipeline_name_sequence"] == ["ekstraksi"]
    assert second is not None and second["pipeline_name_sequence"] is None

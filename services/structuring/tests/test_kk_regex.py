"""The `kk_regex` backend: the vendored K2Regex-v2 parser reading real box geometry.

The point of every test here is the one thing `mock` could not do -- produce a value that came from
the submitted document. So the assertions are about *provenance*, not plausibility: change what the
boxes say and the answer has to change with them, and a document that is not a Kartu Keluarga has to
be refused rather than answered with a perfect one.

Geometry comes from `kk_boxes`, content from `ocr_common.synthetic_kk`. Nothing here needs a
service, a database or the network.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from ocr_common.kk import DOC_FIELDS, MEMBER_FIELDS
from ocr_common.synthetic_kk import household
from ocr_common.types import OcrBox

from app.ml import kk_regex
from app.ml.kk_regex import KKRegexStructurer
from app.ml.mock import NO_TEXT as MOCK_NO_TEXT
from app.ml.mock import NOT_A_KK as MOCK_NOT_A_KK
from app.ml.validity import NO_TEXT, NOT_A_KK
from app.vendor import kk_layout_parser
from tests.kk_boxes import box, kk_boxes

VENDOR = Path(kk_layout_parser.__file__).parent


@pytest.fixture(scope="module")
def structurer() -> KKRegexStructurer:
    return KKRegexStructurer()


def values(result: dict) -> dict:
    """The result flattened to `{field: value}`, members as a list. Easier to compare than nesting."""
    flat = {name: result[name]["value"] for name in DOC_FIELDS}
    flat["anggota"] = [{name: member[name]["value"] for name in MEMBER_FIELDS} for member in result["anggota_keluarga"]]
    return flat


# --- the vendored copy ---------------------------------------------------------------------


def test_the_vendored_parser_is_the_file_the_adapter_says_it_is():
    """The pin in `kk_regex`'s docstring against the bytes on disk.

    A vendored snapshot whose recorded hash has drifted from its content is worse than none: the
    provenance note then vouches for a file nobody checked. The upstream hash cannot be verified
    from here (the source repository is not a dependency), so what is checked is the copy.
    """
    claimed = re.search(r"hashes\n\s*`([0-9a-f]{64})`", kk_regex.__doc__ or "")
    assert claimed, "kk_regex's docstring no longer records the vendored parser's sha256"
    actual = hashlib.sha256((VENDOR / "kk_layout_parser.py").read_bytes()).hexdigest()
    assert actual == claimed.group(1), (
        "kk_layout_parser.py was edited in place. The parser is a vendored snapshot: change it "
        "upstream and re-vendor, or the next refresh silently reverts the edit."
    )


def test_the_template_travels_with_the_parser():
    """`kk_template.json` has to sit beside the parser, because that is where its loader looks.

    Without it the loader answers `{}` and the parser falls back to header-only column boundaries:
    it still returns a plausible card, just a less accurate one. That is the failure mode this
    checks for -- silent, and invisible in any single document.
    """
    template = json.loads((VENDOR / "kk_template.json").read_text(encoding="utf-8"))
    assert "v17" in template and "v15" in template
    assert kk_layout_parser.load_template(), "the parser cannot find the template next to itself"


def test_the_backend_refuses_to_start_without_its_template(monkeypatch):
    """A missing template is a start-up failure, not a quiet accuracy loss."""
    monkeypatch.setattr(kk_layout_parser, "_TEMPLATE", {})
    monkeypatch.setattr(kk_layout_parser, "_TEMPLATE_DIMUAT", True)
    with pytest.raises(RuntimeError, match="kk_template.json"):
        KKRegexStructurer()


# --- provenance: the answer comes from the boxes ----------------------------------------------


def test_two_different_cards_give_two_different_answers(structurer):
    """The single defect that made `mock` unusable as a backend.

    `mock` generates a household, so two different cards came back byte-identical -- and the check
    that would have caught it is this one. It is the cheapest test in the file and the only one
    whose failure means the stage is reading nothing at all.
    """
    first = values(structurer.structure(kk_boxes(seed=0)))
    second = values(structurer.structure(kk_boxes(seed=7)))
    assert first["nomor_kk"] != second["nomor_kk"]
    assert first["anggota"][0]["nik"] != second["anggota"][0]["nik"]
    assert first["anggota"][0]["nama_lengkap"] != second["anggota"][0]["nama_lengkap"]


def test_every_cell_lands_in_the_column_it_was_printed_in(structurer):
    """A whole card, field by field. This is the column assignment, not the recognition."""
    people = household(2, seed=0)
    result = values(structurer.structure(kk_boxes(people, no_kk="9924187486671285")))

    assert result["nomor_kk"] == "9924187486671285"
    assert result["nama_kepala_keluarga"] == people[0].nama_lengkap
    assert result["alamat"] == "KP SUKAMAJU"
    assert (result["rt"], result["rw"]) == ("016", "004")
    assert result["kecamatan"] == "CISAYONG"
    assert result["kabupaten_kota"] == "TASIKMALAYA"
    assert result["provinsi"] == "JAWA BARAT"
    assert result["kode_pos"] == "46153"
    assert result["tanggal_dikeluarkan"] == "29-09-2021"

    assert len(result["anggota"]) == 2
    for person, member in zip(people, result["anggota"], strict=True):
        for name in ("nama_lengkap", "nik", "jenis_kelamin", "tempat_lahir", "agama", "ayah", "ibu"):
            assert member[name] == getattr(person, name), name
        assert member["status_hubungan_dalam_keluarga"] == person.status_hubungan_dalam_keluarga
        assert member["kewarganegaraan"] == "WNI"


def test_a_closed_vocabulary_is_normalised_to_its_canonical_value(structurer):
    """`SD/SEDERAJAT` is what cards print; `TAMAT SD/SEDERAJAT` is what Dukcapil calls it.

    Normalisation is a property of the parser worth pinning here, because it is the reason a
    consumer can match on the value at all -- and because it means the output is deliberately not
    a transcription.
    """
    people = household(2, seed=0)
    assert people[0].pendidikan == "SD/SEDERAJAT"
    result = values(structurer.structure(kk_boxes(people)))
    assert result["anggota"][0]["pendidikan"] == "TAMAT SD/SEDERAJAT"


def test_a_member_added_to_the_card_is_a_member_in_the_answer(structurer):
    """Household size follows the boxes. An off-by-one here shifts every downstream confidence."""
    for size in (1, 3, 5):
        result = values(structurer.structure(kk_boxes(household(size, seed=3))))
        assert len(result["anggota"]) == size


# --- the frozen §7.3 shape ---------------------------------------------------------------------


def test_every_field_carries_the_frozen_shape(structurer):
    result = structurer.structure(kk_boxes())
    assert set(result) == set(DOC_FIELDS) | {"anggota_keluarga", "reject_reason"}
    cells = [result[name] for name in DOC_FIELDS]
    cells += [member[name] for member in result["anggota_keluarga"] for name in MEMBER_FIELDS]
    for cell in cells:
        assert set(cell) == {"value", "ocr_conf", "crf_conf", "features"}
        assert isinstance(cell["value"], str)
        for score in ("ocr_conf", "crf_conf"):
            assert cell[score] is None or 0.0 <= cell[score] <= 1.0
        if not cell["value"]:
            assert cell["ocr_conf"] is None and cell["crf_conf"] is None


def test_document_fields_never_carry_a_crf_score_and_member_fields_usually_do(structurer):
    """The two families the trust model is split along, asserted at the source.

    Document fields are found by regex and position and never pass through Viterbi, so a `crf_conf`
    on one would mean the adapter had copied a score from somewhere it does not belong.
    """
    result = structurer.structure(kk_boxes())
    assert all(result[name]["crf_conf"] is None for name in DOC_FIELDS)
    filled = [member[name] for member in result["anggota_keluarga"] for name in MEMBER_FIELDS if member[name]["value"]]
    assert filled and sum(cell["crf_conf"] is not None for cell in filled) > len(filled) / 2


def test_no_field_carries_a_feature_vector_yet(structurer):
    """Deliberate, and worth a failing test the day it changes.

    The parser does not emit `kk.MEMBER_CELL_FEATURES`; the values exist inside its Viterbi and are
    discarded. `calibrated` scores a vectorless field `None` rather than guessing, so today every
    confidence from this backend is null. When the parser is instrumented, this test is the one that
    should break.
    """
    result = structurer.structure(kk_boxes())
    cells = [result[name] for name in DOC_FIELDS]
    cells += [member[name] for member in result["anggota_keluarga"] for name in MEMBER_FIELDS]
    assert all(cell["features"] is None for cell in cells)


# --- §7.4, the validity gate ---------------------------------------------------------------


def test_the_gate_reasons_are_the_ones_the_mock_reports():
    """Both backends answer the same 400, so both must say the same thing."""
    assert (NO_TEXT, NOT_A_KK) == (MOCK_NO_TEXT, MOCK_NOT_A_KK)


def test_no_boxes_at_all_is_rule_one(structurer):
    result = structurer.structure([])
    assert result["reject_reason"] == NO_TEXT
    assert result["anggota_keluarga"] == []
    assert all(result[name]["value"] == "" for name in DOC_FIELDS)


def test_boxes_the_parser_cannot_use_are_rule_one_too(structurer):
    """Boxes arrived but none had usable geometry: there is no text to judge, so it is not rule two.

    Blaming the document for a blank page would send back "this is not a Kartu Keluarga", which
    tells the submitter to check the wrong thing.
    """
    unusable: list[OcrBox] = [{"text": "", "score": 0.9, "poly": [[0.0, 0.0]] * 4}]
    assert structurer.structure(unusable)["reject_reason"] == NO_TEXT


def test_a_document_that_is_not_a_kartu_keluarga_is_refused(structurer):
    """The case the stage exists for, and the one `mock` answered 200 with a perfect card."""
    letter = [
        box("SURAT KETERANGAN DOMISILI USAHA", 300.0, 40.0, 720.0),
        box("Yang bertanda tangan di bawah ini menerangkan bahwa:", 60.0, 90.0, 560.0),
        box("Nama Usaha", 60.0, 120.0, 140.0),
        box(": WARUNG SEMBAKO MAJU", 200.0, 120.0, 420.0),
        box("Demikian surat keterangan ini dibuat untuk dipergunakan seperlunya.", 60.0, 200.0, 620.0),
    ]
    result = structurer.structure(letter)
    assert result["reject_reason"] == NOT_A_KK
    assert result["anggota_keluarga"] == [] or not any(
        m["nik"]["value"] and m["nama_lengkap"]["value"] for m in result["anggota_keluarga"]
    )


def test_a_card_with_no_kk_number_is_refused(structurer):
    """Rule two. The members read fine; the number that identifies the household does not."""
    result = structurer.structure(kk_boxes(no_kk=""))
    assert result["reject_reason"] == NOT_A_KK


def test_a_card_whose_rows_carry_no_nik_and_name_is_refused(structurer):
    """Rule three. The header is a Kartu Keluarga, the table is unreadable."""
    blank_people = [replace(person, nama_lengkap="", nik="") for person in household(2, seed=0)]
    result = structurer.structure(kk_boxes(blank_people))
    assert result["reject_reason"] == NOT_A_KK


def test_a_readable_card_is_not_refused(structurer):
    assert structurer.structure(kk_boxes())["reject_reason"] is None

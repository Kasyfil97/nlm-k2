"""Rule four of the §7.4 validity gate: every contract column header must be on the card.

A KK whose right side is cut off or covered keeps its No. KK and NIK + nama, so rules two and three pass
it, while the cut column comes out truncated with a confident score. The header is what goes missing.
"""

from __future__ import annotations

import pytest

from app.ml.kk_regex import KKRegexStructurer
from app.ml.validity import INCOMPLETE_KK, NOT_A_KK, missing_headers, reject_reason
from tests.kk_boxes import box, kk_boxes

HEADER_BOXES = {
    "pekerjaan": "Jenis Pekerjaan",
    "pendidikan": "Pendidikan",
    "hubungan": "Status Hubungan Dalam Keluarga",
    "ayah": "Ayah",
    "ibu": "Ibu",
}


def boxes_of(*texts: str):
    return [box(text, 10.0 + 120.0 * i, 10.0) for i, text in enumerate(texts)]


@pytest.fixture(scope="module")
def structurer() -> KKRegexStructurer:
    return KKRegexStructurer()


def test_a_complete_card_has_every_header(structurer):
    assert missing_headers(kk_boxes()) == []
    assert structurer.structure(kk_boxes())["reject_reason"] is None


@pytest.mark.parametrize("name", list(HEADER_BOXES))
def test_a_card_missing_one_header_is_refused(structurer, name):
    boxes = [b for b in kk_boxes() if b["text"] != HEADER_BOXES[name]]
    assert missing_headers(boxes) == [name]
    assert structurer.structure(boxes)["reject_reason"] == INCOMPLETE_KK


@pytest.mark.parametrize(
    "texts",
    [
        ("JENIS POKERJAAN", "PANDIDIKAN", "STATUS HUBUNGEN DALAM", "AYAN", "LBU"),
        ("Jenis Pekenaan", "PENDIDKAN", "Status Huburgan", "Ayah:", "1BU"),
    ],
)
def test_common_ocr_misreads_of_a_header_still_count(texts):
    assert missing_headers(boxes_of(*texts)) == []


@pytest.mark.parametrize("lookalike", ["BURUH HARIAN LEPAS", "IBLAM", "KOTA TASIKMALAYA", "DUASMANSYAH"])
def test_field_values_are_not_mistaken_for_a_short_header(lookalike):
    """A three-letter fuzzy match finds IBU in BURUH and AYAH in -SYAH: on a card cut just left of the
    Ibu column the header would be 'found' in the data and the cut would pass."""
    found = missing_headers(boxes_of("PEKERJAAN", "PENDIDIKAN", "HUBUNGAN", lookalike))
    assert found == ["ayah", "ibu"]


def test_the_earlier_rules_still_come_first():
    members = [{"nik": {"value": ""}, "nama_lengkap": {"value": ""}}]
    assert reject_reason("", members, []) == NOT_A_KK
    assert reject_reason("3206011234567890", members, []) == NOT_A_KK


def test_without_boxes_rule_four_is_skipped():
    """`mock` passes no boxes: its texts are levers, not a card."""
    members = [{"nik": {"value": "3206011234567890"}, "nama_lengkap": {"value": "SITI"}}]
    assert reject_reason("3206011234567890", members) is None
    assert reject_reason("3206011234567890", members, []) == INCOMPLETE_KK

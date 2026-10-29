"""`postprocess`: the household head's label read into the same box as the name.

The inputs are the shapes found in `raw_ocr_v6`, where 128 of 1184 documents have the label and the
name in one box and `kk_model` returned the label as part of the name in 127 of them.
"""

import pytest

from app.ml.postprocess import postprocess, strip_label
from app.ml.validity import field


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Nama Kepala Keluarga : ANTON", "ANTON"),
        ("Nama Kepala Keluarga: MIMI ANDRIYANTI", "MIMI ANDRIYANTI"),
        ("Nama Kepala Keluarga ANTON", "ANTON"),
        ("NamaKepalaKeluarga:ANTON", "ANTON"),
        ("Nama Kepala Keluarga ; H.A CHATIB", "H.A CHATIB"),
        ("Narna Kepala Keluarga : DRS H RIsDJAN SUGANDI", "DRS H RIsDJAN SUGANDI"),
        ("Nama Kepala Keluanga : VICTOR WANDAMANI", "VICTOR WANDAMANI"),
        ("Nama Kapala <aluarga : ENTIS SUTISNA, S.Rd", "ENTIS SUTISNA, S.Rd"),
        ("Nama Kepala Keluarga : IR. MAMAD NANO", "IR. MAMAD NANO"),
        ("Nama Kepala KeluargaANTON", "ANTON"),
        ("Tae Nama Kepala Keluarga", ""),
        ("Nama Kepala Keluarga :", ""),
    ],
)
def test_the_label_is_cut_off_the_name(text, name):
    assert strip_label(text) == name


@pytest.mark.parametrize(
    "name",
    ["ANTON", "NAMA SAPUTRA", "KEPALA DESA SUKAMAJU", "MUHAMMAD KEMAL KELANA", "Nama", ""],
)
def test_a_name_without_the_label_is_left_alone(name):
    assert strip_label(name) == name


def test_the_head_keeps_its_scores_and_features_when_cleaned():
    features = {"has_label": 1.0}
    result = {"nama_kepala_keluarga": field("Nama Kepala Keluarga : ANTON", 0.97, None, features)}
    head = postprocess(result)["nama_kepala_keluarga"]
    assert head == {"value": "ANTON", "ocr_conf": 0.97, "crf_conf": None, "features": features}
    assert result["nama_kepala_keluarga"]["value"] == "Nama Kepala Keluarga : ANTON"


def test_a_label_without_a_name_is_an_empty_field():
    result = {"nama_kepala_keluarga": field("Nama Kepala Keluarga :", 0.97, None, {"has_label": 1.0})}
    assert postprocess(result)["nama_kepala_keluarga"] == field("")


def test_an_answer_without_a_head_passes_through():
    result = {"nama_kepala_keluarga": field("")}
    assert postprocess(result) is result

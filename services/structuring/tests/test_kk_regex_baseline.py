"""Does the vendored parser still extract what K2Regex-v2 measured it extracting?

`test_kk_regex.py` asks whether the adapter reads a card. This file asks something the adapter
cannot answer: whether the *parser* still behaves as the corpus figures describe. The two are
different questions, and only this one catches a bad re-vendor, a stray configuration file, or a
"harmless" edit -- all of which leave a parser that reads a card perfectly well, slightly worse.

The instrument is K2Regex-v2's own value baseline: the sha256 of `structure(raw, debug=False)` for
200 documents. **It stores hashes, not output**, which is what makes it committable -- the corpus is
1184 real Kartu Keluarga and never enters a repository. It lives beside this one
(`<repo>/../raw_ocr_v6`) and these tests skip when it is absent.

The configuration tests below do not need the corpus and always run. They are the ones that would
have caught this port's first version, which treated a resolvable `kk_kolom_model.json` as good news
and logged it as "trained".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from ocr_common.types import OcrBox

from app.ml.kk_regex import KKRegexStructurer
from app.vendor import kk_layout_parser as KK

REPO_ROOT = Path(__file__).resolve().parents[3]
#: Beside the checkout, never inside it: these are real cards.
CORPUS_DIR = REPO_ROOT.parent / "raw_ocr_v6"
BASELINE = Path(__file__).parent / "fixtures" / "kk_parser_value_baseline.json"

corpus_required = pytest.mark.skipif(
    not CORPUS_DIR.is_dir() or not any(CORPUS_DIR.glob("*.json")),
    reason=f"corpus raw_ocr_v6 absent at {CORPUS_DIR} (real KK, kept out of the repository)",
)


def parser_input(path: Path) -> list:
    """A `raw_ocr_v6` file to the parser's `[[poly, [text, score]], ...]`.

    Deliberately the same adaptation `kk_regex._adapt` performs, spelled out again rather than
    imported: the corpus files are the OCR stage's own output, so going through the adapter would
    make this test agree with the adapter by construction instead of with the baseline.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    return [
        [box["poly"], [box["text"], float(box.get("score") or 0.0)]]
        for page in document.get("pages") or []
        for box in page.get("texts") or []
        if box.get("poly") and box.get("text") is not None
    ]


# --- configuration: no corpus needed -------------------------------------------------------


def test_the_template_resolves_and_covers_both_layout_variants():
    template = KK.load_template()
    assert template and {"v17", "v15"} <= set(template)


def test_column_assignment_runs_through_the_crf():
    """`CRF_KOLOM` off swaps the Viterbi for plain x-range membership, which can place a row's
    fifth box in `ayah` and its sixth in `pendidikan` -- an order the card cannot print."""
    assert KK.CRF_KOLOM is True


def test_the_columns_are_the_nine_and_eight_the_card_prints():
    assert len(KK.COLS_T1) == 9
    assert len(KK.COLS_T2) == 8


def test_a_trained_column_model_is_a_fault_not_an_upgrade():
    """The counter-intuitive one, and the reason this file exists.

    Hand-tuned emission weights are the configuration the 89.02% figure was measured in. A
    resolvable `kk_kolom_model.json` silently substitutes trained pointwise weights and costs about
    2.4 points, with no other signal. The first version of this port read the same condition and
    logged it approvingly as "trained".
    """
    assert not KK.load_model_kolom(), "a kk_kolom_model.json is resolvable; it must not be"


def test_the_backend_refuses_to_start_when_a_trained_model_appears(monkeypatch):
    monkeypatch.setattr(KK, "load_model_kolom", lambda: {"bobot": [1.0]})
    with pytest.raises(RuntimeError, match="kk_kolom_model.json"):
        KKRegexStructurer()


def test_the_backend_refuses_to_start_with_the_crf_off(monkeypatch):
    monkeypatch.setattr(KK, "CRF_KOLOM", False)
    with pytest.raises(RuntimeError, match="CRF_KOLOM"):
        KKRegexStructurer()


def test_the_start_up_failure_names_every_problem_at_once(monkeypatch):
    """One restart per fault is one restart too many when three can be reported together."""
    monkeypatch.setattr(KK, "CRF_KOLOM", False)
    monkeypatch.setattr(KK, "load_model_kolom", lambda: {"bobot": [1.0]})
    monkeypatch.setattr(KK, "load_template", dict)
    with pytest.raises(RuntimeError) as raised:
        KKRegexStructurer()
    message = str(raised.value)
    assert "kk_template.json" in message and "CRF_KOLOM" in message and "kk_kolom_model.json" in message


# --- value preservation: corpus-backed -----------------------------------------------------


def test_the_baseline_is_the_one_k2regex_pinned():
    """The fixture is copied, so its provenance has to be checkable without the source repository."""
    meta = json.loads(BASELINE.read_text(encoding="utf-8"))["_meta"]
    assert meta["n_docs"] == 200
    assert (
        meta["parser_sha256"] in (KK.__doc__ or "")
        or meta["parser_sha256"] in Path(KK.__file__).read_text(encoding="utf-8")[:600]
    ), "the baseline was taken against a different parser than the one vendored here"


@corpus_required
def test_the_vendored_parser_reproduces_the_baseline_exactly():
    """200 documents, hash for hash. Not "close": identical.

    A field-accuracy threshold would let a regression hide behind the corpus average. Hashing the
    whole result per document means one changed character in one cell of one card fails the test,
    which is the sensitivity a vendored snapshot needs.
    """
    hashes = json.loads(BASELINE.read_text(encoding="utf-8"))["hashes"]
    checked, drifted = 0, []
    for document_id, expected in hashes.items():
        source = CORPUS_DIR / f"{document_id}.json"
        if not source.exists():
            continue
        output = KK.structure(parser_input(source), debug=False)
        canonical = json.dumps(output, sort_keys=True, ensure_ascii=False)
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != expected:
            drifted.append(document_id)
        checked += 1
    if checked == 0:
        # ty 0.0.81 does not know `pytest.skip`'s keyword; same ignore as test_gateway_spec.py.
        pytest.skip(reason="the corpus holds none of the baseline documents")  # ty: ignore[unknown-argument]
    assert not drifted, f"{len(drifted)}/{checked} documents drifted from the baseline: {drifted[:5]}"


@corpus_required
def test_asking_for_confidences_does_not_change_a_single_value():
    """`debug=True` is not a debugging flag here -- it is how the adapter gets `crf_conf` at all.

    So the pipeline runs the parser in a mode the baseline was NOT taken in. What has to hold is
    that the mode adds `_meta` and touches nothing else; otherwise every number in the baseline
    describes a code path production never takes.
    """
    for source in sorted(CORPUS_DIR.glob("*.json"))[:25]:
        raw = parser_input(source)
        quiet = KK.structure(raw, debug=False)
        loud = KK.structure(raw, debug=True)
        assert "_meta" not in quiet
        assert {key: value for key, value in loud.items() if key != "_meta"} == quiet, source.name


@corpus_required
def test_every_confidence_the_adapter_copies_is_aligned_and_in_range():
    """`_meta.crf_conf` is read positionally against `anggota_keluarga`, so a length mismatch would
    attribute one person's confidence to another -- silently, since both are plausible numbers."""
    seen = 0
    for source in sorted(CORPUS_DIR.glob("*.json"))[:50]:
        result = KK.structure(parser_input(source), debug=True)
        members = result.get("anggota_keluarga") or []
        if not members:
            continue
        seen += 1
        meta = result["_meta"]
        assert len(meta["crf_conf"]) == len(members), source.name
        assert len(meta["conf"]["anggota_keluarga"]) == len(members), source.name
        for row in meta["crf_conf"]:
            for field, value in row.items():
                assert value is None or 0.0 <= value <= 1.0, f"{source.name}: {field}={value}"
    assert seen, "no corpus document produced members"


@corpus_required
def test_the_adapter_carries_the_parser_through_unchanged_on_real_cards():
    """The end of the chain: what `kk_regex` returns still holds what the parser found.

    Values equal, member count equal, nothing dropped. This is the one test that would catch an
    adapter bug the synthetic fixture cannot -- a real card has empty cells, merged cells and
    columns the parser never placed, and those are the paths where a mapping quietly loses a field.
    """
    structurer = KKRegexStructurer()
    compared = 0
    for source in sorted(CORPUS_DIR.glob("*.json"))[:25]:
        raw = parser_input(source)
        parsed = KK.structure(raw, debug=True)
        if not parsed.get("anggota_keluarga"):
            continue
        boxes = [cast(OcrBox, {"text": text, "score": score, "poly": poly}) for poly, (text, score) in raw]
        adapted = structurer.structure(boxes)
        assert len(adapted["anggota_keluarga"]) == len(parsed["anggota_keluarga"]), source.name
        assert adapted["nomor_kk"]["value"] == parsed["nomor_kk"], source.name
        for mine, theirs in zip(adapted["anggota_keluarga"], parsed["anggota_keluarga"], strict=True):
            for name in ("nama_lengkap", "nik", "pendidikan", "jenis_pekerjaan", "ayah", "ibu"):
                assert mine[name]["value"] == theirs[name], f"{source.name}: {name}"
        compared += 1
    assert compared, "no corpus document produced members"

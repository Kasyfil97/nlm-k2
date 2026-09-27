# `app/vendor` — code copied in, not written here

Everything in this directory arrived from somewhere else and is kept **byte-for-byte** as delivered,
apart from CRLF → LF, which this repository requires. It is excluded from `ruff`, `ty` and
`pyrefly` (see the root `pyproject.toml` and this service's), because formatting or re-typing a
snapshot destroys the one property that makes it worth vendoring: that it is the same code, running
the same way, as the corpus it was measured against.

**Do not edit these files.** Change them upstream and re-vendor. An edit made here survives until
the next refresh and then silently disappears.

## `kk_layout_parser.py` + `kk_template.json`

Layout-aware structuring of a Kartu Keluarga from OCR boxes: zoning by the `(1)..(17)` markers,
column boundaries from fuzzy header matching snapped to empty corridors, row anchoring on the serial
column, a Viterbi over the column assignment with forward–backward marginals, normalisation to the
canonical Dukcapil vocabularies, and cross-validation of each NIK against the birth date and sex.

| | |
|---|---|
| from | `K2Regex-v2/src/services/kk_layout_parser.py` |
| upstream sha256 | `1515c5dbd08c89c094a25586fdbaf90793967836346b194e7b7618718808732d` |
| vendored sha256 | `7cd62c4f5466b6fe7fc5963075f7e96b4bc1ea28c0d6cca586b4b0ac04677048` |
| template upstream | `2ed7fa34e8dc362e8497c45c8be4dc009a3f2c20a68039caf44dc7dc83cdb36b` |
| template vendored | `0c108595800c96bd8785b857f03d83b5ab22caaf4b27284a1e6212e52b164f6e` |

The upstream file is itself a vendored snapshot, of
`docs/brainstorms/2026-09-22-kk-full-schema-parser.py`, pinned there by content hash — so the true
source is that prototype, and a refresh should start from whichever of the two has moved.

`kk_template.json` holds the per-variant column priors (`v17` and `v15`). The parser looks for it
next to its own source file, so the two must stay together; `tests/test_kk_regex.py` fails if they
do not, because without the template the column boundaries fall back to headers alone and accuracy
drops without anything going wrong visibly.

`kk_kolom_model.json` — trained pointwise emission weights — must **not** be present. This reads
backwards and is the parser's sharpest edge: its 89.02% field accuracy is the number for the
*hand-tuned* weights, i.e. for `load_model_kolom()` finding nothing. A stray copy anywhere on the
parser's search path (its own directory, the working directory, or up to three levels above either)
silently swaps the trained weights in and drops accuracy to about 86.65%, with no other signal at
all. K2Regex-v2 fails startup on it, and `app/ml/kk_regex.py` does the same; if a trained model is
ever the better one, that is a re-vendor, not a file someone drops in.

The adapter that maps this into the pipeline's §7.3 shape is
[`app/ml/kk_regex.py`](../ml/kk_regex.py); it is ordinary repository code and is linted and typed
like the rest.

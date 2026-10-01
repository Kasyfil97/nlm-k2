"""A Kartu Keluarga as §7.1 boxes: the shape extraction hands to structuring.

`kk_regex` is a *layout* parser, so a fixture that is only a list of strings tests nothing -- the
whole question is whether the right text lands in the right column. These boxes carry real geometry:
the column positions, the `(1)..(17)` markers, the two tables and the identity block of an actual
card, measured once off a real photograph and then emptied of it.

**The geometry is real; none of the content is.** Every value comes from `ocr_common.synthetic_kk`,
whose NIKs carry the unassigned province code 99, so no fixture here can collide with a living
person. Coordinates carry no such risk: a column boundary is the same on every card printed.

The layout is Dukcapil's `v17` variant (markers up to `(17)`), which the parser's `layout_variant`
detects and which its template has entries for.
"""

from __future__ import annotations

from ocr_common.synthetic_kk import SyntheticMember, household, nomor_kk
from ocr_common.types import OcrBox

#: Rough glyph advance of the card's print, in the same pixel frame as the coordinates below. Only
#: the left edge of a value really matters to the parser -- that is what its emission scores read --
#: but a box still has to have a plausible width, or `estimate_shear` and the row bands see nothing.
CHAR_W = 5.6
LINE_H = 11.0

#: Data-cell left edges, table 1. Measured from a real card; see the module docstring.
T1_X = {
    "nama_lengkap": 50.0,
    "nik": 237.0,
    "jenis_kelamin": 324.0,
    "tempat_lahir": 375.0,
    "tanggal_lahir": 528.0,
    "agama": 571.0,
    "pendidikan": 660.0,
    "jenis_pekerjaan": 809.0,
    "golongan_darah": 959.0,
}
T2_X = {
    "status_perkawinan": 48.0,
    "tanggal_perkawinan": 184.0,
    "status_hubungan_dalam_keluarga": 242.0,
    "kewarganegaraan": 348.0,
    "ayah": 597.0,
    "ibu": 819.0,
}

#: Header cells, as printed: the parser finds each by fuzzy match and takes its right edge as a
#: first estimate of the column boundary, so they are laid out centred over their column the way
#: the card does it, not left-aligned with the values.
T1_HEADERS = [
    ("No", 33.0, 51.0),
    ("Nama Lengkap", 104.0, 182.0),
    ("NIK", 268.0, 292.0),
    ("Jenis Kelamin", 328.0, 371.0),
    ("Tempat Lahir", 418.0, 483.0),
    ("Tanggal Lahir", 528.0, 569.0),
    ("Agama", 596.0, 634.0),
    ("Pendidikan", 704.0, 760.0),
    ("Jenis Pekerjaan", 841.0, 922.0),
    ("Golongan Darah", 957.0, 1009.0),
]
T2_HEADERS = [
    ("No", 25.0, 44.0),
    ("Status Perkawinan", 83.0, 146.0),
    ("Tanggal Perceraian", 184.0, 241.0),
    ("Status Hubungan Dalam Keluarga", 251.0, 340.0),
    ("Kewarganegaraan", 352.0, 441.0),
    ("No. Paspor", 452.0, 511.0),
    ("No. KITAP", 528.0, 585.0),
    ("Ayah", 691.0, 722.0),
    ("Ibu", 905.0, 927.0),
]

#: Marker centres, `(1)..(9)` then `(10)..(17)`. These are what `marker_groups` zones the page with,
#: and what `column_ranges` registers the template against, so they matter as much as the headers.
T1_MARKERS = [143.0, 279.0, 349.0, 450.0, 548.0, 614.0, 733.0, 882.0, 984.0]
T2_MARKERS = [114.0, 212.0, 295.0, 395.0, 480.0, 557.0, 705.0, 916.0]

#: The identity block: label on the left column or the right one, value beside it.
IDENTITY = [
    ("Nama Kepala Keluarga", 136.0, 253.0, 265.0, 71.0),
    ("Alamat", 134.0, 176.0, 265.0, 83.0),
    ("RT/RW", 136.0, 177.0, 266.0, 95.0),
    ("Kode Pos", 133.0, 187.0, 265.0, 106.0),
    ("Desa/Kelurahan", 668.0, 751.0, 774.0, 75.0),
    ("Kecamatan", 668.0, 729.0, 774.0, 87.0),
    ("Kabupaten/Kota", 668.0, 751.0, 774.0, 100.0),
    ("Provinsi", 668.0, 711.0, 774.0, 112.0),
]

T1_TOP, T2_TOP, ROW_PITCH = 168.0, 366.0, 14.0
SCORE = 0.99


def box(text: str, x0: float, y0: float, x1: float | None = None, score: float = SCORE) -> OcrBox:
    """One §7.1 box: an axis-aligned quad, four points, clockwise from the top left."""
    right = x1 if x1 is not None else x0 + max(len(text), 1) * CHAR_W
    bottom = y0 + LINE_H
    return {
        "text": text,
        "score": score,
        "poly": [[x0, y0], [right, y0], [right, bottom], [x0, bottom]],
    }


def kk_boxes(
    members: list[SyntheticMember] | None = None,
    *,
    no_kk: str | None = None,
    seed: int = 0,
    tanggal_dikeluarkan: str = "29-09-2021",
    alamat: str = "KP SUKAMAJU",
    desa_kelurahan: str = "SUKAMAJU",
    rt_rw: str = "016/004",
    kode_pos: str = "46153",
    kecamatan: str = "CISAYONG",
    kabupaten_kota: str = "TASIKMALAYA",
    provinsi: str = "JAWA BARAT",
) -> list[OcrBox]:
    """A complete card. `members` defaults to a two-person household from `synthetic_kk`."""
    people = household(2, seed=seed) if members is None else members
    head = nomor_kk(seed) if no_kk is None else no_kk
    values = {
        "Nama Kepala Keluarga": people[0].nama_lengkap if people else "",
        "Alamat": alamat,
        "RT/RW": rt_rw,
        "Kode Pos": kode_pos,
        "Desa/Kelurahan": desa_kelurahan,
        "Kecamatan": kecamatan,
        "Kabupaten/Kota": kabupaten_kota,
        "Provinsi": provinsi,
    }

    boxes = [box("KARTU KELUARGA", 361.0, 11.0, 682.0)]
    if head:
        boxes.append(box(f"No.{head}", 348.0, 46.0, 690.0))
    for label, lx0, lx1, vx0, y in IDENTITY:
        boxes.append(box(label, lx0, y, lx1))
        boxes.append(box(f": {values[label]}", vx0, y + 2.0))

    for table, headers, markers, columns, top in (
        (1, T1_HEADERS, T1_MARKERS, T1_X, T1_TOP),
        (2, T2_HEADERS, T2_MARKERS, T2_X, T2_TOP),
    ):
        header_y = top - 32.0
        for text, hx0, hx1 in headers:
            boxes.append(box(text, hx0, header_y, hx1))
        first = 1 if table == 1 else 10
        for offset, centre in enumerate(markers):
            label = f"({first + offset})"
            boxes.append(box(label, centre - 8.0, top - 12.0, centre + 8.0))
        # The serial column runs 1..10 whether or not the rows are filled, exactly as printed: the
        # parser anchors its row bands on it, so a card with two people still shows ten numbers.
        for index in range(10):
            y = top + index * ROW_PITCH
            boxes.append(box(str(index + 1), 33.0, y + 3.0, 46.0))
        for index, person in enumerate(people):
            y = top + index * ROW_PITCH
            for name, x0 in columns.items():
                value = getattr(person, name, "")
                if value:
                    boxes.append(box(value, x0, y))

    boxes.append(box("Dikeluarkan Tanggal:", 27.0, 535.0, 141.0))
    boxes.append(box(tanggal_dikeluarkan, 171.0, 538.0, 233.0))
    return boxes

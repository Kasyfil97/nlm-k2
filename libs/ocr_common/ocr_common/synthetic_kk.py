"""Synthetic Kartu Keluarga values for mocks, fixtures and smoke tests.

Nothing here may be copied from a real card. A NIK is not just an identifier, it is structured
(region, birth date, sequence), so a hand-typed one is easy to get subtly wrong *and* easy to get
accidentally right -- sixteen plausible digits can collide with a living person's.

The defence is the **region code**: every value generated here starts with province `99`, which
Indonesia does not assign. The result passes a shape check and looks like a NIK to a parser, but it
cannot be anyone's. Everything else is derived deterministically from a seed, so fixtures are stable
and a diff means someone changed the generator, not the weather.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: Unassigned province code. Real ones run 11-94, so nothing generated here can be a real NIK.
SYNTHETIC_PROVINCE = "99"

_GIVEN = ("BUDI", "SITI", "AGUS", "DEWI", "RIZKY", "INDAH", "FAJAR", "NURUL", "HENDRA", "LESTARI")
_FAMILY = ("SANTOSO", "NURHALIZA", "WIJAYA", "PRATAMA", "ANGGRAINI", "SETIAWAN", "MAHARANI", "KUSUMA")
_BIRTHPLACE = ("BANDUNG", "GARUT", "CIMAHI", "SUMEDANG", "CIANJUR", "SUKABUMI")
_EDUCATION = ("SD/SEDERAJAT", "SLTP/SEDERAJAT", "SLTA/SEDERAJAT", "D-III", "S1", "S2")
_WORK = ("KARYAWAN SWASTA", "MENGURUS RUMAH TANGGA", "PELAJAR/MAHASISWA", "WIRASWASTA", "PEGAWAI NEGERI SIPIL")
#: Card order: the head comes first, then the spouse, then children.
_RELATION = ("KEPALA KELUARGA", "ISTRI", "ANAK", "ANAK", "ANAK", "FAMILI LAIN")


@dataclass(frozen=True)
class SyntheticMember:
    """One printed row of a card, all fifteen columns (structuring emits seven of them)."""

    nama_lengkap: str
    nik: str
    jenis_kelamin: str
    tempat_lahir: str
    tanggal_lahir: str
    agama: str
    pendidikan: str
    jenis_pekerjaan: str
    golongan_darah: str
    status_perkawinan: str
    tanggal_perkawinan: str
    status_hubungan_dalam_keluarga: str
    kewarganegaraan: str
    ayah: str
    ibu: str


def nomor_kk(seed: int = 0) -> str:
    """A 16-digit KK number under the unassigned province code."""
    return f"{SYNTHETIC_PROVINCE}{_digits(seed, 'kk', 14)}"


def nik(seed: int, *, female: bool, birth: tuple[int, int, int]) -> str:
    """A 16-digit NIK: `99` + regency/district + DDMMYY + sequence.

    Women carry `day + 40`, which is how a real NIK encodes sex; keeping that true means a parser
    that reads sex from the NIK behaves the same on synthetic data as on a real card.
    """
    day, month, year = birth
    region = _digits(seed, "region", 4)
    encoded_day = day + 40 if female else day
    sequence = _digits(seed, "seq", 4)
    return f"{SYNTHETIC_PROVINCE}{region}{encoded_day:02d}{month:02d}{year % 100:02d}{sequence}"


def member(index: int, *, seed: int = 0) -> SyntheticMember:
    """Member `index` of a household, in card order."""
    female = index == 1 or (index > 1 and index % 2 == 0)
    birth = (1 + (index * 7 + seed) % 28, 1 + (index * 5 + seed) % 12, 1960 + (index * 11 + seed) % 45)
    given = _GIVEN[(index + seed) % len(_GIVEN)]
    family = _FAMILY[seed % len(_FAMILY)]
    married = index < 2
    return SyntheticMember(
        nama_lengkap=f"{given} {family}",
        nik=nik(index + seed * 100, female=female, birth=birth),
        jenis_kelamin="PEREMPUAN" if female else "LAKI-LAKI",
        tempat_lahir=_BIRTHPLACE[(index + seed) % len(_BIRTHPLACE)],
        tanggal_lahir=f"{birth[0]:02d}-{birth[1]:02d}-{birth[2]}",
        agama="ISLAM",
        pendidikan=_EDUCATION[(index * 3 + seed) % len(_EDUCATION)],
        jenis_pekerjaan=_WORK[(index * 2 + seed) % len(_WORK)],
        golongan_darah="O" if index % 3 else "-",
        status_perkawinan="KAWIN" if married else "BELUM KAWIN",
        tanggal_perkawinan="08-08-2010" if married else "",
        status_hubungan_dalam_keluarga=_RELATION[min(index, len(_RELATION) - 1)],
        kewarganegaraan="WNI",
        ayah=f"{_GIVEN[(index + seed + 4) % len(_GIVEN)]} {family}",
        ibu=f"{_GIVEN[(index + seed + 7) % len(_GIVEN)]} {_FAMILY[(seed + 3) % len(_FAMILY)]}",
    )


def household(members: int = 2, *, seed: int = 0) -> list[SyntheticMember]:
    """A household of `members` rows. `0` is allowed and is the §7.4 rejection case."""
    return [member(index, seed=seed) for index in range(members)]


def _digits(seed: int, salt: str, length: int) -> str:
    """Deterministic digits from a seed.

    sha256 rather than `hash()`: Python randomises string hashing per process, so `hash()` would
    give different fixtures on every run -- the opposite of the stability this module promises.
    Not a security hash; it is only a stable source of digits.
    """
    digest = hashlib.sha256(f"{seed}:{salt}".encode()).hexdigest()
    return f"{int(digest, 16) % (10**length):0{length}d}"

# VENDORED SNAPSHOT — do not edit extraction logic here except the value-preserving
# CRF-confidence plumbing documented in the K2Regex-v2 plan (Unit 3).
# Source : ../../../docs/brainstorms/2026-09-22-kk-full-schema-parser.py
# SHA-256: f7a03fcb9931fb5cce24c2fe92772954eb92f09491ec6836ef1595d380650874
# Pinned : 2026-09-24T07:14:16Z (source dir is not a git repo; pinned by content hash)
"""Layout-aware structuring of a KK (Kartu Keluarga) from PaddleOCR raw output.

Input : result.json["raw_ocr_output"] = [[quad, [text, conf]], ...]
Output: skema KK lengkap (identitas + 17 kolom anggota).

Algoritma (ringkas):
  0. normalisasi box -> (x0,x1,y0,y1,cx,cy,text,conf)
  1. ZONING   : marker "(1)..(9)" dan "(10)..(17)" memotong halaman jadi
                identitas / tabel-1 / tabel-2 / footer
  2. KOLOM    : header cell dicari fuzzy (kiri->kanan, monoton);
                batas kolom = header.x1, lalu di-SNAP ke koridor kosong pada
                proyeksi-x nilai sel (header rata-tengah, nilai rata-kiri)
  3. BARIS    : nomor urut 1..10 di kolom paling kiri = anchor baris;
                setiap box diikat ke pita baris dgn overlap-y maksimum,
                + koreksi offset per-kolom (kolom yang tercetak agak turun)
  4. SEL      : token dalam (baris, kolom) digabung urut x
  5. NORMALISASI: kolom bervocab tertutup di-fuzzy-match ke nilai kanonik Dukcapil
  6. VALIDASI : NIK (DDMMYY, +40 utk perempuan) dicek silang ke tanggal lahir &
                jenis kelamin; nama kepala keluarga diambil konsensus 4 sumber

Pure stdlib.
"""
from __future__ import annotations

import collections
import json
import math
import re
import statistics
import sys
from difflib import SequenceMatcher

# ---------------------------------------------------------------- vocabularies
JENIS_KELAMIN = ["LAKI-LAKI", "PEREMPUAN"]
AGAMA = ["ISLAM", "KRISTEN", "KATOLIK", "HINDU", "BUDHA", "KHONGHUCU",
         "KEPERCAYAAN TERHADAP TUHAN YME"]
GOLONGAN_DARAH = ["A", "B", "AB", "O", "A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-",
                  "TIDAK TAHU"]
PENDIDIKAN = ["TIDAK/BELUM SEKOLAH", "BELUM TAMAT SD/SEDERAJAT", "TAMAT SD/SEDERAJAT",
              "SLTP/SEDERAJAT", "SLTA/SEDERAJAT", "DIPLOMA I/II",
              "AKADEMI/DIPLOMA III/S. MUDA", "DIPLOMA IV/STRATA I", "STRATA II", "STRATA III"]
# Daftar jenis pekerjaan Dukcapil (SIAK), 89 nilai kanonik.
# URUTAN = kelaziman di korpus (dihitung dari 1184 sampel raw_ocr), bukan urutan
# administratif: normalize() memakai urutan ini untuk memutus kemenangan tipis,
# jadi nilai yang lazim harus di depan. Dengan urutan resmi, "ARASVASTA" jatuh ke
# KARYAWAN SWASTA (no. 15) alih-alih WIRASWASTA (no. 87) yang 204x lebih sering.
PEKERJAAN = [
    # -- muncul di korpus, dari yang paling sering
    "PELAJAR/MAHASISWA", "BELUM/TIDAK BEKERJA", "MENGURUS RUMAH TANGGA",
    "KARYAWAN SWASTA", "WIRASWASTA", "PEGAWAI NEGERI SIPIL",
    "TENTARA NASIONAL INDONESIA", "PENSIUNAN", "GURU", "KARYAWAN HONORER",
    "KEPOLISIAN RI", "KARYAWAN BUMN", "PETANI/PEKEBUN", "BURUH HARIAN LEPAS",
    "PELAUT", "BIDAN", "PEDAGANG", "KARYAWAN BUMD", "PERAWAT", "PENDETA", "DOSEN",
    "PERANGKAT DESA", "SOPIR", "WARTAWAN", "DOKTER", "TRANSPORTASI",
    "PEMBANTU RUMAH TANGGA", "LAINNYA", "TUKANG BATU", "TUKANG SOL SEPATU",
    "APOTEKER", "PENELITI", "NELAYAN/PERIKANAN", "NOTARIS", "PENATA BUSANA",
    "PENATA RIAS", "PENGACARA",
    # -- sah menurut Dukcapil tapi belum pernah muncul di korpus
    "PERDAGANGAN", "PETERNAK", "INDUSTRI", "KONSTRUKSI", "BURUH TANI/PERKEBUNAN",
    "BURUH NELAYAN/PERIKANAN", "BURUH PETERNAKAN", "TUKANG CUKUR", "TUKANG LISTRIK",
    "TUKANG KAYU", "TUKANG LAS/PANDAI BESI", "TUKANG JAHIT", "TUKANG GIGI",
    "PENATA RAMBUT", "MEKANIK", "SENIMAN", "TABIB", "PARAJI", "PERANCANG BUSANA",
    "PENTERJEMAH", "IMAM MASJID", "PASTOR", "USTADZ/MUBALIGH", "JURU MASAK",
    "PROMOTOR ACARA", "KEPALA DESA", "PILOT", "ARSITEK", "AKUNTAN", "KONSULTAN",
    "PSIKIATER/PSIKOLOG", "PENYIAR TELEVISI", "PENYIAR RADIO", "PIALANG",
    "PARANORMAL", "BIARAWATI", "ANGGOTA DPR-RI", "ANGGOTA DPD", "ANGGOTA BPK",
    "ANGGOTA DPRD PROVINSI", "ANGGOTA DPRD KABUPATEN/KOTA",
    "ANGGOTA MAHKAMAH KONSTITUSI", "ANGGOTA KABINET/KEMENTERIAN", "DUTA BESAR",
    "GUBERNUR", "WAKIL GUBERNUR", "BUPATI", "WAKIL BUPATI", "WALIKOTA",
    "WAKIL WALIKOTA", "PRESIDEN", "WAKIL PRESIDEN",
]
# Bentuk yang tercetak di kartu tapi bukan nilai kanonik (singkatan dalam kurung).
PEKERJAAN_CETAK = ["PEGAWAI NEGERI SIPIL (PNS)", "PEGAWAI NEGERI SIPIL(PNS)",
                   "TENTARA NASIONAL INDONESIA (TNI)", "KEPOLISIAN RI (POLRI)"]

STATUS_KAWIN = ["BELUM KAWIN", "KAWIN TERCATAT", "KAWIN BELUM TERCATAT", "CERAI HIDUP",
                "CERAI MATI", "KAWIN"]
HUBUNGAN = ["KEPALA KELUARGA", "SUAMI", "ISTRI", "ANAK", "MENANTU", "CUCU", "ORANG TUA",
            "MERTUA", "FAMILI LAIN", "PEMBANTU", "LAINNYA"]
WARGANEGARA = ["WNI", "WNA", "INDONESIA"]          # form lama menulis "INDONESIA"
ALIAS = {"INDONESIA": "WNI",
         "PEGAWAI NEGERI SIPIL (PNS)": "PEGAWAI NEGERI SIPIL",
         "PEGAWAI NEGERI SIPIL(PNS)": "PEGAWAI NEGERI SIPIL",
         "TENTARA NASIONAL INDONESIA (TNI)": "TENTARA NASIONAL INDONESIA",
         "KEPOLISIAN RI (POLRI)": "KEPOLISIAN RI"}

# kolom tabel: (key output, label header di dokumen, vocab utk normalisasi)
COLS_T1 = [
    ("nama_lengkap", "Nama Lengkap", None),
    ("nik", "NIK", None),
    ("jenis_kelamin", "Jenis Kelamin", JENIS_KELAMIN),
    ("tempat_lahir", "Tempat Lahir", None),
    ("tanggal_lahir", "Tanggal Lahir", None),
    ("agama", "Agama", AGAMA),
    ("pendidikan", "Pendidikan", PENDIDIKAN),
    ("jenis_pekerjaan", "Jenis Pekerjaan", PEKERJAAN + PEKERJAAN_CETAK),
    ("golongan_darah", "Golongan Darah", GOLONGAN_DARAH),
]
COLS_T2 = [
    ("status_perkawinan", "Status Perkawinan", STATUS_KAWIN),
    ("tanggal_perkawinan", "Tanggal Perkawinan", None),
    ("status_hubungan_dalam_keluarga", "Status Hubungan Dalam Keluarga", HUBUNGAN),
    ("kewarganegaraan", "Kewarganegaraan", WARGANEGARA),
    ("no_paspor", "No. Paspor", None),
    ("no_kitap", "No. KITAP", None),
    ("ayah", "Ayah", None),
    ("ibu", "Ibu", None),
]


# ------------------------------------------------------------------- utilities
def compact(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def sim(a, b):
    return SequenceMatcher(None, compact(a), compact(b)).ratio()


def normalize(text, vocab, thr=0.62, margin=0.05):
    """Snap ke nilai kanonik kalau cukup mirip; kalau tidak, kembalikan apa adanya.

    Dua pengaman, dipasang setelah daftar pekerjaan dilengkapkan ke 89 nilai
    (makin panjang daftarnya, makin gampang teks rusak tertarik ke nilai langka):

      1. Menang tipis bukan kemenangan. Kalau selisih dengan runner-up <= margin,
         dipilih yang lebih umum (urutan daftar), bukan yang skornya sepersekian
         lebih tinggi -- 'PELAJARAS' pernah jadi PENATA RIAS (0.737) alih-alih
         PELAJAR/MAHASISWA (0.720).
      2. Kemiripan rasio menipu untuk teks pendek: 'PEIN' 0.667 mirip PRESIDEN.
         Kalau skornya pas-pasan (<0.8) dan nilai kanoniknya >= 7 karakter,
         dituntut minimal 6 karakter yang benar-benar cocok. Kecocokan kuat
         tetap lolos: 'PERIART' -> PERAWAT (0.857) tidak ikut terbuang.
    """
    if not text or not vocab:
        return text
    ca = compact(text)
    # Batas atas rasio SequenceMatcher = 2*min(la,lb)/(la+lb). Kandidat yang
    # panjangnya terlalu jauh mustahil lolos ambang, jadi tidak perlu dihitung
    # -- daftar pekerjaan 93 entri jadi tidak membebani tiap sel.
    kand = []
    for i, v in enumerate(vocab):
        lb = len(compact(v))
        if lb and 2.0 * min(len(ca), lb) / (len(ca) + lb) >= thr:
            kand.append((sim(text, v), -i, v))
    scored = sorted(kand, reverse=True)
    layak = []
    for skor, negidx, v in scored:
        if skor < thr:
            break
        cocok = skor * (len(ca) + len(compact(v))) / 2.0     # perkiraan karakter cocok
        if skor < 0.8 and len(compact(v)) >= 7 and cocok < 6:
            continue                                         # mirip tipis DAN buktinya sedikit
        layak.append((skor, negidx, v))
    if not layak:
        return text
    dekat = [x for x in layak if layak[0][0] - x[0] <= margin]
    pilih = min(dekat, key=lambda x: -x[1])                  # index terkecil = paling umum
    return ALIAS.get(pilih[2], pilih[2])


def median(xs, default=0.0):
    xs = list(xs)
    return statistics.median(xs) if xs else default


class Box:
    __slots__ = ("x0", "x1", "y0", "y1", "cx", "cy", "text", "conf")

    def __init__(self, quad, text, conf):
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        self.x0, self.x1, self.y0, self.y1 = min(xs), max(xs), min(ys), max(ys)
        self.cx, self.cy = (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2
        self.text = (text or "").strip()
        self.conf = float(conf)

    def __repr__(self):
        return "%r@(%.0f-%.0f,%.0f)" % (self.text, self.x0, self.x1, self.cy)


def load_boxes(raw):
    out = []
    for item in raw:
        try:
            quad, (text, conf) = item[0], item[1]
            if len(quad) != 4 or not str(text).strip():
                continue
            b = Box(quad, str(text), conf)
            if b.x1 <= b.x0 or b.y1 <= b.y0:
                continue
            out.append(b)
        except Exception:
            continue
    return out


# ------------------------------------------------------------ 0b. deskew
def _row_score(ys, win):
    """Seberapa 'menggerombol' proyeksi-y: makin tinggi makin rapi barisnya."""
    ys = sorted(ys)
    j = k = 0
    score = 0
    for i, y in enumerate(ys):
        while ys[j] < y - win:
            j += 1
        while k < len(ys) and ys[k] <= y + win:
            k += 1
        score += k - j
    return score


def estimate_shear(boxes, span=0.15, step=0.004):
    """Kemiringan baris (dy/dx) lewat projection profile pada pusat box.

    Dipakai geser-vertikal (shear), bukan rotasi: koordinat x tidak berubah
    sehingga logika kolom tetap valid, dan dokumen yang melengkung (foto KK)
    bisa dikoreksi per-tabel dengan kemiringan yang berbeda.
    """
    if len(boxes) < 15:
        return 0.0
    win = median([b.y1 - b.y0 for b in boxes], 10.0) * 0.3
    pts = [(b.cx, b.cy) for b in boxes]
    best, best_s, sl = -1, 0.0, -span
    while sl <= span + 1e-9:
        sc = _row_score([y - sl * x for x, y in pts], win)
        if sc > best:
            best, best_s = sc, sl
        sl += step
    return best_s


def apply_shear(boxes, slope):
    """y' = y - slope*x (x, lebar, tinggi tidak berubah)."""
    if abs(slope) < 0.002:
        return boxes
    out = []
    for b in boxes:
        dy = -slope * b.cx
        out.append(Box([[b.x0, b.y0 + dy], [b.x1, b.y0 + dy],
                        [b.x1, b.y1 + dy], [b.x0, b.y1 + dy]], b.text, b.conf))
    return out


# --------------------------------------------------------- 1. zoning by markers
MARKER = re.compile(r"^\(?(\d{1,2})\)$")


def marker_groups(boxes, u=12.0):
    """Dua baris penanda kolom, dipisah lewat celah-y terbesar.

    Penomorannya TIDAK boleh diasumsikan: KK format lama punya 15 kolom
    (tanpa Golongan Darah dan tanpa Tanggal Perkawinan) sehingga tabel bawah
    bernomor (9)..(15), sedangkan format baru 17 kolom -> (10)..(17).
    Nomor mentah dikembalikan apa adanya; pemetaan ke indeks kolom dikalibrasi
    belakangan terhadap header yang benar-benar terbaca.
    """
    marks = []
    for b in boxes:
        m = MARKER.match(b.text)
        if m:
            marks.append((int(m.group(1)), b))
    marks.sort(key=lambda t: t[1].cy)
    if len(marks) < 4:
        return []
    gaps = [(marks[i + 1][1].cy - marks[i][1].cy, i) for i in range(len(marks) - 1)]
    gap, cut = max(gaps) if gaps else (0, 0)
    groups = [marks[:cut + 1], marks[cut + 1:]] if gap > 1.8 * u else [marks]

    def clean(group):
        if len(group) < 4:
            return {}, None
        cy = median([b.cy for _, b in group])
        h = median([b.y1 - b.y0 for _, b in group], 12.0)
        group = [(n, b) for n, b in group if abs(b.cy - cy) < 3 * h]
        best = {}
        for n, b in group:                           # duplikat -> paling dekat baris
            if n not in best or abs(b.cy - cy) < abs(best[n].cy - cy):
                best[n] = b
        keep, last = {}, -1
        for n, b in sorted(best.items(), key=lambda t: t[1].cx):
            if n > last:                             # x naik -> n harus naik
                keep[n] = b
                last = n
        if len(keep) < 4:
            return {}, None
        return keep, median([b.cy for b in keep.values()])

    return [g for g in (clean(x) for x in groups) if g[0]]


def calibrate_base(marks, headers, default):
    """Nomor marker mana yang berarti 'kolom ke-0'?

    Tiap marker dicocokkan ke header terdekat (dua-duanya rata-tengah di
    kolomnya), lalu selisih nomor-vs-indeks divoting. Ini yang membuat format
    lama (tabel bawah mulai dari "(9)") tidak menggeser seluruh kolom.
    """
    hs = [(i, h) for i, (_, h) in enumerate(headers) if h is not None]
    if not hs or not marks:
        return default
    votes = [n - min(hs, key=lambda t: abs(t[1].cx - b.cx))[0] for n, b in marks.items()]
    return int(round(median(votes, default)))


# ------------------------------------------------------ 2. header & kolom ranges
def find_header(boxes, label, ymin, ymax, min_x=-1.0, saingan=()):
    """Cari sel header lewat kemiripan teks, lalu gabung pecahannya
    ('Jenis' + 'Kelamin' yang tercetak dua baris).

    `saingan` = label kolom lain di tabel yang sama. Beberapa header KK berbagi
    kata depan ("Jenis Pekerjaan" vs "Jenis Kelamin", "Tanggal Lahir" vs
    "Tanggal Perkawinan"), sehingga kotak milik kolom TETANGGA ikut mencetak
    skor lewat jalur per-kata. Kalau dibiarkan, kotak tetangga itu terpilih,
    lalu ditolak min_x, dan kolom yang dicari hilang sama sekali -- 92% kolom
    "Jenis Pekerjaan" yang gagal pada korpus v6 persis karena ini. Kandidat yang
    lebih mirip label lain daripada label yang dicari karena itu dibuang.
    """
    thr = 0.60 if len(compact(label)) <= 6 else 0.65
    cands = [b for b in boxes if ymin <= b.cy <= ymax and b.x0 > min_x
             and not b.text.startswith("(")]
    u = median([b.y1 - b.y0 for b in cands], 12.0)   # satuan = tinggi teks
    words = [w for w in label.split() if len(w) >= 4]

    def skor(b, lab):
        w = [x for x in lab.split() if len(x) >= 4]
        return round(max([sim(b.text.split(":")[0], lab)]
                         + [0.9 * sim(b.text, x) for x in w]), 3)

    scored = [(skor(b, label), b) for b in cands]
    scored = [(s, b) for s, b in scored if s >= thr]
    if saingan:
        scored = [(s, b) for s, b in scored
                  if s >= max((skor(b, lab) for lab in saingan), default=0.0)]
    if not scored:
        return None
    best = max(scored, key=lambda t: (t[0], -t[1].x0))[1]   # seri -> paling kiri
    words = label.split()
    parts = [best] + [
        b for b in cands
        if b is not best and abs(b.cy - best.cy) < 2.0 * u
        and (0 <= best.x0 - b.x1 < 1.2 * u or 0 <= b.x0 - best.x1 < 1.2 * u
             or abs(b.cx - best.cx) < 2.5 * u)
        and max(sim(b.text, w) for w in words) >= 0.7
    ]
    x0, x1 = min(p.x0 for p in parts), max(p.x1 for p in parts)
    y0, y1 = min(p.y0 for p in parts), max(p.y1 for p in parts)
    return Box([[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
               " ".join(p.text for p in sorted(parts, key=lambda p: (p.cy, p.x0))), 1.0)


def blobs(body, pad=1.0):
    """Proyeksi-x nilai sel -> gumpalan interval yang saling tumpang tindih."""
    out = []
    for b in sorted(body, key=lambda b: b.x0):
        if out and b.x0 <= out[-1][1] + pad:
            out[-1][1] = max(out[-1][1], b.x1)
        else:
            out.append([b.x0, b.x1])
    return out


def column_starts(body, tol=6.0):
    """Tepi kiri sel yang mengelompok = awal tiap kolom (isi sel rata-kiri).

    -> [(x_awal, jumlah_box)]. Jumlahnya ikut dibawa karena dipakai sebagai
    bukti: satu box nyasar bisa membentuk "kelompok", sedangkan tepi kiri kolom
    sungguhan didukung banyak baris sekaligus.
    """
    out = []
    for x in sorted(b.x0 for b in body):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [(g[0], len(g)) for g in out]


def snap(est, starts, bl, colw, prev):
    """Rapikan batas kolom hasil taksiran header.

    Header rata-tengah sedangkan isi sel rata-kiri, jadi header.x1 kadang jatuh
    persis di atas teks tetangganya ('Kelamin'.x1=387 vs 'WAPO'.x0=391, atau
    'Tanggal Lahir'.x1=1093 vs 'ISLAM'.x0=1092). Urutan penyelamat:
      1. tepat di kiri awal kolom berikutnya,
      2. tengah koridor kosong terdekat,
      3. biarkan taksiran header.
    """
    win = 0.25 * colw
    # Penangkap kasus ekor: kalau ADA tepi-kiri berdukungan kuat di sebelah KIRI
    # taksiran, batasnya pasti terlalu ke kanan -- ia akan menelan isi kolom
    # berikutnya. Ini bukan penyetelan: tepi-kiri yang didukung banyak baris
    # adalah bukti langsung letak kolom, sedangkan taksiran header/marker cuma
    # perkiraan. Terukur 3.4% batas meleset >0.3 lebar kolom, dan di situlah
    # seluruh nilai "ibu" hilang tertelan kolom "ayah".
    kuat = [c for c, n in starts
            if n >= 4 and est - 0.55 * colw < c < est - 0.18 * colw
            and c > prev + 0.30 * colw]
    if kuat:
        return min(kuat) - 1.0
    cand = [c for c, _n in starts if abs(c - est) <= win and c > prev + 0.1 * colw]
    if cand:
        return min(cand, key=lambda c: abs(c - est)) - 1.0
    margin = max(2.0, 0.05 * colw)
    if not any(lo - margin <= est <= hi + margin for lo, hi in bl):
        return est
    gaps = [(bl[i][1], bl[i + 1][0]) for i in range(len(bl) - 1)]
    gaps = [g for g in gaps if g[1] > g[0] and abs((g[0] + g[1]) / 2 - est) <= win]
    if not gaps:
        return est
    lo, hi = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - est))
    return (lo + hi) / 2.0


def marker_centers(marks, ncols):
    """Pusat marker "(n)" per kolom; yang tak terbaca diinterpolasi dari tetangganya."""
    ks = sorted(marks)
    out = {}
    for i in range(ncols):
        if i in marks:
            out[i] = marks[i].cx
            continue
        lo = [k for k in ks if k < i]
        hi = [k for k in ks if k > i]
        if lo and hi:
            a, b = lo[-1], hi[0]
            out[i] = marks[a].cx + (marks[b].cx - marks[a].cx) * (i - a) / (b - a)
    return out


# ------------------------------------------- template kolom + registrasi affine
# KK adalah formulir CETAK: posisi kolomnya tetap, yang berubah antar dokumen
# cuma skala dan geser. kk_template.json menyimpan batas kanan tiap kolom dalam
# koordinat ternormalisasi (0..1) per varian layout; di sini tiap dokumen
# di-"pas"-kan ke template itu.
#
# Ini menggantikan dua tambalan yang terbukti rapuh:
#   * header yang salah terdeteksi tidak lagi menjatuhkan kolom di kanannya --
#     RANSAC membuangnya sebagai outlier, bukan meneruskannya lewat min_x;
#   * kolom yang headernya tidak terbaca tidak lagi hilang -- template yang
#     memberi batasnya.
_TEMPLATE = {}
_TEMPLATE_DIMUAT = False


def load_template():
    """kk_template.json dari direktori parser, cwd, atau induknya. Tak ada -> {}."""
    global _TEMPLATE, _TEMPLATE_DIMUAT
    if _TEMPLATE_DIMUAT:
        return _TEMPLATE
    _TEMPLATE_DIMUAT = True
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    cari = [os.path.join(here, "kk_template.json"),
            os.path.join(os.getcwd(), "kk_template.json")]
    cari += [os.path.join(here, *([".."] * n), "kk_template.json") for n in (1, 2, 3)]
    for p in cari:
        try:
            with open(p, encoding="utf-8") as f:
                _TEMPLATE = json.load(f)
            break
        except (OSError, ValueError):
            continue
    return _TEMPLATE


def layout_variant(boxes):
    """v17 / v15 dari nomor marker tertinggi. Tak terbaca -> v17 (mayoritas)."""
    besar = 0
    for b in boxes:
        m = MARKER.match(b.text.strip())
        if m:
            besar = max(besar, int(m.group(1)))
    return "v15" if 14 <= besar <= 15 else "v17"


def template_for(varian, tag):
    """{key: (t, sd)} untuk satu tabel, atau {} kalau template tidak tersedia.

    sd ikut dibawa karena dipakai sebagai AMBANG: template hanya boleh menimpa
    batas dari header kalau simpangannya jauh di luar sebaran wajar kolom itu.
    Tanpa itu, prediksi template (sd 0.035-0.065) menimpa header yang sudah
    benar dan justru menurunkan hasil -- terukur -5 poin di status_hubungan.
    """
    d = (load_template().get(varian) or {}).get(tag) or {}
    return {k: (v["t"], v.get("sd") or 0.05)
            for k, v in d.items() if isinstance(v, dict) and "t" in v}


def fit_affine(anchors, tol):
    """x = a*t + b dari pasangan (t, x), tahan outlier.

    Jumlah anchor <= 9, jadi semua pasangan dicoba secara menyeluruh -- hasilnya
    deterministik, tidak seperti RANSAC yang mengambil sampel acak.
    """
    if len(anchors) < 2:
        return None
    terbaik = None
    for i in range(len(anchors)):
        for j in range(i + 1, len(anchors)):
            (t1, x1), (t2, x2) = anchors[i], anchors[j]
            if abs(t2 - t1) < 1e-6:
                continue
            a = (x2 - x1) / (t2 - t1)
            if a <= 0:                                # skala harus positif
                continue
            b = x1 - a * t1
            inlier = [(t, x) for t, x in anchors if abs(a * t + b - x) <= tol]
            if terbaik is None or len(inlier) > len(terbaik[0]):
                terbaik = (inlier, a, b)
    if terbaik is None:
        return None
    inlier, a, b = terbaik
    if len(inlier) < max(2, len(anchors) // 3):       # konsensus terlalu tipis
        return None
    n = len(inlier)                                   # refit kuadrat terkecil
    st = sum(t for t, _ in inlier); sx = sum(x for _, x in inlier)
    stt = sum(t * t for t, _ in inlier); stx = sum(t * x for t, x in inlier)
    den = n * stt - st * st
    if abs(den) > 1e-9:
        a = (n * stx - st * sx) / den
        b = (sx - a * st) / n
    return (a, b) if a > 0 else None


def column_ranges(headers, body, x_left, x_right, marks=None, tpl=None, anchor_col=None):
    """(lo, hi] per kolom. Sel dimiliki kolom pertama yang batas kanannya >= x0 nilai.

    Kolom yang headernya gagal terbaca memakai cadangan: titik tengah antara
    dua marker "(n)" yang mengapitnya -- tanpa itu isinya bocor ke kolom sebelah.
    """
    mc = marker_centers(marks or {}, len(headers))
    mid = {i: (mc[i] + mc[i + 1]) / 2 for i in range(len(headers) - 1)
           if i in mc and i + 1 in mc}
    span = median([mid[i + 1] - mid[i] for i in range(len(headers) - 2)
                   if i in mid and i + 1 in mid], 0.0)
    named = []
    for i, (k, h) in enumerate(headers):
        if h is not None and (i not in mid or not span
                              or abs(h.x1 - mid[i]) < 0.7 * span):
            named.append((k, h.x1))                  # header cocok dgn marker
        elif i in mid:
            named.append((k, mid[i]))                # header gagal -> pakai marker
        elif h is not None:
            named.append((k, h.x1))
    # Kolom TERAKHIR tidak punya batas kanan dari header/marker (batasnya tepi
    # tabel), jadi kalau headernya gagal terbaca ia akan hilang dan kolom
    # sebelumnya melar menelan isinya -- header "Ibu" cuma 3 huruf dan sering
    # terbaca "lbu"/"bu". Pertahankan selama ada isi di sebelah kanannya.
    last_key = headers[-1][0]
    if headers[-1][1] is None and last_key not in [k for k, _ in named] and named:
        batas = named[-1][1]
        if (len(headers) - 1) in mc or any(b.x0 > batas + 0.15 * (span or 40) for b in body):
            named.append((last_key, x_right))
    # --- registrasi ke template cetak -------------------------------------
    # Batas dari header/marker dipakai sebagai ANCHOR, bukan sebagai kebenaran:
    # yang sejalan dengan template dipertahankan, yang menyimpang diganti hasil
    # prediksi, dan kolom yang tak punya anchor sama sekali diisi template.
    if tpl:
        W = max(1.0, x_right - x_left)
        posisi = dict(named)
        # Tepi kanan tabel adalah korespondensi yang DIKETAHUI untuk t=1.0 --
        # template memang dinormalisasi dengan R = tepi kanan. Memasukkannya
        # sebagai anchor mencegah fit melewati tepi; tanpa itu batas kolom
        # kedua-terakhir menembus x_right dan kolom terakhir ("ibu") tersisa
        # selebar 1 piksel.
        jangkar = [(tpl[k][0], x) for k, x in named if k in tpl] + [(1.0, x_right)]
        pas = fit_affine(jangkar, 0.08 * W)
        if pas:
            a, b = pas
            baru = []
            t_sblm = 0.0                             # batas template kolom sebelumnya
            # Kolom ANCHOR (dan semua yang di kirinya) tidak boleh disentuh:
            # anchor menentukan row_bands, jadi menggeser batasnya sedikit saja
            # merusak penomoran baris seluruh tabel. Terukur: menyisipkan
            # "tanggal_perkawinan" menyempitkan batas kiri status_hubungan,
            # pita baris tabel-2 melonjak 6->10, dan field itu turun 5 poin.
            urut = [k for k, _ in headers]
            batas_kiri = urut.index(anchor_col) if anchor_col in urut else -1
            for i_kol, (k, _h) in enumerate(headers):
                if i_kol <= batas_kiri:
                    if k in posisi:
                        baru.append((k, posisi[k]))
                    if k in tpl:
                        t_sblm = tpl[k][0]
                    continue
                t_sd = tpl.get(k)
                ramal = a * t_sd[0] + b if t_sd else None
                # Prediksi HARUS di dalam tabel. Tanpa jepitan ini, batas kolom
                # bisa melewati tepi kanan; kolom terakhir lalu menerima
                # (prev, x_right) dengan prev > x_right -- rentang terbalik yang
                # tak mungkin menangkap apa pun. Terukur: 20 nilai "ibu" hilang
                # di 9 dokumen karena "ayah" disisipkan di luar tepi tabel.
                if ramal is not None:
                    ramal = min(max(ramal, x_left + 1.0), x_right - 1.0)
                kini = posisi.get(k)
                if kini is None:
                    # Kolom tanpa anchor hanya diisi kalau ADA ISINYA. Rentang
                    # dibangun berurutan, jadi menyisipkan kolom kosong merampas
                    # ruang-x tetangganya -- dan kolom yang paling sering tak
                    # ber-header di korpus ini (No. Paspor, No. KITAP, Tanggal
                    # Perkawinan) memang selalu kosong. Menyisipkannya terukur
                    # menjatuhkan status_hubungan 6 poin.
                    if ramal is None:
                        continue
                    lebar = (t_sd[0] - t_sblm) * a
                    if lebar > 0 and any(ramal - lebar < bx.x0 <= ramal for bx in body):
                        baru.append((k, ramal))
                        t_sblm = t_sd[0]
                    continue
                # ada anchor: pertahankan, kecuali menyimpang jauh di luar
                # sebaran wajar kolom ini (4 sigma, minimal 10% lebar tabel)
                batas = max(4.0 * t_sd[1], 0.10) * W if t_sd else None
                # Dicoba dan DITOLAK: menjamin kolom sisa kebagian ruang
                # (pilih = min(pilih, x_right - 0.5*a*(1-t))) memang menaikkan
                # coverage, tapi validitasnya turun lebih banyak -- 86.62% ->
                # 86.55%. Nilai tambahan yang terjaring ternyata sebagian besar
                # salah kolom, bukan nilai yang selama ini hilang.
                baru.append((k, ramal if (ramal is not None and abs(kini - ramal) > batas)
                             else kini))
                if t_sd:
                    t_sblm = t_sd[0]
            if baru:
                named = baru

    named = [(k, x) for k, x in named if x > x_left]
    for i in range(1, len(named)):                   # buang header yang tak monoton
        if named[i][1] <= named[i - 1][1]:
            named[i] = (named[i][0], named[i - 1][1] + 1)
    if not named:
        return {}
    bl, st = blobs(body), column_starts(body)
    est = [x for _, x in named[:-1]] + [x_right]
    colw = max(20.0, median([est[i + 1] - est[i] for i in range(len(est) - 1)], 80.0))
    bounds, prev = [], x_left
    for i, e in enumerate(est):
        prev = x_right if i == len(est) - 1 else max(snap(e, st, bl, colw, prev), prev + 1)
        bounds.append(prev)
    ranges, prev = {}, x_left
    for (k, _), b in zip(named, bounds):
        ranges[k] = (prev, b)
        prev = b
    return ranges


# --------------------------------------------------------------- 3. baris/rows
SERIAL = re.compile(r"^\(?(\d{1,2})\)?[-.,:]?$")


def split_serial_column(body):
    """Pisahkan kolom nomor urut (paling kiri, teks <=3 karakter).

    OCR sering salah baca nomor urut ('1'->'', '3'->'2', '10'->'1o'), jadi ia
    dipakai sebagai petunjuk indeks baris saja -- bukan sebagai anchor utama.
    """
    if not body:
        return [], [], 0.0
    u = median([b.y1 - b.y0 for b in body], 12.0)
    xmin = min(b.x0 for b in body)
    serial = [b for b in body if b.x0 < xmin + 2.2 * u and len(compact(b.text)) <= 3]
    if len(serial) < 3:
        return body, [], xmin - 2
    sid = set(id(b) for b in serial)
    rest = [b for b in body if id(b) not in sid]
    return rest, serial, (min(b.x0 for b in rest) - 2 if rest else xmin)


def row_bands(anchors, body, hint=None):
    """Kisi baris: pitch + fase diambil dari kolom anchor (regresi linier atas
    indeks baris), lalu diperluas sejauh masih ada isi di zona tabel."""
    cys = sorted(b.cy for b in anchors) or sorted(b.cy for b in (hint or []))
    heights = [b.y1 - b.y0 for b in body] or [10.0]
    if len(cys) >= 2:
        diffs = [d for d in (cys[i + 1] - cys[i] for i in range(len(cys) - 1)) if d > 3]
        base = min(diffs) if diffs else median(heights, 10.0) * 1.3
        pitch = median([d / max(1, round(d / base)) for d in diffs], base)
    else:
        pitch = median(heights, 10.0) * 1.3
    if not cys:
        return [], pitch
    # regresi cy = a + b*k dengan k = indeks baris relatif (toleran baris kosong)
    ks = [round((c - cys[0]) / pitch) for c in cys]
    n = len(ks)
    mk, mc = sum(ks) / n, sum(cys) / n
    var = sum((k - mk) ** 2 for k in ks)
    b_ = (sum((ks[i] - mk) * (cys[i] - mc) for i in range(n)) / var) if var else pitch
    b_ = b_ if 0.5 * pitch < b_ < 1.5 * pitch else pitch
    a_ = mc - b_ * mk
    lo = min(x.cy for x in body)
    hi = max(x.cy for x in body)
    k0 = int(round((lo - a_) / b_))
    k1 = int(round((hi - a_) / b_))
    k0, k1 = min(k0, 0), max(k1, max(ks))
    if k1 - k0 > 14:                                 # jaga-jaga kalau zona meleset
        k1 = k0 + 14
    return [a_ + b_ * k for k in range(k0, k1 + 1)], b_


def assign_rows(col_boxes, cys, pitch):
    """Ikat box ke pita baris via overlap-y maksimum, lalu koreksi offset kolom."""
    def bind(shift):
        res = {}
        for b in col_boxes:
            best, best_ov = None, 0.0
            for i, cy in enumerate(cys):
                lo, hi = cy + shift - pitch / 2, cy + shift + pitch / 2
                ov = min(b.y1, hi) - max(b.y0, lo)
                if ov > best_ov:
                    best, best_ov = i, ov
            if best is None:                         # box lebih tipis dari pita
                dist, best = min((abs(b.cy - (cy + shift)), i) for i, cy in enumerate(cys))
                if dist > 0.6 * pitch:
                    continue
            res.setdefault(best, []).append(b)
        return res

    res = bind(0.0)
    off = median([b.cy - cys[i] for i, bs in res.items() for b in bs], 0.0)
    if abs(off) > 0.3 * pitch:                       # kolom tercetak naik/turun
        res = bind(off)
    return res


CRF_KOLOM = True          # penetapan kolom lewat Viterbi (butuh kk_template.json)

# ---------------------------------------- fitur & bobot terlatih untuk emisi
# Bobot emisi versi pertama ditetapkan dengan tangan (+3 kalau cocok vocab, -1
# kalau tidak, penalti posisi kuadratik). Angka-angka itu tebakan saya, dan
# tebakan yang sama dipakai untuk SEMUA kolom -- padahal cocok-vocab di
# status_hubungan (11 nilai pendek, khas) jauh lebih kuat sebagai bukti
# daripada di jenis_pekerjaan (89 nilai, gampang salah cocok).
#
# Di sini bobotnya DIPELAJARI: klasifikasi biner atas pasangan (box, kolom)
# "apakah box ini milik kolom itu", dilatih di luar lalu diekspor ke
# kk_kolom_model.json. Parser tetap stdlib murni -- inferensinya cuma perkalian
# titik. Tanpa berkas model, skor tangan lama yang dipakai.
RE_TANGGAL = re.compile(r"^\d{1,2}[-/. ]\d{1,2}[-/. ]\d{2,4}$")
KOL_TANGGAL = ("tanggal_lahir", "tanggal_perkawinan")

# di_dalam DIPECAH menurut apakah kolomnya punya bukti isi (vocab / NIK /
# tanggal). Dengan satu fitur gabungan, satu bobot harus melayani dua keadaan
# yang bertentangan dan keduanya rusak -- terukur: bobot tunggal yang besar
# membuat isi tak pernah bisa menang (dokumen bersih 476 -> 398), sedangkan
# membuang fiturnya meruntuhkan kolom tanpa vocab (ibu 80.2% -> 47.8%).
FITUR_KOLOM = ["bias", "di_dalam_isi", "di_dalam_pos", "jarak", "jarak2",
               "vocab_sim", "vocab_ok", "nama_sim", "nik_cocok", "tanggal_cocok",
               "lebar_rel", "conf", "digit"]

_MODEL_KOLOM = None
_MODEL_DIMUAT = False
JEJAK_KOLOM = None        # set ke list untuk merekam keputusan kolom (pembuatan dataset)


def load_model_kolom():
    """Bobot terlatih dari kk_kolom_model.json; {} kalau belum dilatih."""
    global _MODEL_KOLOM, _MODEL_DIMUAT
    if _MODEL_DIMUAT:
        return _MODEL_KOLOM
    _MODEL_DIMUAT = True
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for p in [os.path.join(here, "kk_kolom_model.json"),
              os.path.join(os.getcwd(), "kk_kolom_model.json")] + \
             [os.path.join(here, *([".."] * n), "kk_kolom_model.json") for n in (1, 2, 3)]:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("fitur") != FITUR_KOLOM:      # definisi fitur berubah -> jangan dipakai
                continue
            _MODEL_KOLOM = d
            break
        except (OSError, ValueError):
            continue
    return _MODEL_KOLOM


def sim_terbaik(text, vocab):
    """Kemiripan tertinggi ke sebuah daftar, dengan prafilter panjang."""
    ca = compact(text)
    if not ca or not vocab:
        return 0.0
    terbaik = 0.0
    for v in vocab:
        lb = len(compact(v))
        if not lb or 2.0 * min(len(ca), lb) / (len(ca) + lb) < 0.5:
            continue
        s = sim(text, v)
        if s > terbaik:
            terbaik = s
    return terbaik


def fitur_kolom(b, key, vocab, span_k, nama_dok):
    """Vektor fitur untuk pasangan (box, kolom), urut sesuai FITUR_KOLOM.

    SATU definisi dipakai pembuatan dataset DAN inferensi. Memisahkannya adalah
    cara paling andal membuat model yang bagus saat latih lalu buruk saat pakai.
    """
    lo, hi, sd = span_k
    t = b.x0
    di_dalam = 1.0 if lo < t <= hi else 0.0
    d = 0.0 if di_dalam else ((lo - t) if t <= lo else (t - hi)) / max(sd, 1e-6)
    d = min(d, 8.0)
    ca = compact(b.text)
    vs = sim_terbaik(b.text, vocab) if vocab else 0.0
    punya_isi = bool(vocab) or key == "nik" or key in KOL_TANGGAL
    return [
        1.0,
        di_dalam if punya_isi else 0.0,
        0.0 if punya_isi else di_dalam,
        d,
        min(d * d, 64.0),
        vs,
        1.0 if (vocab and normalize(b.text, vocab) in vocab) else 0.0,
        sim_terbaik(b.text, nama_dok) if (key in ("ayah", "ibu") and nama_dok) else 0.0,
        1.0 if (key == "nik" and is_nik(b.text)) else 0.0,
        1.0 if (key in KOL_TANGGAL and RE_TANGGAL.match(b.text.strip())) else 0.0,
        min((b.x1 - b.x0) / max(hi - lo, 1e-6), 3.0),
        b.conf,
        (sum(c.isdigit() for c in ca) / len(ca)) if ca else 0.0,
    ]


def bonus_vocab(text, vocab, skala=8.0, ambang=0.62, lantai=-1.5, atap=3.0):
    """Bukti isi untuk emisi CRF, BERJENJANG menurut kemiripan.

    Versi pertama menilainya menang-kalah: +3 kalau normalize() berhasil, -1
    kalau tidak. Token yang 0.95 mirip nilai kanonik dan yang 0.63 mirip
    diperlakukan sama, padahal yang pertama bukti jauh lebih kuat. Di sini
    skornya sebanding kemiripan, dijepit supaya satu kolom tidak bisa
    mendominasi keputusan seluruh baris.

    Ambang 0.62 disamakan dengan normalize() supaya "cocok" berarti hal yang
    sama di penetapan kolom dan di normalisasi nilai.
    """
    ca = compact(text)
    if not ca or not vocab:
        return 0.0
    terbaik = 0.0
    for v in vocab:
        lb = len(compact(v))
        # batas atas rasio SequenceMatcher: kandidat yang panjangnya terlalu
        # jauh mustahil menang, jadi tidak perlu dihitung (daftar pekerjaan 93
        # entri dikalikan tiap token dan tiap kolom -- ini yang menahan biayanya)
        if not lb or 2.0 * min(len(ca), lb) / (len(ca) + lb) < 0.5:
            continue
        s = sim(text, v)
        if s > terbaik:
            terbaik = s
    return max(lantai, min(atap, skala * (terbaik - ambang)))


def assign_columns_viterbi(row_boxes, cols, ranges, tpl, W, nama_dok=()):
    """Tetapkan kolom untuk box-box dalam SATU baris. Rantai linear + Viterbi.

    Keanggotaan-berdasarkan-rentang-x menilai tiap box sendiri-sendiri, jadi ia
    bisa menghasilkan urutan yang mustahil: box ke-5 masuk "ayah" sementara box
    ke-6 masuk "pendidikan". Kolom KK berurutan dan tidak saling menyelip, dan
    di sini kendala itu ditegakkan sebagai transisi (state hanya boleh maju),
    bukan ditambal belakangan.

    Skor emisi menggabungkan dua bukti yang saling melengkapi:
      * posisi  -- jarak ke span kolom pada template, dibagi sigma kolom itu,
      * isi     -- kolom bervocab tertutup memberi bonus besar kalau teksnya
                   menyambung ke nilai kanonik. Inilah yang tidak dimiliki
                   pendekatan rentang-x: "WNI" di posisi meragukan tetap
                   tertarik ke kolom kewarganegaraan.
    """
    if not row_boxes or not ranges:
        return {}
    # Span kolom dipakai dalam KOORDINAT HALAMAN dari `ranges` -- itu hasil
    # registrasi per dokumen. Memakai (x0 - x_left)/lebar justru mengandaikan
    # dokumen membentang persis seperti template, asumsi yang registrasi ada
    # untuk menggantikannya. tpl hanya dipakai untuk sigma tiap kolom.
    span = []
    for k, _lab, _v in cols:
        if k in ranges:
            lo, hi = ranges[k]
            sd = max((tpl.get(k) or (0, 0.05))[1], 0.02) * W
            span.append((lo, hi, sd))
        else:
            span.append(None)                        # kolom tak ada di varian ini
    if not any(span):
        return {}

    boxes = sorted(row_boxes, key=lambda b: b.x0)
    n, NEG = len(cols), -1e9
    model = load_model_kolom()
    bobot = model.get("bobot") if model else None
    emisi = []
    for b in boxes:
        t = b.x0
        baris = []
        for i, (k, _lab, vocab) in enumerate(cols):
            if span[i] is None:
                baris.append(NEG)
                continue
            if bobot:                              # emisi = log-odds terlatih
                f = fitur_kolom(b, k, vocab, span[i], nama_dok)
                baris.append(sum(w * x for w, x in zip(bobot, f)))
                continue
            lo, hi, sd = span[i]
            if lo < t <= hi:
                s = 0.0
            else:
                d = (lo - t) if t <= lo else (t - hi)
                s = -min((d / sd) ** 2, 50.0)
            if vocab:
                # Dicoba dan DITOLAK: bonus berjenjang bonus_vocab(b.text, vocab)
                # untuk kolom Dukcapil. Coverage naik tipis tapi validitas turun
                # lebih banyak -- 88.79% -> 88.71%, dokumen bersih 476 -> 472.
                # Untuk vocab tertutup yang pendek dan khas, "cocok / tidak"
                # ternyata sinyal yang lebih bersih daripada derajat kemiripan.
                s += 3.0 if normalize(b.text, vocab) in vocab else -1.0
            elif k == "nik":
                s += 3.0 if is_nik(b.text) else -1.0
            elif k in ("ayah", "ibu") and nama_dok:
                # Vocab PER DOKUMEN. "ayah"/"ibu" satu-satunya kolom tanpa
                # daftar kanonik, dan satu-satunya yang tidak terbantu CRF.
                # Terukur di korpus: ~80% nama orang tua pada baris ANAK adalah
                # PERSIS nama anggota lain di dokumen yang sama (340/438 untuk
                # ibu, 327/450 untuk ayah). Daftar itu diambil dari kolom
                # nama_lengkap tabel-1 -- kolom lain, anchor lain, dan akurasinya
                # sudah 95-98%, jadi ini bukan penalaran melingkar.
                # HANYA memberi hadiah, tidak pernah menghukum: orang tua kepala
                # keluarga dan istrinya memang bukan anggota rumah tangga ini,
                # jadi "tidak cocok" itu normal dan bukan bukti salah kolom.
                # Dengan penalti (lantai -1.5), ayah runtuh 86.8% -> 37.3%.
                s += max(0.0, bonus_vocab(b.text, nama_dok, ambang=0.70))
            baris.append(s)
        emisi.append(baris)

    dp = [[NEG] * n for _ in boxes]
    bp = [[-1] * n for _ in boxes]
    for i in range(n):
        dp[0][i] = emisi[0][i]
    for j in range(1, len(boxes)):
        terbaik, arg = NEG, -1
        for i in range(n):                           # maksimum berjalan: i' <= i
            if dp[j - 1][i] > terbaik:
                terbaik, arg = dp[j - 1][i], i
            dp[j][i] = terbaik + emisi[j][i]
            bp[j][i] = arg
    i = max(range(n), key=lambda i: dp[-1][i])
    jalur = [i]
    for j in range(len(boxes) - 1, 0, -1):
        i = bp[j][i]
        jalur.append(i)
    jalur.reverse()

    # ---- forward-backward: keyakinan penetapan tiap token -----------------
    # Viterbi cuma memberi jalur terbaik, skornya dibuang. Marginal
    # P(token j ada di kolom k | seluruh baris) menjawab hal lain: SEBERAPA
    # YAKIN penetapan itu. Biayanya sama dengan Viterbi -- max diganti
    # logsumexp, dengan jumlah berjalan yang sama karena transisi hanya maju.
    #
    # Ini TIDAK mengubah keputusan. Jalur Viterbi tetap yang dipakai; marginal
    # hanya dilaporkan.
    def _lse(a, b):
        if a <= NEG / 2:
            return b
        if b <= NEG / 2:
            return a
        hi, lo = (a, b) if a > b else (b, a)
        return hi + math.log1p(math.exp(lo - hi))

    m = len(boxes)
    alpha = [[NEG] * n for _ in range(m)]
    beta = [[NEG] * n for _ in range(m)]
    for i2 in range(n):
        alpha[0][i2] = emisi[0][i2]
    for j in range(1, m):
        jalan = NEG                                   # logsumexp berjalan, k' <= k
        for i2 in range(n):
            jalan = _lse(jalan, alpha[j - 1][i2])
            alpha[j][i2] = jalan + emisi[j][i2]
    for i2 in range(n):
        beta[m - 1][i2] = 0.0
    for j in range(m - 2, -1, -1):
        jalan = NEG                                   # dari kanan: k'' >= k
        for i2 in range(n - 1, -1, -1):
            jalan = _lse(jalan, emisi[j + 1][i2] + beta[j + 1][i2])
            beta[j][i2] = jalan
    Z = NEG
    for i2 in range(n):
        Z = _lse(Z, alpha[m - 1][i2])
    keyakinan = []
    for j in range(m):
        if Z <= NEG / 2:
            keyakinan.append(0.0)
            continue
        lp = alpha[j][jalur[j]] + beta[j][jalur[j]] - Z
        keyakinan.append(math.exp(lp) if lp < 0 else 1.0)

    if JEJAK_KOLOM is not None:
        # Perekam untuk pembuatan dataset latih. Sengaja di SINI, bukan disalin
        # ulang di script latih: box per baris, span kolom, dan nama_dok harus
        # identik dengan yang dipakai saat inferensi, kalau tidak modelnya
        # dilatih atas sesuatu yang tidak pernah ia lihat waktu dipakai.
        JEJAK_KOLOM.append({"boxes": boxes, "span": span, "nama_dok": nama_dok,
                            "cols": cols, "jalur": jalur})

    hasil, yakin = {}, {}
    for b, i, ky in zip(boxes, jalur, keyakinan):
        if 0 <= i < n and span[i] is not None:
            hasil.setdefault(cols[i][0], []).append(b)
            # sel berisi beberapa token: seyakin token paling ragu di dalamnya
            k = cols[i][0]
            yakin[k] = ky if k not in yakin else min(yakin[k], ky)
    return hasil, yakin


# ----------------------------------------------------- 3b. zoning & pairing
def is_nik(text):
    return bool(re.fullmatch(r"\d{16}", compact(text)))


def is_hubungan(text):
    return normalize(text, HUBUNGAN, 0.7) in HUBUNGAN


def footer_top(boxes, y_after, page_h, u=12.0):
    """Batas bawah tabel-2: blok tanda tangan / 'Dikeluarkan Tanggal'."""
    ys = [b.y0 for b in boxes if b.cy > y_after and (
        sim(b.text.split(":")[0], "Dikeluarkan Tanggal") >= 0.6
        or sim(b.text, "KEPALA DINAS KEPENDUDUKAN DAN") >= 0.7
        or sim(b.text, "Tanda Tangan/Cap Jempol") >= 0.7)]
    return min(ys) - 0.25 * u if ys else 0.90 * page_h


def table_zones(boxes, page_h):
    """Tentukan (body, header-band) tiap tabel.

    Anchor utama = baris marker '(1)..(9)' / '(10)..(17)'; kalau tak terbaca,
    posisinya ditaksir dari header kolom. Salah satu tabel boleh gagal.
    """
    # baris marker ditempelkan ke tabelnya lewat posisi-y header, bukan nomornya
    u = median([b.y1 - b.y0 for b in boxes], 12.0)   # satuan = tinggi teks
    rows = marker_groups(boxes, u)
    h1 = find_header(boxes, "Nama Lengkap", 0, page_h)
    h2 = find_header(boxes, "Status Hubungan Dalam Keluarga", (h1.y1 + 15) if h1 else 0, page_h)
    k1 = k2 = {}
    m1 = m2 = None
    for k, cy in rows:
        if h1 and h2:
            t1_side = abs(cy - h1.y1) <= abs(cy - h2.y1)
        elif h2:
            t1_side = cy < h2.y0
        elif h1:
            t1_side = cy < h1.y1 + 5 * u
        else:
            t1_side = max(k) <= 9
        if t1_side and m1 is None:
            k1, m1 = k, cy
        elif not t1_side and m2 is None:
            k2, m2 = k, cy
    if m1 is None and h1 is not None:
        m1 = h1.y1 + 0.9 * u                         # baris marker persis di bawah header
    if m2 is None:
        h = h2 or find_header(boxes, "Kewarganegaraan", (m1 or 0) + 15, page_h)
        m2 = h.y1 + 0.9 * u if h is not None else None
    z1 = hb1 = z2 = hb2 = None
    if m2 is not None:
        hdr2 = [b.y0 for b in boxes if m2 - 4 * u < b.cy < m2 - 0.2 * u]
        t2_top = min(hdr2) if hdr2 else m2 - 2.5 * u
        hb2 = (m2 - 4 * u, m2 - 0.25 * u)
        z2 = (m2 + 0.35 * u, footer_top(boxes, m2 + 0.35 * u, page_h, u))
    if m1 is not None:
        hb1 = (m1 - 4 * u, m1 - 0.25 * u)
        z1 = (m1 + 0.35 * u, (t2_top - 0.25 * u) if m2 is not None
              else footer_top(boxes, m1 + 0.35 * u, page_h, u))
    return (z1, hb1, k1, m1), (z2, hb2, k2, m2)


def is_member_row_t1(r):
    return is_nik(r.get("nik", "")) or len(compact(r.get("nama_lengkap", ""))) >= 4


def is_member_row_t2(r):
    return (is_hubungan(r.get("status_hubungan_dalam_keluarga", ""))
            or normalize(r.get("kewarganegaraan", ""), WARGANEGARA, 0.7) in WARGANEGARA)


def score_pairing(pairs):
    """Seberapa masuk akal satu hipotesis pemasangan, dinilai dari struktur keluarga.

    Pemasangan yang benar menghasilkan KK yang konsisten: kepala keluarga ada
    tepat satu dan di baris pertama, dan ayah/ibu seorang ANAK memang nama
    anggota lain di kartu yang sama.
    """
    if not pairs:
        return -99.0
    shdk = [normalize(p[2][0].get("status_hubungan_dalam_keluarga", ""), HUBUNGAN)
            for p in pairs]
    nama = [p[1][0].get("nama_lengkap", "") for p in pairs]
    sc = 2.0 if shdk[0] == "KEPALA KELUARGA" else 0.0
    sc += 2.0 if shdk.count("KEPALA KELUARGA") == 1 else -1.0
    kk = nama[shdk.index("KEPALA KELUARGA")] if "KEPALA KELUARGA" in shdk else ""
    istri = nama[shdk.index("ISTRI")] if "ISTRI" in shdk else ""
    for s_, p in zip(shdk, pairs):
        if s_ == "ANAK":
            if kk and sim(p[2][0].get("ayah", ""), kk) >= 0.85:
                sc += 1.0
            if istri and sim(p[2][0].get("ibu", ""), istri) >= 0.85:
                sc += 1.0
        if not any(p[2][0].values()):
            sc -= 0.5                                # baris tanpa pasangan
    return sc


def pair_rows(a, b):
    """Pasangkan baris tabel-1 dengan tabel-2.

    Tiga hipotesis dihitung lalu dinilai (lihat score_pairing), yang paling
    konsisten dipakai:
      1. nomor baris ABSOLUT (jarak ke baris marker / pitch) -- baris ke-k
         dicetak di posisi sama di kedua tabel, jadi baris yang hilang di salah
         satu tabel tidak menggeser sisanya; beda acuan antar tabel dikalibrasi,
      2. nomor urut fisik kalau kolomnya terbaca,
      3. urutan baris berisi (paling rapuh, tapi kadang satu-satunya).
    """
    rows1 = [(i, r, c) for i, (r, c) in enumerate(a["rows"]) if is_member_row_t1(r)]
    if not rows1:
        return []
    isi = lambda r: any(re.search(r"[A-Za-z0-9]", v or "") for v in r.values())
    rows2 = [(i, r, c) for i, (r, c) in enumerate(b["rows"]) if isi(r)]
    rows2_real = [x for x in rows2 if is_member_row_t2(x[1])]

    def keyed(rows, src):
        k = [src[i] if i < len(src) else None for i, _, _ in rows]
        return None if (None in k or len(set(k)) != len(k)) else k

    cands = []
    for src in ("rownum", "labels"):
        ka, kb = keyed(rows1, a[src]), keyed(rows2, b[src])
        if not ka or not kb:
            continue
        # geser kb sejauh delta yang paling banyak mencocokkan baris
        d = max(range(-3, 4), key=lambda d: (len(set(ka) & {k + d for k in kb}), -abs(d)))
        idx = {k + d: x for k, x in zip(kb, rows2)}
        cands.append([(i, (r, c), (idx[k][1], idx[k][2]) if k in idx else ({}, {}))
                      for (i, r, c), k in zip(rows1, ka)])
    cands.append([(i, (r, c), (rows2_real[n][1], rows2_real[n][2])
                   if n < len(rows2_real) else ({}, {}))
                  for n, (i, r, c) in enumerate(rows1)])
    return max(cands, key=score_pairing)


def split_straddling(body, ranges):
    """OCR kadang menyatukan dua sel ('29-02-1990 ISLAM'). Potong di batas kolom,
    pada spasi terdekat dengan posisi batas itu."""
    bounds = sorted(set(hi for _, hi in ranges.values()))
    out = []
    for b in body:
        cuts = [x for x in bounds if b.x0 + 3 < x < b.x1 - 3]
        if not cuts or " " not in b.text.strip():
            out.append(b)
            continue
        x0, text = b.x0, b.text
        for cut in cuts:
            w = b.x1 - x0
            i = int(round((cut - x0) / w * len(text))) if w > 0 else 0
            sp = [j for j, ch in enumerate(text) if ch == " "]
            if not sp:
                break
            j = min(sp, key=lambda j: abs(j - i))
            out.append(Box([[x0, b.y0], [cut, b.y0], [cut, b.y1], [x0, b.y1]],
                           text[:j], b.conf))
            x0, text = cut, text[j + 1:]
        out.append(Box([[x0, b.y0], [b.x1, b.y0], [b.x1, b.y1], [x0, b.y1]], text, b.conf))
    return [b for b in out if b.text.strip()]


EMPTY_TABLE = {"rows": [], "ranges": {}, "pitch": 0.0, "headers": [], "labels": [],
               "rownum": [], "cys": [], "body": [], "kol_conf": []}


# ------------------------------------------------------------- 4. parse a table
def parse_table(boxes, cols, zone, hdr_band, page_w, anchor_col, anchor_ok,
                marks=None, m_cy=None, pitch_hint=None, nama_dok=()):
    y_top, y_bot = zone
    body = [b for b in boxes if y_top < b.cy < y_bot and b.conf > 0.3
            and not MARKER.match(b.text) and re.search(r"[A-Za-z0-9]", b.text)]
    body, serial, x_left = split_serial_column(body)
    if not body:
        return EMPTY_TABLE
    local = estimate_shear(body)                     # dokumen melengkung: tiap tabel beda
    body, serial = apply_shear(body, local), apply_shear(serial, local)

    u_hdr = median([b.y1 - b.y0 for b in boxes], 12.0)
    labels_semua = [c[1] for c in cols]
    headers, last_x = [], -1.0
    for key, label, _ in cols:
        saingan = [x for x in labels_semua if x != label]
        h = find_header(boxes, label, hdr_band[0], hdr_band[1], last_x, saingan)
        if h is None:
            # Pita header dihitung dari baris marker, yang posisinya kadang
            # ditaksir. Header yang meleset beberapa piksel di luar pita (mis.
            # "Ayah" pada cy=628 sementara pita berakhir di 625) hilang total,
            # jadi sekali lagi dengan pita yang dilonggarkan.
            h = find_header(boxes, label, hdr_band[0] - 2.0 * u_hdr,
                            hdr_band[1] + 1.5 * u_hdr, last_x, saingan)
        headers.append((key, h))
        if h is not None:
            last_x = h.x1
    x_right = max([b.x1 for b in body] + [h.x1 for _, h in headers if h]) + 8
    base = calibrate_base(marks or {}, headers, 1 if cols is COLS_T1 else 10)
    marks = {n - base: b for n, b in (marks or {}).items()}
    tpl = template_for(layout_variant(boxes), "T1" if cols is COLS_T1 else "T2")
    ranges = column_ranges(headers, body, x_left, min(x_right, page_w), marks, tpl,
                           anchor_col)
    if not ranges:
        return EMPTY_TABLE
    body = split_straddling(body, ranges)

    lo, hi = ranges.get(anchor_col, (x_left, x_right))
    anchors = [b for b in body if lo < b.x0 <= hi and anchor_ok(b.text)]
    if len(anchors) < 2 and len(serial) < 2:
        # kolom anchor tak terbaca sama sekali: pakai kolom terpadat sebagai
        # pengganti -- satu kolom saja sudah memberi pitch dan fase baris
        cols_boxes = [[b for b in body if a < b.x0 <= z] for a, z in ranges.values()]
        anchors = max(cols_boxes, key=len) if cols_boxes else []
    cys, pitch = row_bands(anchors, body, hint=serial)
    if pitch_hint and len(anchors) < 3:               # tabel lain punya anchor lebih kuat
        pitch = pitch_hint
    if not cys:
        return dict(EMPTY_TABLE, headers=[h for _, h in headers if h])

    keys = [c[0] for c in cols]
    rows = [dict.fromkeys(keys, "") for _ in cys]
    confs = [dict.fromkeys(keys, None) for _ in cys]
    label_of = {c[0]: c[1] for c in cols}
    # Pengikatan BARIS tetap per kolom: assign_rows mengoreksi offset-y tiap
    # kolom (sebagian kolom tercetak agak naik/turun), dan koreksi itu hilang
    # kalau semua box diikat sekaligus.
    per_baris = {}
    terpakai = set()
    for key, (lo, hi) in ranges.items():
        col_boxes = [b for b in body if lo < b.x0 <= hi]
        terpakai.update(id(b) for b in col_boxes)
        for r, bs in assign_rows(col_boxes, cys, pitch).items():
            per_baris.setdefault(r, []).extend(bs)
    # Box di luar SEMUA rentang sengaja tidak diikutkan: rentang sudah
    # menutupi [x_left, x_right] secara berurutan, jadi sisanya adalah sampah
    # tepi (mis. marker "(15)" yang terbaca "15" tanpa kurung).

    def isi(r, key, bs):
        bs = sorted(bs, key=lambda b: b.x0)
        txt = " ".join(b.text for b in bs).strip()
        if sim(txt, label_of[key]) >= 0.8:           # teks header bocor ke body
            return
        rows[r][key] = txt
        confs[r][key] = round(min(b.conf for b in bs), 4)

    kol_conf = [dict.fromkeys(keys, None) for _ in cys]
    if CRF_KOLOM and tpl:
        for r, bs in per_baris.items():
            grup_kol, yakin = assign_columns_viterbi(
                bs, cols, ranges, tpl,
                max(1.0, min(x_right, page_w) - x_left), nama_dok)
            for key, grup in grup_kol.items():
                isi(r, key, grup)
                if r < len(kol_conf):
                    kol_conf[r][key] = round(yakin.get(key, 0.0), 4)
    else:
        for key, (lo, hi) in ranges.items():
            col_boxes = [b for b in body if lo < b.x0 <= hi]
            for r, bs in assign_rows(col_boxes, cys, pitch).items():
                isi(r, key, bs)
    # nomor urut fisik (kalau terbaca) -> label baris
    labels = [None] * len(cys)
    for b in serial:
        m = SERIAL.match(b.text.replace("o", "0").replace("O", "0"))
        r = min(range(len(cys)), key=lambda i: abs(cys[i] - b.cy))
        if m and abs(cys[r] - b.cy) < 0.6 * pitch:
            labels[r] = int(m.group(1))
    # nomor baris ABSOLUT: jarak ke baris marker dibagi pitch. Geometri kedua
    # tabel sama, jadi baris ke-k di tabel atas dan bawah dapat nomor yang sama.
    rownum = [int(round((c - m_cy) / pitch)) for c in cys] if m_cy else [None] * len(cys)
    return {"rows": list(zip(rows, confs)), "ranges": ranges, "pitch": pitch,
            "headers": [h for _, h in headers if h], "labels": labels, "rownum": rownum,
            # cys & body dipakai pemakai luar (mis. ekstraktor LLM) yang perlu
            # token mentah per baris: keduanya dalam frame shear LOKAL tabel ini,
            # jadi jangan dicampur dengan boxes hasil shear global.
            "cys": cys, "body": body,
            # keyakinan PENETAPAN KOLOM (forward-backward), bukan keyakinan OCR.
            # Keduanya gagal dengan cara berbeda dan sengaja tidak dilebur.
            "kol_conf": kol_conf}


# --------------------------------------------------------- 5. identitas (kepala)
IDENT = [("nama_kepala_keluarga", "Nama Kepala Keluarga"), ("alamat", "Alamat"),
         ("rt_rw", "RT/RW"), ("kode_pos", "Kode Pos"),
         ("desa_kelurahan", "Desa/Kelurahan"), ("kecamatan", "Kecamatan"),
         ("kabupaten_kota", "Kabupaten/Kota"), ("provinsi", "Provinsi")]


def value_right_of(boxes, label_box, y_tol=0.45, max_dx=20.0, stop_labels=()):
    """Nilai di kanan label, berhenti di label berikutnya.

    Blok identitas punya dua kolom ("Alamat" di kiri, "Kecamatan" di kanan),
    jadi batas jarak saja tidak cukup: pengambilan dihentikan begitu ketemu box
    yang ternyata label lain -- kalau tidak, nilainya ikut menelan kolom kanan.
    """
    h = label_box.y1 - label_box.y0
    max_dx = max_dx * h                              # jarak dalam satuan tinggi teks
    out = [b for b in boxes
           if b is not label_box and b.x0 >= label_box.x1 - 4
           and b.x0 - label_box.x1 < max_dx
           and (min(b.y1, label_box.y1) - max(b.y0, label_box.y0)) > y_tol * h]
    out.sort(key=lambda b: b.x0)
    hasil = []
    for b in out:
        kepala = b.text.split(":")[0]
        if any(sim(kepala, lab) >= 0.6 for lab in stop_labels):
            break                                    # sudah masuk label berikutnya
        if hasil and re.match(r"^\s*N[oO0]\.?\s*\d", b.text):
            break                                    # "No.<angka>" = blok lain
        hasil.append(b)
    return hasil


def parse_identitas(boxes, y_end):
    zone = [b for b in boxes if b.cy < y_end]
    res, used = {}, set()
    for key, label in IDENT:
        h, best = None, 0.0
        for b in zone:
            if id(b) in used:
                continue
            s = sim(b.text.split(":")[0], label)
            if s > best and s >= 0.62:
                best, h = s, b
        if h is None:
            res[key] = ("", None)
            continue
        used.add(id(h))
        if ":" in h.text and h.text.split(":", 1)[1].strip():
            val, conf = h.text.split(":", 1)[1].strip(), h.conf
        else:
            vb = value_right_of(zone, h, stop_labels=[l for _, l in IDENT])
            val = " ".join(b.text for b in vb).strip()
            conf = round(min([b.conf for b in vb], default=1.0), 4) if vb else None
        res[key] = (re.sub(r"^[:\s]+", "", val), conf)
    return res


# salah baca yang lazim pada deret angka (hanya dipakai di dalam token berangka)
DIGIT_FIX = {"O": "0", "o": "0", "D": "0", "Q": "0", "U": "0",
             "I": "1", "l": "1", "i": "1", "|": "1", "!": "1", "L": "1",
             "Z": "2", "z": "2", "E": "3", "A": "4", "S": "5", "s": "5",
             "G": "6", "b": "6", "T": "7", "B": "8", "g": "9", "q": "9"}


def digit_runs(text):
    """Deret 16 angka di dalam teks, toleran label dan salah baca.

    Menangani "No.8101...", "No：8101...", "NO·8101...", "No KK:332303.181205.2168".
    Label dibuang dulu supaya 'o' pada "No" tidak ikut jadi angka 0.
    """
    out = []
    for tok in re.split(r"\s+", text.strip()):
        tok = re.sub(r"^[^0-9A-Za-z]*(?:N[oO0]\.?|KK)[^0-9A-Za-z]*", "", tok)
        tok = re.sub(r"^[^0-9]{0,3}", "", tok)
        tok = "".join(DIGIT_FIX.get(ch, ch) for ch in tok)
        tok = re.sub(r"(?<=\d)[ .\-/,](?=\d)", "", tok)     # pemisah di antara angka
        out += [m for m in re.findall(r"\d+", tok) if len(m) == 16]
    return out


def same_line_groups(zone, max_len=3):
    """Gabungan 2-3 box bersebelahan pada baris yang sama (nomor terpotong OCR)."""
    zone = sorted(zone, key=lambda b: (round(b.cy / 8), b.x0))
    out = []
    for i, b in enumerate(zone):
        h, txt, conf, x1 = b.y1 - b.y0, b.text, b.conf, b.x1
        for nxt in zone[i + 1:i + max_len]:
            if (min(nxt.y1, b.y1) - max(nxt.y0, b.y0) < 0.5 * h
                    or not 0 <= nxt.x0 - x1 < 1.5 * h):
                break
            txt, conf, x1 = txt + nxt.text, min(conf, nxt.conf), nxt.x1
            out.append((txt, conf, b))
    return out


def parse_nomor_kk(boxes, y_end, member_niks, page_h):
    """Nomor KK = 6 digit wilayah + 6 digit tanggal pencatatan (DDMMYY) + 4 urut.

    Dicari hanya di blok identitas (di atas header tabel-1), lalu kandidatnya
    dinilai: dekat label "No", dicetak lebih besar, tanggalnya masuk akal, kode
    wilayahnya sama dengan NIK anggota, dan bukan NIK anggota itu sendiri.
    """
    zone = [b for b in boxes if b.cy < (y_end or 0.30 * page_h)]
    if not zone:
        return "", None
    u = median([b.y1 - b.y0 for b in zone], 12.0)
    wilayah = collections.Counter(n[:6] for n in member_niks if len(n) == 16)

    cands = []
    for txt, conf, b in [(x.text, x.conf, x) for x in zone] + same_line_groups(zone):
        for v in digit_runs(txt):
            sc = 0.0
            if re.search(r"N[oO0]|N[oO0]\.|NO\s*KK|N[oO0][^0-9A-Za-z]", txt):
                sc += 3.0                            # label "No" menempel di box ini
            else:                                    # atau label berdiri sendiri di kirinya
                if any(sim(q.text, "No.") > 0.6 and 0 <= b.x0 - q.x1 < 4 * u
                       and abs(q.cy - b.cy) < 0.8 * u for q in zone):
                    sc += 3.0
            sc += 2.0 if (b.y1 - b.y0) > 1.3 * u else 0.0        # dicetak besar
            dd, mm = int(v[6:8]), int(v[8:10])
            sc += 2.0 if (1 <= dd <= 31 and 1 <= mm <= 12) else -2.0
            sc += 1.0 if v[:6] in wilayah else 0.0
            sc -= 5.0 if v in member_niks else 0.0
            sc += 1.0 if b.cy < 0.5 * (y_end or 0.30 * page_h) else 0.0
            cands.append((sc, -b.cy, v, conf))
    if not cands:
        return "", None
    sc, _, v, conf = max(cands)
    return (v, round(conf, 4)) if sc > 0 else ("", None)


def parse_tanggal_terbit(boxes, y_start):
    lab = [b for b in boxes if b.cy > y_start
           and sim(b.text.split(":")[0], "Dikeluarkan Tanggal") >= 0.6]
    if not lab:
        return "", None
    h = min(lab, key=lambda b: b.cy)
    for b in value_right_of(boxes, h, max_dx=15.0):
        if re.fullmatch(r"\d{2}-\d{2}-\d{4}", b.text.strip()):
            return b.text.strip(), b.conf
    return "", None


# ------------------------------------------------------------- 6. cross-checks
def nik_parts(nik):
    if not re.fullmatch(r"\d{16}", nik or ""):
        return None
    dd, mm, yy = int(nik[6:8]), int(nik[8:10]), int(nik[10:12])
    female = dd > 40
    return (dd - 40 if female else dd, mm, yy, "PEREMPUAN" if female else "LAKI-LAKI")


def consensus(cands):
    """Skor kemiripan total; seri -> pilih yang spasinya paling utuh."""
    cands = [c for c in cands if c]
    if not cands:
        return ""
    return max(cands, key=lambda c: (round(sum(sim(c, o) for o in cands), 3), c.count(" ")))


def repair_from_nik(m):
    """NIK memuat DDMMYY kelahiran (+40 pada DD untuk perempuan) -> perbaiki sel
    yang jelas salah baca. Kembalikan daftar perbaikan untuk audit."""
    p = nik_parts(m.get("nik", ""))
    if not p:
        return []
    fixes = []
    if m.get("jenis_kelamin") not in JENIS_KELAMIN:
        fixes.append(("jenis_kelamin", m.get("jenis_kelamin", ""), p[3]))
        m["jenis_kelamin"] = p[3]
    tgl = (m.get("tanggal_lahir") or "").strip()
    mo = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", tgl)
    now = __import__("datetime").date.today().year
    if mo and mo.group(3)[2:] == "%02d" % p[2]:
        year = mo.group(3)
    else:
        year = "%d" % (2000 + p[2] if 2000 + p[2] <= now else 1900 + p[2])
    fixed = "%02d-%02d-%s" % (p[0], p[1], year)
    if (mo or not tgl) and fixed != tgl:
        fixes.append(("tanggal_lahir", tgl, fixed))
        m["tanggal_lahir"] = fixed
    return fixes


def unify_names(members, extra, thr=0.88):
    """Nama yang sama muncul beberapa kali (baris, kolom ayah/ibu, tanda tangan).
    Kelompokkan varian yang nyaris sama, pakai satu ejaan konsensus."""
    pool = [v for m in members for k in ("nama_lengkap", "ayah", "ibu") for v in [m.get(k)] if v]
    pool += [e for e in extra if e]
    groups = []
    for v in pool:
        for g in groups:
            if sim(v, g[0]) >= thr:
                g.append(v)
                break
        else:
            groups.append([v])
    best = {}
    for g in groups:
        c = consensus(g)
        for v in g:
            best[v] = c
    fixes = []
    for i, m in enumerate(members):
        for k in ("nama_lengkap", "ayah", "ibu"):
            v = m.get(k)
            if v and best.get(v, v) != v:
                fixes.append((i + 1, k, v, best[v]))
                m[k] = best[v]
    return fixes


# ----------------------------------------------------------------- 7. pipeline
def structure(raw, debug=False):
    boxes = load_boxes(raw)
    if not boxes:
        return {}
    skew = estimate_shear(boxes)
    boxes = apply_shear(boxes, skew)
    page_h = max(b.y1 for b in boxes)
    page_w = max(b.x1 for b in boxes)

    (z1, hb1, k1, m1), (z2, hb2, k2, m2) = table_zones(boxes, page_h)
    a = parse_table(boxes, COLS_T1, z1, hb1, page_w, "nik", is_nik, k1, m1) if z1 else EMPTY_TABLE
    # nama anggota dari tabel-1 jadi vocab per dokumen untuk kolom ayah/ibu
    nama_dok = [r.get("nama_lengkap", "") for r, _ in a["rows"]
                if len(compact(r.get("nama_lengkap", ""))) >= 4]
    b = parse_table(boxes, COLS_T2, z2, hb2, page_w, "status_hubungan_dalam_keluarga",
                    is_hubungan, k2, m2, a["pitch"], nama_dok) if z2 else EMPTY_TABLE
    t1, r1, hdr1, lab1 = a["rows"], a["ranges"], a["headers"], a["labels"]
    t2, r2, lab2 = b["rows"], b["ranges"], b["labels"]
    ident_end = (hb1 or hb2 or (0, 0))[0] + 3

    ident = parse_identitas(boxes, min([h.y0 for h in hdr1] or [ident_end]) - 1)
    tgl_terbit = parse_tanggal_terbit(boxes, (z2 or z1 or (0, 0))[0])

    vocab = {k: v for k, _, v in COLS_T1 + COLS_T2}
    members, checks = [], []
    for i, (row, conf), (srow, sconf) in pair_rows(a, b):
        cell = dict(row)
        cell.update(srow)
        cfs = dict(conf)
        cfs.update(sconf)
        m = {}
        for k, _, _ in COLS_T1 + COLS_T2:
            m[k] = normalize(cell.get(k, ""), vocab.get(k))
        m["nik"] = compact(m["nik"])
        members.append((m, cfs))

        p = nik_parts(m["nik"])
        tgl = re.sub(r"[^\d-]", "", m.get("tanggal_lahir", ""))
        checks.append({
            "row": i + 1,
            "nik_valid": p is not None,
            "nik_vs_tanggal_lahir": bool(p) and tgl.startswith("%02d-%02d-" % (p[0], p[1]))
                                    and tgl.endswith("%02d" % p[2]),
            "nik_vs_jenis_kelamin": bool(p) and sim(m.get("jenis_kelamin", ""), p[3]) > 0.8,
        })

    niks = set(m["nik"] for m, _ in members)
    nomor_kk = parse_nomor_kk(boxes, min([h.y0 for h in hdr1] or [0.30 * page_h]) - 1,
                              niks, page_h)

    # nama kepala keluarga: konsensus header / baris KK / tanda tangan / ayah-nya anak
    row_kk = next((m["nama_lengkap"] for m, _ in members
                   if m.get("status_hubungan_dalam_keluarga") == "KEPALA KELUARGA"), "")
    ttd = find_header(boxes, "Tanda Tangan/Cap Jempol", 0.80 * page_h, page_h)
    sig = ""
    if ttd:
        above = [b for b in boxes if b.cy < ttd.cy and 0 < ttd.y0 - b.y1 < 18
                 and abs(b.cx - ttd.cx) < 90]
        sig = " ".join(b.text for b in sorted(above, key=lambda b: b.x0))
    child_father = next((m["ayah"] for m, _ in members
                         if m.get("status_hubungan_dalam_keluarga") == "ANAK"), "")
    kepala = consensus([ident["nama_kepala_keluarga"][0], row_kk, sig, child_father])

    # perbaikan silang (auditable lewat _meta.repairs)
    repairs = [(i + 1,) + f for i, (m, _) in enumerate(members) for f in repair_from_nik(m)]
    name_fixes = unify_names([m for m, _ in members],
                             [kepala, ident["nama_kepala_keluarga"][0], sig])
    kepala = consensus([kepala, ident["nama_kepala_keluarga"][0], sig, child_father])
    for m, _ in members:
        if m.get("status_hubungan_dalam_keluarga") == "KEPALA KELUARGA":
            m["nama_lengkap"] = kepala

    rt, _, rw = (ident["rt_rw"][0] or "").partition("/")
    out = {
        "nomor_kk": nomor_kk[0],
        "nama_kepala_keluarga": kepala,
        "alamat": ident["alamat"][0],
        "desa_kelurahan": ident["desa_kelurahan"][0],
        "rt": rt.strip(),
        "rw": rw.strip(),
        "kecamatan": ident["kecamatan"][0],
        "kabupaten_kota": ident["kabupaten_kota"][0],
        "provinsi": ident["provinsi"][0],
        "kode_pos": ident["kode_pos"][0],
        "tanggal_dikeluarkan": tgl_terbit[0],
        "anggota_keluarga": [m for m, _ in members],
    }
    if debug:
        conf = {"nomor_kk": nomor_kk[1], "tanggal_dikeluarkan": tgl_terbit[1]}
        conf.update({k: v[1] for k, v in ident.items()})
        conf["anggota_keluarga"] = [c for _, c in members]
        # CRF column-assignment confidence (forward-backward marginals) per member,
        # aligned with anggota_keluarga. This is SEPARATE from `conf` (OCR): it says
        # how decisively the Viterbi placed each cell, not how sure the OCR text is.
        # Computed entirely here in the debug branch by re-running pair_rows (pure and
        # deterministic, same result as the members loop above) so the value path is
        # untouched: `i` indexes a["kol_conf"], and each T2 srow is matched back to
        # b["kol_conf"] by object identity. Missing cells / template-absent path leave
        # a field as None.
        a_kol = a.get("kol_conf") or []
        b_kol = b.get("kol_conf") or []
        b_pos = {id(row_dict): pos for pos, (row_dict, _c) in enumerate(b["rows"])}
        crf_conf = []
        for i, (_r, _c), (srow, _sc) in pair_rows(a, b):
            cc = {}
            if i < len(a_kol) and a_kol[i]:
                cc.update(a_kol[i])
            bpos = b_pos.get(id(srow))
            if bpos is not None and bpos < len(b_kol) and b_kol[bpos]:
                cc.update(b_kol[bpos])
            crf_conf.append(cc)
        out["_meta"] = {
            "skew_deg": round(skew, 2),
            "conf": conf,
            "crf_conf": crf_conf,
            "row_checks": checks,
            "repairs": [{"row": r[0], "field": r[1], "ocr": r[2], "fixed": r[3]} for r in repairs],
            "name_fixes": [{"row": r[0], "field": r[1], "ocr": r[2], "fixed": r[3]} for r in name_fixes],
            "col_ranges_tabel1": {k: [round(v[0], 1), round(v[1], 1)] for k, v in r1.items()},
            "col_ranges_tabel2": {k: [round(v[0], 1), round(v[1], 1)] for k, v in r2.items()},
            "kepala_votes": [ident["nama_kepala_keluarga"][0], row_kk, sig, child_father],
        }
    return out


if __name__ == "__main__":
    path = sys.argv[1]
    d = json.load(open(path, encoding="utf-8"))
    raw = d["raw_ocr_output"] if isinstance(d, dict) else d
    print(json.dumps(structure(raw, debug="--debug" in sys.argv), indent=2, ensure_ascii=False))

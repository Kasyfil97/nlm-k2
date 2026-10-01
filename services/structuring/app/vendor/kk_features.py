#!/usr/bin/env python3
"""Ekstraksi fitur confidence per sel KK -- dipakai SAMA oleh latih dan inferensi.

Satu definisi fitur untuk keduanya. Memisahkannya adalah cara paling andal
membuat model yang bagus saat latih lalu buruk saat dipakai.

Parser TIDAK disentuh (berkas layanan adalah snapshot yang dipin lewat SHA-256),
jadi instrumentasinya monkeypatch di sekitar assign_columns_viterbi: fungsi asli
tetap yang menentukan NILAI dan PENEMPATAN, wrapper hanya menghitung ulang emisi
dan marginal forward-backward yang parser buang.

Kesetiaan hitung-ulang itu bisa diperiksa: parser menyimpan marginal sel
terpilih di `kol_conf`, dan `diff_maks()` melaporkan selisih maksimum terhadap
angka yang dihitung di sini. Selisih itu HARUS ~0; kalau tidak, fitur di sini
bukan yang parser lihat.
"""
import math
import re
import sys

# DIUBAH SAAT VENDOR (satu-satunya perubahan; lihat README.md di direktori ini).
# Aslinya memuat parser lewat importlib dari checkout K2Regex-v2. Di sini ia HARUS objek modul yang
# sama dengan yang dipakai `app/ml/kk_regex.py`, karena berkas ini mem-monkeypatch
# `assign_columns_viterbi`: dua salinan modul berarti adapter memanggil yang tidak ter-patch dan
# JEJAK tidak pernah terisi -- gagal diam-diam, tepat ke arah "semua confidence 0.0" lagi.
from app.vendor import kk_layout_parser as KK

# Sembilan field kontrak, dua keluarga model. Field anggota punya dua sinyal per
# sel (ocr + crf) dan seluruh internal CRF; field dokumen regex/posisional -- tak
# pernah lewat Viterbi, jadi `crf_conf` selalu null dan fiturnya lain sama sekali.
FIELDS = ["nama_lengkap", "nik", "pendidikan", "jenis_pekerjaan",
          "status_hubungan_dalam_keluarga", "ayah", "ibu"]
DOC_FIELDS = ["nomor_kk", "nama_kepala_keluarga"]
NEG = -1e9
CONFUSABLE = set("O0I1lS5B8Z2")

JEJAK = []
_ASLI = KK.assign_columns_viterbi
_DIFF_MAKS = [0.0]


def diff_maks():
    return _DIFF_MAKS[0]


# ------------------------------------------------------------ inti forward-backward
def _lse(a, b):
    if a <= NEG / 2:
        return b
    if b <= NEG / 2:
        return a
    hi, lo = (a, b) if a > b else (b, a)
    return hi + math.log1p(math.exp(lo - hi))


def _emisi(boxes, cols, span, nama_dok):
    """Salinan setia blok emisi parser (jalur bobot tangan, tanpa model file)."""
    out = []
    for b in boxes:
        t = b.x0
        baris = []
        for i, (k, _lab, vocab) in enumerate(cols):
            if span[i] is None:
                baris.append(NEG)
                continue
            lo, hi, sd = span[i]
            if lo < t <= hi:
                s = 0.0
            else:
                d = (lo - t) if t <= lo else (t - hi)
                s = -min((d / sd) ** 2, 50.0)
            if vocab:
                s += 3.0 if KK.normalize(b.text, vocab) in vocab else -1.0
            elif k == "nik":
                s += 3.0 if KK.is_nik(b.text) else -1.0
            elif k in ("ayah", "ibu") and nama_dok:
                s += max(0.0, KK.bonus_vocab(b.text, nama_dok, ambang=0.70))
            baris.append(s)
        out.append(baris)
    return out


def _marginal(emisi, n):
    """P(token j di kolom i | seluruh baris) untuk SEMUA i, bukan cuma terpilih.

    Inilah yang parser hitung lalu buang 16 dari 17 angkanya. Jarak ke kolom
    runner-up (`margin_min`) ada di sini, dan itu jauh lebih diskriminatif
    daripada marginal terpilih yang saturasi mendekati 1.0.
    """
    m = len(emisi)
    alpha = [[NEG] * n for _ in range(m)]
    beta = [[NEG] * n for _ in range(m)]
    for i in range(n):
        alpha[0][i] = emisi[0][i]
    for j in range(1, m):
        jalan = NEG
        for i in range(n):
            jalan = _lse(jalan, alpha[j - 1][i])
            alpha[j][i] = jalan + emisi[j][i]
    for i in range(n):
        beta[m - 1][i] = 0.0
    for j in range(m - 2, -1, -1):
        jalan = NEG
        for i in range(n - 1, -1, -1):
            jalan = _lse(jalan, emisi[j + 1][i] + beta[j + 1][i])
            beta[j][i] = jalan
    Z = NEG
    for i in range(n):
        Z = _lse(Z, alpha[m - 1][i])
    P = []
    for j in range(m):
        row = []
        for i in range(n):
            lp = alpha[j][i] + beta[j][i] - Z
            row.append(math.exp(lp) if lp < 0 else 1.0)
        P.append(row)
    return P


def _jejak_viterbi(row_boxes, cols, ranges, tpl, W, nama_dok=()):
    hasil = _ASLI(row_boxes, cols, ranges, tpl, W, nama_dok)
    try:
        if not hasil or not isinstance(hasil, tuple):
            return hasil
        grup, yakin = hasil
        boxes = sorted(row_boxes, key=lambda b: b.x0)
        span = []
        for k, _lab, _v in cols:
            if k in ranges:
                lo, hi = ranges[k]
                sd = max((tpl.get(k) or (0, 0.05))[1], 0.02) * W
                span.append((lo, hi, sd))
            else:
                span.append(None)
        emisi = _emisi(boxes, cols, span, nama_dok)
        P = _marginal(emisi, len(cols))
        kol_of = {}
        for ki, (k, _l, _v) in enumerate(cols):
            for b in grup.get(k, ()):
                kol_of[id(b)] = ki
        JEJAK.append({"cols": cols, "boxes": boxes, "span": span, "emisi": emisi,
                      "P": P, "kol_of": kol_of, "yakin": yakin, "W": W})
    except Exception as e:                    # instrumentasi tidak boleh merusak parsing
        print("  [warn] jejak gagal: %r" % (e,), file=sys.stderr)
    return hasil


KK.assign_columns_viterbi = _jejak_viterbi


# ------------------------------------------------------------------- fitur sel
def _kandidat_vocab(text, vocab, thr=0.62):
    """Kandidat vocab berperingkat. Penyaring normalize() dipakai ulang supaya
    `cocok`/gap/ties berarti hal yang sama dengan yang dipakai parser."""
    if not text or not vocab:
        return []
    ca = KK.compact(text)
    kand = []
    for i, v in enumerate(vocab):
        lb = len(KK.compact(v))
        if lb and 2.0 * min(len(ca), lb) / (len(ca) + lb) >= thr:
            kand.append((KK.sim(text, v), -i, v))
    layak = []
    for skor, _negidx, v in sorted(kand, reverse=True):
        if skor < thr:
            break
        cocok = skor * (len(ca) + len(KK.compact(v))) / 2.0
        if skor < 0.8 and len(KK.compact(v)) >= 7 and cocok < 6:
            continue
        layak.append((skor, v, cocok))
    return layak


def _teks_fitur(txt):
    ca = [c for c in txt if not c.isspace()]
    n = max(len(ca), 1)
    return {
        "txt_len": float(len(txt)),
        "digit_ratio": sum(c.isdigit() for c in ca) / n,
        "nonalnum_ratio": sum(not c.isalnum() for c in ca) / n,
        "n_space": float(txt.count(" ")),
        "confusable_ratio": sum(c in CONFUSABLE for c in ca) / n,
    }


def fitur_baris(je):
    """{key: fitur} untuk satu baris tabel, termasuk agregat level baris."""
    cols, boxes, span, emisi, P = je["cols"], je["boxes"], je["span"], je["emisi"], je["P"]
    kol_of, n = je["kol_of"], len(je["cols"])
    h_med = KK.median([b.y1 - b.y0 for b in boxes], 1.0) or 1.0

    per_kol = {}
    for j, b in enumerate(boxes):
        ki = kol_of.get(id(b))
        if ki is not None:
            per_kol.setdefault(ki, []).append(j)

    dipakai = sorted(per_kol)
    skipped = sum(dipakai[i + 1] - dipakai[i] - 1 for i in range(len(dipakai) - 1))
    marg_terpakai = [P[j][kol_of[id(boxes[j])]] for j in range(len(boxes))
                     if id(boxes[j]) in kol_of]

    hasil = {}
    for ki, idxs in per_kol.items():
        key, _lab, vocab = cols[ki]
        lo, hi, sd = span[ki]
        bs = [boxes[j] for j in idxs]
        txt = " ".join(b.text for b in bs).strip()

        margs = [P[j][ki] for j in idxs]
        margin, logit_margin, ent, egap = [], [], [], []
        for j in idxs:
            lain = max([P[j][i] for i in range(n) if i != ki] or [0.0])
            margin.append(P[j][ki] - lain)
            logit_margin.append(math.log((P[j][ki] + 1e-12) / (lain + 1e-12)))
            e = [x for x in P[j] if x > 1e-12]
            ent.append(-sum(x * math.log(x) for x in e))
            el = max([emisi[j][i] for i in range(n) if i != ki] or [NEG])
            egap.append(emisi[j][ki] - el if el > NEG / 2 else 50.0)

        confs = [b.conf for b in bs]
        lay = _kandidat_vocab(txt, vocab) if vocab else []
        ternorm = KK.normalize(txt, vocab) if vocab else txt

        f = {
            # --- CRF: struktur keputusan, bukan cuma hasilnya
            "marg_min": min(margs),
            "marg_mean": sum(margs) / len(margs),
            "margin_min": min(margin),
            "margin_mean": sum(margin) / len(margin),
            "logit_margin_min": max(-30.0, min(30.0, min(logit_margin))),
            "entropy_max": max(ent),
            "entropy_mean": sum(ent) / len(ent),
            "emis_gap_min": min(egap),
            "emis_assigned_min": min(emisi[j][ki] for j in idxs),
            "n_tokens": float(len(idxs)),
            # --- geometri
            "inside_frac": sum(1.0 for b in bs if lo < b.x0 <= hi) / len(bs),
            "dist_sigma_max": max(0.0 if lo < b.x0 <= hi else
                                  min(((lo - b.x0) if b.x0 <= lo else (b.x0 - hi)) / max(sd, 1e-6), 8.0)
                                  for b in bs),
            # >1.0 menandai sel tergabung -- ini yang menangkap ayah+ibu menyatu
            "lebar_rel": min((max(b.x1 for b in bs) - min(b.x0 for b in bs)) / max(hi - lo, 1e-6), 4.0),
            "h_rel": (sum(b.y1 - b.y0 for b in bs) / len(bs)) / h_med,
            # --- OCR
            "ocr_min": min(confs),
            "ocr_mean": sum(confs) / len(confs),
            "ocr_spread": max(confs) - min(confs),
            "n_low_conf": float(sum(1 for c in confs if c < 0.95)),
            # --- leksikon (berjenjang: ditolak sebagai EMISI, sah sebagai FITUR)
            "has_vocab": 1.0 if vocab else 0.0,
            "vocab_size": float(len(vocab or ())),
            "vocab_sim": lay[0][0] if lay else 0.0,
            "vocab_gap": (lay[0][0] - lay[1][0]) if len(lay) > 1 else (1.0 if lay else 0.0),
            "vocab_nties": float(sum(1 for s, _v, _c in lay if lay and lay[0][0] - s <= 0.05)),
            "vocab_cocok_char": lay[0][2] if lay else 0.0,
            "vocab_ok": 1.0 if (vocab and ternorm in vocab) else 0.0,
            "norm_changed": 0.0 if ternorm == txt else 1.0,
            "is_nik_ok": 1.0 if KK.is_nik(txt) else 0.0,
            # --- level baris: kesalahan penempatan datang berkelompok
            "row_n_cells": float(len(per_kol)),
            "row_n_tokens": float(len(boxes)),
            "row_cols_skipped": float(skipped),
            "row_marg_mean": sum(marg_terpakai) / max(len(marg_terpakai), 1),
            "row_marg_min": min(marg_terpakai or [0.0]),
        }
        f.update(_teks_fitur(txt))
        f["_text"] = txt
        hasil[key] = f
    return hasil


# ------------------------------------------------------------------ level dokumen
def norm_banding(s):
    """Normalisasi untuk perbandingan EXACT: kapital, spasi dirapikan."""
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def boxes_dari_v6(path):
    import json
    page = json.load(open(path, encoding="utf-8"))["pages"][0]
    return [[t["poly"], [t["text"], float(t["score"])]] for t in page["texts"]]


def agregat_ocr(raw):
    """text_regions_count / avg / min seperti `ocr_common.kk.ocr_aggregates`.

    None untuk raw kosong, bukan 0: gambar tanpa teks terbaca adalah penolakan di
    structuring, bukan dokumen yang skornya nol.
    """
    skor = [float(b[1][1]) for b in raw if b[1][1] is not None]
    if not skor:
        return {"text_regions_count": len(raw), "avg_doc_score": None, "min_doc_score": None}
    return {"text_regions_count": len(raw),
            "avg_doc_score": round(sum(skor) / len(skor), 4),
            "min_doc_score": round(min(skor), 4)}


def fitur_doc_fields(out, agregat, guardrail_probability=None):
    """Fitur dua field dokumen kontrak: nomor_kk dan nama_kepala_keluarga.

    Keluarga fitur yang berbeda dari sel anggota, dan itu bukan pilihan gaya:
    `crf_conf` memang tidak ada untuk field dokumen, jadi bukti penempatan harus
    datang dari tempat lain. Untuk `nama_kepala_keluarga` tempat itu adalah
    `kepala_votes` -- empat saksi independen (label identitas, baris KEPALA
    KELUARGA, tanda tangan, `ayah` milik ANAK) yang `consensus()` pilih satu di
    antaranya. Jumlah saksi yang sepakat adalah redundansi sejati, setara peran
    NIK-vs-tanggal-lahir di sel anggota.
    """
    meta = out.get("_meta") or {}
    conf = meta.get("conf") or {}
    votes = [v for v in (meta.get("kepala_votes") or [])]
    niks = {(a.get("nik") or "") for a in (out.get("anggota_keluarga") or [])}
    namafix = {r["field"] for r in (meta.get("name_fixes") or [])}
    n_anggota = len(out.get("anggota_keluarga") or [])

    baris = []
    for field in DOC_FIELDS:
        val = str(out.get(field) or "")
        f = {
            "ocr_conf": float(conf[field]) if isinstance(conf.get(field), (int, float)) else float("nan"),
            "is_16digit": 1.0 if KK.compact(val).isdigit() and len(KK.compact(val)) == 16 else 0.0,
            # nomor KK yang sama dengan NIK seorang anggota adalah pola nyata di
            # korpus, bukan anomali -- dan sinyal kuat bahwa angkanya terbaca utuh
            "sama_dengan_nik": 1.0 if val and val in niks else 0.0,
            "n_votes_terisi": float(sum(1 for v in votes if v)),
            "n_votes_setuju": float(sum(1 for v in votes if v and KK.sim(v, val) >= 0.88)),
            "votes_sim_maks": max([KK.sim(v, val) for v in votes if v] or [0.0]),
            "name_fixed": 1.0 if field in namafix else 0.0,
            "avg_doc_score": _f(agregat.get("avg_doc_score")),
            "min_doc_score": _f(agregat.get("min_doc_score")),
            "text_regions_count": _f(agregat.get("text_regions_count")),
            "guardrail_probability": _f(guardrail_probability),
            "n_anggota": float(n_anggota),
            "doc_skew_abs": abs(float(meta.get("skew_deg") or 0.0)),
        }
        f.update(_teks_fitur(val))
        f["field"] = field
        f["pred"] = val
        baris.append(f)
    return baris


def _f(x):
    """Angka opsional -> float, dengan NaN untuk yang tidak ada.

    NaN, bukan 0 atau -1: HistGradientBoosting menangani nilai hilang secara
    native, dan menyandikannya sebagai angka nyata akan membuat "tidak ada
    guardrail" terbaca sebagai "guardrail bilang 0.0".
    """
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else float("nan")


def fitur_dokumen(raw):
    """(out_parser, [{field, member, pred, ...fitur}]) untuk satu dokumen.

    Sel jejak dihubungkan ke anggota lewat `_meta.crf_conf`: angka di situ adalah
    round(min marginal, 4) dari sel yang sama, jadi (field, nilai bulat) menunjuk
    balik ke satu sel jejak tanpa perlu menebak urutan iterasi baris di parser.
    """
    JEJAK.clear()
    out = KK.structure(raw, debug=True)
    if not out or not out.get("anggota_keluarga"):
        return out, []
    meta = out.get("_meta") or {}

    idx = {}
    for je in JEJAK:
        for key, f in fitur_baris(je).items():
            yp = je["yakin"].get(key)
            if yp is not None:
                _DIFF_MAKS[0] = max(_DIFF_MAKS[0], abs(yp - f["marg_min"]))
            idx.setdefault((key, round(yp if yp is not None else -1, 4)), []).append(f)

    anggota = out["anggota_keluarga"]
    crf = meta.get("crf_conf") or []
    checks = {c["row"]: c for c in (meta.get("row_checks") or [])}
    repaired = {(r["row"], r["field"]) for r in (meta.get("repairs") or [])}
    namafix = {(r["row"], r["field"]) for r in (meta.get("name_fixes") or [])}
    nama_dok = [m.get("nama_lengkap", "") for m in anggota]
    niks = [m.get("nik", "") for m in anggota]

    baris = []
    for i, m in enumerate(anggota):
        cc = crf[i] if i < len(crf) else {}
        ck = checks.get(i + 1, {})
        for field in FIELDS:
            conf = cc.get(field)
            if conf is None:
                continue
            kand = idx.get((field, round(conf, 4)))
            if not kand:
                continue
            pred = m.get(field, "") or ""
            sama = [x for x in kand if norm_banding(x["_text"]) == norm_banding(pred)]
            f = dict((sama or kand)[0])
            f.pop("_text", None)
            f.update({
                "nik_valid": 1.0 if ck.get("nik_valid") else 0.0,
                "nik_vs_tgl": 1.0 if ck.get("nik_vs_tanggal_lahir") else 0.0,
                "nik_vs_gender": 1.0 if ck.get("nik_vs_jenis_kelamin") else 0.0,
                "was_repaired": 1.0 if (i + 1, field) in repaired else 0.0,
                "name_fixed": 1.0 if (i + 1, field) in namafix else 0.0,
                "nik_dup": 1.0 if niks.count(m.get("nik", "")) > 1 else 0.0,
                # redundansi yang sudah terukur: ~80% nama orang tua baris ANAK
                # adalah persis nama anggota lain di dokumen yang sama
                "parent_match": (max((KK.sim(pred, nm) for nm in nama_dok if nm), default=0.0)
                                 if field in ("ayah", "ibu") else 0.0),
                "doc_skew_abs": abs(float(meta.get("skew_deg") or 0.0)),
                "doc_n_members": float(len(anggota)),
                "row_pos_rel": i / max(len(anggota) - 1, 1),
                "crf_conf": float(conf),
                "field": field,
                "member": i,
                "pred": pred,
            })
            baris.append(f)
    return out, baris

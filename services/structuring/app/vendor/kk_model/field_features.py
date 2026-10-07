"""
Fitur per FIELD untuk model skor akhir (final_conf = P(field benar secara struktur DAN nilai)).

Satu baris = satu field terisi (punya kotak) pada keluaran structuring: no_kk, nama_kepala_keluarga, dan
7 field tiap anggota. Semua fitur hanya memakai yang tersedia saat inferensi: OCR v6, probabilitas kotak dari
model structuring, hasil pengelompokan, dan kosakata tertutup.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import numpy as np

from .features import C2I, CLASSES, KEYS, norm
from .structure import CLOSED, MEMBER_KEYS, field

HEADER = ["no_kk", "nama_kepala_keluarga"]
FIELDS = HEADER + MEMBER_KEYS
EPS = 1e-6

FEATS_V1 = [
    # struktur (probabilitas kotak dari model structuring)
    "lg_conf", "lg_struct_min", "lg_pk_min", "lg_pk_mean", "margin_min", "n_boxes", "filled",
    # OCR
    "lg_ocr_min", "lg_ocr_mean",
    # kosakata tertutup (0 untuk field terbuka)
    "snapped", "sim", "raw_in_vocab",
    # bentuk nilai
    "len_norm", "digit_frac", "alpha_frac", "n_tok", "fmt16", "nik_date_ok", "region_match",
    # konteks dokumen
    "n_members", "key_coverage", "member_rel",
]

# v2: dari analisis error (struktur benar, nilai salah = salah baca OCR, mayoritas pada nama orang tua)
FEATS_V2 = [
    # silang antarfield nama (ayah anak == nama kepala, ibu == nama istri, kepala == nama_lengkap anggota kepala)
    "xname_exact", "xname_near", "xname_any",
    # konsistensi NIK
    "nik_dup", "nik_gender", "nik_tail0",
    # kosakata: ambiguitas (selisih kemiripan entri terbaik vs kedua)
    "sim_gap",
    # bentuk teks mentah OCR
    "n_weird", "has_lower", "has_label", "digit_in_name", "last_tok_len",
    # geometri: kerapatan huruf vs median dokumen (huruf hilang/terpotong), kotak kandidat yang tertinggal
    "cpw_rel", "cpw_dev", "cand_pk_max", "cand_n", "edge_gap",
    # kualitas OCR dokumen
    "doc_ocr_mean", "doc_ocr_p10", "member_n_filled",
]
NUM_FEATS = FEATS_V1 + FEATS_V2
NAME_KEYS = ("nama_kepala_keluarga", "nama_lengkap", "ayah", "ibu")
LABELS = ("NAMAKEPALAKELUARGA", "NAMALENGKAP", "NAMAAYAH", "NAMAIBU", "JENISPEKERJAAN", "PENDIDIKAN")
FEMALE_STATUS, MALE_STATUS = ("ISTRI",), ("SUAMI", "KEPALAKELUARGA")


def logit(p):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def nik_date_ok(d: str) -> float:
    """NIK digit 7-12 = DDMMYY (perempuan DD+40)."""
    if len(d) != 16:
        return 0.0
    dd, mm = int(d[6:8]), int(d[8:10])
    dd = dd - 40 if dd > 40 else dd
    return float(1 <= dd <= 31 and 1 <= mm <= 12)


def field_row(key, ids, member, doc, P, st, lex, no_kk_digits=""):
    """-> dict fitur + nilai akhir (sama dengan structuring to_json/rich_field)."""
    texts, conf = doc["texts"], st["_conf"]
    use_lex = lex if (member is not None and key in CLOSED) else None
    f = field(key, ids, texts, conf, use_lex)
    k = C2I[key]
    pk = np.array([P[i, k] for i in ids])
    other = np.array([np.delete(P[i], k).max() for i in ids])
    ocr = np.array([texts[i]["score"] for i in ids])
    raw = " ".join(texts[i]["text"] for i in ids)
    v = f["value"]
    nv = norm(v)
    snapped, sim, raw_in_vocab = 0.0, 0.0, 0.0
    if use_lex is not None:
        best, sim = use_lex.best(key, raw)
        snapped = float(use_lex.snap(key, raw)[1] is not None)
        raw_in_vocab = float(norm(raw) in {norm(x) for x in use_lex.vocab.get(key, [])})
    digits = re.sub(r"\D", "", v)
    n_mem = len(st["members"])
    cov = (sum(1 for m in st["members"] if m[key]) / n_mem) if (member is not None and n_mem) else 1.0
    return {
        "member": "" if member is None else member, "field": key, "value": v, "box_ids": list(map(int, ids)),
        "conf": f["conf"], "structuring_conf": float(min(conf[i] for i in ids)), "ocr_conf": float(ocr.min()),
        "lg_conf": float(logit(f["conf"])), "lg_struct_min": float(logit(min(conf[i] for i in ids))),
        "lg_pk_min": float(logit(pk.min())), "lg_pk_mean": float(logit(pk.mean())),
        "margin_min": float((pk - other).min()), "n_boxes": len(ids),
        "filled": float(any(CLASSES[int(P[i].argmax())] != key for i in ids)),
        "lg_ocr_min": float(logit(ocr.min())), "lg_ocr_mean": float(logit(ocr.mean())),
        "snapped": snapped, "sim": float(sim), "raw_in_vocab": raw_in_vocab,
        "len_norm": len(nv), "digit_frac": sum(c.isdigit() for c in nv) / max(len(nv), 1),
        "alpha_frac": sum(c.isalpha() for c in nv) / max(len(nv), 1), "n_tok": len(v.split()),
        "fmt16": float(len(digits) == 16) if key in ("no_kk", "nik") else 0.0,
        "nik_date_ok": nik_date_ok(digits) if key == "nik" else 0.0,
        "region_match": float(len(digits) == 16 and len(no_kk_digits) == 16 and digits[:6] == no_kk_digits[:6])
        if key == "nik" else 0.0,
        "n_members": n_mem, "key_coverage": cov,
        "member_rel": (member / max(n_mem - 1, 1)) if member is not None else 0.0,
        "_raw": raw,
    }


def _width(t) -> float:
    (x0, y0), (x1, y1) = t["poly"][0], t["poly"][1]
    return max(float(np.hypot(x1 - x0, y1 - y0)), 1.0)


def add_v2(rows, doc, P, st, lex, aux):
    """Fitur v2 (butuh semua field dokumen + geometri aux dari featurize/predict_doc)."""
    texts, g, hmed, W = doc["texts"], aux["g"], aux["hmed"], doc["W"]
    sc = np.array([t["score"] for t in texts])
    cpw_all = [len(norm(t["text"])) / _width(t) for t in texts if len(norm(t["text"])) >= 3]
    cpw_med = float(np.median(cpw_all)) if cpw_all else 1.0
    used = set(st["no_kk"]) | set(st["nama_kepala_keluarga"])
    used |= {i for m in st["members"] for k in MEMBER_KEYS for i in m[k]}
    free = np.array(sorted(set(range(len(texts))) - used), int)
    names = [(j, norm(r["value"])) for j, r in enumerate(rows) if r["field"] in NAME_KEYS and norm(r["value"])]
    niks = [re.sub(r"\D", "", r["value"]) for r in rows if r["field"] == "nik"]
    status = {r["member"]: norm(r["value"]) for r in rows if r["field"] == "status_hubungan_dalam_keluarga"}
    filled = {}
    for r in rows:
        filled[r["member"]] = filled.get(r["member"], 0) + 1
    for j, r in enumerate(rows):
        key, ids, nv, raw = r["field"], r["box_ids"], norm(r["value"]), r.pop("_raw")
        # silang nama
        ex, near = 0, 0.0
        if key in NAME_KEYS and nv:
            for jj, o in names:
                if jj == j:
                    continue
                if o == nv:
                    ex += 1
                else:
                    near = max(near, SequenceMatcher(None, nv, o).ratio())
        r["xname_exact"], r["xname_near"] = float(ex), near
        r["xname_any"] = float(ex > 0 or near > 0) if key in NAME_KEYS else 0.0
        # NIK
        d = re.sub(r"\D", "", r["value"])
        r["nik_dup"] = float(key == "nik" and len(d) == 16 and niks.count(d) > 1)
        gen = 0.0
        if key == "nik" and len(d) == 16:
            female = int(d[6:8]) > 40
            stt = status.get(r["member"], "")
            if stt in FEMALE_STATUS:
                gen = 1.0 if female else -1.0
            elif stt in MALE_STATUS:
                gen = 1.0 if not female else -1.0
        r["nik_gender"] = gen
        r["nik_tail0"] = float(key == "nik" and len(d) == 16 and d[-4:] == "0000")
        # kosakata: ambiguitas
        gap = 0.0
        if lex is not None and r["member"] != "" and key in CLOSED and lex.vocab.get(key):
            nr = norm(raw)
            sims = sorted((SequenceMatcher(None, nr, norm(v)).ratio() for v in lex.vocab[key]), reverse=True)
            gap = sims[0] - (sims[1] if len(sims) > 1 else 0.0)
        r["sim_gap"] = gap
        # teks mentah
        r["n_weird"] = float(len(re.findall(r"[^A-Za-z0-9 .,/'()\-:]", raw)))
        r["has_lower"] = float(bool(re.search(r"[a-z]", raw)))
        nraw = norm(raw)
        r["has_label"] = float(any(lb in nraw for lb in LABELS))
        r["digit_in_name"] = float(key in NAME_KEYS and bool(re.search(r"\d", raw)))
        toks = re.findall(r"[A-Za-z]+", r["value"])
        r["last_tok_len"] = float(len(toks[-1])) if toks else 0.0
        # geometri
        wsum = sum(_width(texts[i]) for i in ids)
        cpw = len(nraw) / wsum / cpw_med if cpw_med else 1.0
        r["cpw_rel"], r["cpw_dev"] = cpw, abs(float(np.log(max(cpw, 1e-3))))
        fcy = float(np.mean(g[ids, 5]))
        lo, hi = float(g[ids, 0].min()), float(g[ids, 1].max())
        cand = free[(np.abs(g[free, 5] - fcy) <= 0.6 * hmed) & (g[free, 1] >= lo - 0.06 * W)
                    & (g[free, 0] <= hi + 0.06 * W)] if len(free) else free
        cand = [i for i in cand if aux["alnum"][i]]
        r["cand_pk_max"] = float(max((P[i, C2I[key]] for i in cand), default=0.0))
        r["cand_n"] = float(len(cand))
        right = free[(np.abs(g[free, 5] - fcy) <= 0.6 * hmed) & (g[free, 0] >= hi - 2)] if len(free) else free
        r["edge_gap"] = min(float((g[right, 0].min() - hi) / hmed), 20.0) if len(right) else 20.0
        r["doc_ocr_mean"], r["doc_ocr_p10"] = float(sc.mean()), float(np.percentile(sc, 10))
        r["member_n_filled"] = float(filled.get(r["member"], 0))


def doc_rows(doc, P, st, lex, aux):
    """Semua field TERISI satu dokumen -> list dict. Field tanpa kotak tidak masuk (final_conf = 0 by rule)."""
    out = []
    nk = st["no_kk"]
    nk_digits = re.sub(r"\D", "", field("no_kk", nk, doc["texts"], st["_conf"])["value"]) if nk else ""
    for k in HEADER:
        if st[k]:
            out.append(field_row(k, st[k], None, doc, P, st, lex, nk_digits))
    for m, mem in enumerate(st["members"]):
        for k in MEMBER_KEYS:
            if mem[k]:
                out.append(field_row(k, mem[k], m, doc, P, st, lex, nk_digits))
    add_v2(out, doc, P, st, lex, aux)
    return out


def design(df, inter=(), feats=None):
    """DataFrame baris field -> matriks fitur (numerik `feats` + one-hot field + interaksi field x fitur)."""
    cols = {c: df[c].astype(float).to_numpy() for c in (feats or NUM_FEATS)}
    for k in FIELDS:
        oh = (df.field == k).to_numpy(float)
        cols[f"is_{k}"] = oh
        for c in inter:
            cols[f"{k}*{c}"] = oh * df[c].astype(float).to_numpy()
    names = list(cols)
    return np.column_stack([cols[c] for c in names]), names


assert set(KEYS) == set(FIELDS)

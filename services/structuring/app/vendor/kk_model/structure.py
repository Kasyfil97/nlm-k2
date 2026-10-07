"""
Salinan structuring/training/structure.py (logika tidak diubah) agar scoring tidak mengimpor modul dari folder
lain. Model structuring dimuat dari salinan artefaknya: scoring/training/artifacts/structuring/<id>/model.joblib
(disalin oleh build_dataset.py). Bila aturan di structuring berubah, salin ulang file ini.

Inferensi ujung-ke-ujung: keluaran OCR v6 -> probabilitas key per kotak (model terlatih) -> pengelompokan ke
anggota keluarga -> JSON akhir.

  from structure import load_model, predict_doc, group_boxes, to_json
  model = load_model("m04_06102026")
  P, aux = predict_doc(model, doc)            # doc = {"texts": [...], "W": int, "H": int}
  st = group_boxes(doc, P, aux)               # struktur dengan id kotak
  out = to_json(doc, st)                      # JSON skema {"no_kk": {"value","conf"}, ...}

Aturan pengelompokan (semua koordinat sudah dikoreksi kemiringan):
  * Anggota = kluster baris dari kotak berkelas nama_lengkap (urut dari atas); n anggota = jumlah kluster.
  * Kolom tabel-1 (nik, pendidikan, pekerjaan): kotak ke anggota dengan baris nama terdekat setelah
    dikurangi offset median kolom itu (menyerap kemiringan/lengkung).
  * Tabel-2 (status, ayah, ibu): baris status = jangkar tabel-2 (tiap anggota punya status); kotak ayah/ibu
    ke baris status terdekat setelah offset median kolom.
  * Header: no_kk = kotak berkonfiden tertinggi; kepala keluarga = kluster baris berkonfiden rata-rata tertinggi.
  * conf field = min probabilitas kelas pada kotak-kotak penyusunnya; field kosong -> value "" dan conf 0.0.
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

import joblib
import numpy as np

from .features import CLASSES, C2I, KEYS, context_feats, featurize, neighbors

HERE = Path(__file__).resolve().parent
STRUCT_ART = HERE / "artifacts" / "structuring"
MEMBER_KEYS = ["nama_lengkap", "nik", "pendidikan", "pekerjaan", "status_hubungan_dalam_keluarga", "ayah", "ibu"]
TABLE1 = ["nik", "pendidikan", "pekerjaan"]
TABLE2 = ["ayah", "ibu"]


def load_model(exp_id: str):
    """Artefak model structuring. Pickle-nya merujuk `pipeline.Full` dan `pipeline.shape` (didefinisikan ulang di
    scoring/training/pipeline.py dengan isi yang sama)."""
    import pipeline  # noqa: F401  (modul yang dirujuk pickle)
    return joblib.load(STRUCT_ART / exp_id / "model.joblib")


def predict_doc(model, doc: dict):
    """Probabilitas key per kotak, mereplikasi rantai pelatihan: [teks] -> tahap-1 -> konteks_k."""
    X, _, aux = featurize(doc)
    nb = neighbors(aux["g"], aux["row"])
    ms = model["models"]
    if "text" in ms:
        T = ms["text"].predict_proba(np.array([t["text"] for t in doc["texts"]], dtype=object))
        X = np.hstack([X, T])
    P = ms["stage1"].predict_proba(X)
    r = 1
    while f"ctx{r}" in ms:
        X = np.hstack([X, context_feats(P, nb, aux["row"])])
        P = ms[f"ctx{r}"].predict_proba(X)
        r += 1
    return P, aux


def clusters(idx, cy, thr):
    """Kelompokkan indeks kotak ke baris menurut cy (celah > thr = baris baru)."""
    out = []
    for i in sorted(idx, key=lambda i: cy[i]):
        if out and cy[i] - cy[out[-1][-1]] <= thr:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def assign_monotone(rows_cy, anchor_cy):
    """Cocokkan baris kolom (urut atas->bawah) ke anggota dengan menjaga urutan. m == n -> identitas.
    m < n: pilih anggota mana yang punya sel; m > n: pilih baris mana yang dipakai. Skor = sebaran
    (cy baris - cy jangkar) di sekitar mediannya, seri -> paling dekat identitas.
    Hasil: daftar pasangan (indeks_baris, indeks_anggota)."""
    from itertools import combinations
    m, n = len(rows_cy), len(anchor_cy)
    if m == 0 or n == 0:
        return []
    if m == n:
        return [(j, j) for j in range(m)]
    k = min(m, n)
    big, small = (n, m) if m < n else (m, n)
    from math import comb
    if comb(big, k) > 6000:
        return [(j, int(np.argmin([abs(rows_cy[j] - a) for a in anchor_cy]))) for j in range(m)]
    best = None
    for c in combinations(range(big), k):
        pairs = [(j, c[j]) for j in range(k)] if m < n else [(c[j], j) for j in range(k)]
        d = [rows_cy[r] - anchor_cy[mm] for r, mm in pairs]
        med = statistics.median(d)
        cost = (sum(abs(x - med) for x in d), sum(abs(r - mm) for r, mm in pairs))
        if best is None or cost < best[0]:
            best = (cost, pairs)
    return best[1]


FILL_KEYS = ["nik", "pendidikan", "pekerjaan", "status_hubungan_dalam_keluarga", "ayah", "ibu"]


def complete_cells(st, P, aux, name_cy, anchor2, pitch, tau):
    """Sel anggota yang kosong: ambil kotak berprediksi 'O' pada baris/kolom yang diharapkan bila
    P(key) >= tau (dekode terstruktur: hampir semua anggota punya NIK, status, dll.)."""
    if tau is None:
        return
    g = aux["g"]
    cy, x0, x1 = g[:, 5], g[:, 0], g[:, 1]
    pred = P.argmax(1)
    used = {i for m in st["members"] for k in MEMBER_KEYS for i in m[k]}
    for key in FILL_KEYS:
        anchors = anchor2 if key in ("status_hubungan_dalam_keluarga", "ayah", "ibu") else name_cy
        have = [(k, m[key]) for k, m in enumerate(st["members"]) if m[key]]
        if not have or len(have) == len(st["members"]):
            continue
        off = statistics.median(float(np.mean([cy[i] for i in ids])) - anchors[k] for k, ids in have)
        lo = min(x0[i] for _, ids in have for i in ids) - 6
        hi = max(x1[i] for _, ids in have for i in ids) + 6
        for k, m in enumerate(st["members"]):
            if m[key]:
                continue
            exp = anchors[k] + off
            cand = [i for i in range(len(cy)) if i not in used and CLASSES[pred[i]] == "O"
                    and abs(cy[i] - exp) <= 0.5 * pitch and lo <= (x0[i] + x1[i]) / 2 <= hi
                    and P[i, C2I[key]] >= tau and aux["alnum"][i]]
            if cand:
                i = max(cand, key=lambda i: P[i, C2I[key]])
                m[key] = [i]
                used.add(i)


def group_boxes(doc: dict, P: np.ndarray, aux: dict, fill_tau: float | None = 0.05, joint_parents: bool = False) -> dict:
    g, hmed = aux["g"], aux["hmed"]
    cy, cx = g[:, 5], g[:, 4]
    pred = P.argmax(1)
    conf = P.max(1)
    by = {k: [i for i in range(len(pred)) if CLASSES[pred[i]] == k] for k in KEYS}
    thr = 0.6 * hmed

    st = {"no_kk": [], "nama_kepala_keluarga": [], "members": [], "n_name_rows": 0, "_conf": conf}
    if by["no_kk"]:
        st["no_kk"] = [max(by["no_kk"], key=lambda i: conf[i])]
    if by["nama_kepala_keluarga"]:
        cl = clusters(by["nama_kepala_keluarga"], cy, thr)
        st["nama_kepala_keluarga"] = max(cl, key=lambda c: np.mean([conf[i] for i in c]))

    names = clusters(by["nama_lengkap"], cy, thr)
    if len(names) > 1:                     # baris rapat: pisahkan ulang dengan ambang berbasis jarak baris
        pitch0 = statistics.median(np.diff([float(np.mean([cy[i] for i in c])) for c in names]))
        names = clusters(by["nama_lengkap"], cy, min(thr, 0.5 * pitch0))
    n = len(names)
    st["n_name_rows"] = n
    st["members"] = [{k: [] for k in MEMBER_KEYS} for _ in range(n)]
    if not n:
        return st
    for k, c in enumerate(names):
        st["members"][k]["nama_lengkap"] = c
    name_cy = [float(np.mean([cy[i] for i in c])) for c in names]
    pitch = statistics.median(np.diff(name_cy)) if n > 1 else 2.5 * hmed
    rthr = min(thr, 0.5 * pitch)

    def place(key, anchor_cy, target):
        rows = clusters(by[key], cy, rthr)
        rcy = [float(np.mean([cy[i] for i in c])) for c in rows]
        for j, mm in assign_monotone(rcy, anchor_cy):
            st["members"][mm][key] = rows[j]

    for key in TABLE1:
        place(key, name_cy, None)

    srows = clusters(by["status_hubungan_dalam_keluarga"], cy, rthr)
    scy = [float(np.mean([cy[i] for i in c])) for c in srows]
    for j, mm in assign_monotone(scy, name_cy):
        st["members"][mm]["status_hubungan_dalam_keluarga"] = srows[j]
    # jangkar tabel-2 = baris status anggota yang punya status; anggota tanpa status memakai nama_cy + offset
    s_off = statistics.median([scy[j] - name_cy[mm] for j, mm in assign_monotone(scy, name_cy)] or [0.0])
    anchor2 = [(float(np.mean([cy[i] for i in m["status_hubungan_dalam_keluarga"]]))
                if m["status_hubungan_dalam_keluarga"] else name_cy[k] + s_off)
               for k, m in enumerate(st["members"])]
    # baris orang tua: ayah+ibu satu baris -> kluster gabungan, lalu dibagi per key
    if joint_parents:
        prow = clusters(by["ayah"] + by["ibu"], cy, rthr)
        pcy = [float(np.mean([cy[i] for i in c])) for c in prow]
        for j, mm in assign_monotone(pcy, anchor2):
            for i in prow[j]:
                st["members"][mm][CLASSES[pred[i]]].append(i)
    else:
        for key in TABLE2:
            place(key, anchor2, None)

    complete_cells(st, P, aux, name_cy, anchor2, pitch, fill_tau)
    for m in st["members"]:
        for key in MEMBER_KEYS:
            m[key] = sorted(set(m[key]), key=lambda i: (round(cy[i] / max(hmed * 0.6, 1)), cx[i]))
    return st


# ----------------------------------------------------------------- nilai & JSON
def clean_value(key: str, text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip().lstrip(":").strip()
    if key in ("no_kk", "nik"):
        m = re.search(r"\d{16}", re.sub(r"[\s.\-]", "", t))
        return m.group(0) if m else re.sub(r"\D", "", t)
    return t


CLOSED = ("pendidikan", "pekerjaan", "status_hubungan_dalam_keluarga")


class Lexicon:
    """Kosakata tertutup dari GT train (nilai berfrekuensi >= min_count). snap(key, teks) -> (nilai, kemiripan).
    Ambang kemiripan per key disetel pada data train (lihat tune)."""

    def __init__(self, vocab: dict, theta: dict | None = None):
        self.vocab = vocab
        self.theta = theta or {k: 0.8 for k in CLOSED}

    @classmethod
    def from_gt(cls, gt_paths, min_count=2):
        import json
        from collections import Counter
        cnt = {k: Counter() for k in CLOSED}
        for p in gt_paths:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            for m in d["anggota_keluarga"]:
                for k in CLOSED:
                    if m[k]["value"]:
                        cnt[k][m[k]["value"]] += 1
        return cls({k: [v for v, c in cnt[k].items() if c >= min_count] for k in CLOSED})

    def best(self, key, text):
        from difflib import SequenceMatcher
        nt = re.sub(r"[^A-Z0-9]", "", text.upper())
        best = ("", 0.0)
        for v in self.vocab.get(key, []):
            r = SequenceMatcher(None, nt, re.sub(r"[^A-Z0-9]", "", v.upper())).ratio()
            if r > best[1]:
                best = (v, r)
        return best

    def snap(self, key, text):
        v, r = self.best(key, text)
        return (v, r) if v and r >= self.theta.get(key, 1.1) else (text, None)

    def tune(self, samples, grid=(0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01)):
        """samples: {key: [(teks_v6, nilai_gt)]}. Pilih ambang dengan akurasi (norm) tertinggi; seri -> ambang lebih tinggi."""
        n = lambda x: re.sub(r"[^A-Z0-9]", "", x.upper())  # noqa: E731
        for k, rows in samples.items():
            pre = [(t, g, *self.best(k, t)) for t, g in rows]
            best = None
            for th in grid:
                acc = np.mean([n(v if (v and r >= th) else t) == n(g) for t, g, v, r in pre])
                if best is None or acc >= best[0]:
                    best = (acc, th)
            self.theta[k] = best[1]
        return self.theta


def field(key, ids, texts, conf, lex=None):
    if not ids:
        return {"value": "", "conf": 0.0}
    txt = " ".join(texts[i]["text"] for i in ids)
    c = float(min(conf[i] for i in ids))
    if lex is not None and key in CLOSED:
        val, sim = lex.snap(key, txt)
        if sim is not None:
            return {"value": val, "conf": round(min(c, sim), 4)}
    return {"value": clean_value(key, txt), "conf": round(c, 4)}


def to_json(doc: dict, st: dict, lex: Lexicon | None = None) -> dict:
    conf, texts = st.get("_conf"), doc["texts"]
    out = {"no_kk": field("no_kk", st["no_kk"], texts, conf),
           "nama_kepala_keluarga": field("nama_kepala_keluarga", st["nama_kepala_keluarga"], texts, conf),
           "anggota_keluarga": []}
    for m in st["members"]:
        out["anggota_keluarga"].append({k: field(k, m[k], texts, conf, lex) for k in MEMBER_KEYS})
    return out

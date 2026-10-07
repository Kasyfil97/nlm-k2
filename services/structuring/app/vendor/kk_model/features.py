"""
Fitur per kotak OCR v6 untuk klasifikasi key (9 field + "O") -- salinan structuring/training/features.py
(+ context_feats dari structuring/training/pipeline.py), dipakai untuk menjalankan ulang model structuring
pasangan scorer. Harus identik dengan versi structuring agar probabilitas kotak sama; fitur per field ada di
field_features.py.

Hanya memakai keluaran OCR v6 (texts: text/score/poly + width/height), jadi sama persis dengan yang
tersedia di produksi.
"""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

import numpy as np

KEYS = ["no_kk", "nama_kepala_keluarga", "nama_lengkap", "nik", "pendidikan", "pekerjaan",
        "status_hubungan_dalam_keluarga", "ayah", "ibu"]
CLASSES = KEYS + ["O"]
C2I = {c: i for i, c in enumerate(CLASSES)}

ANCHORS = {"kartu": "KARTUKELUARGA", "kepala": "NAMAKEPALAKELUARGA", "nama": "NAMALENGKAP", "nik": "NIK",
           "pend": "PENDIDIKAN", "kerja": "JENISPEKERJAAN", "status": "STATUSHUBUNGAN", "ayah": "AYAH",
           "ibu": "IBU"}


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def skew(texts) -> float:
    angs = []
    for t in texts:
        (x0, y0), (x1, y1) = t["poly"][0], t["poly"][1]
        if math.hypot(x1 - x0, y1 - y0) > 40:
            angs.append(math.atan2(y1 - y0, x1 - x0))
    th = float(np.median(angs)) if angs else 0.0
    return th if abs(th) < math.radians(10) else 0.0


def geometry(texts, th):
    c, s = math.cos(th), math.sin(th)
    g = np.zeros((len(texts), 8))          # x0 x1 y0 y1 cx cy w h  (koordinat sudah dikoreksi miring)
    for i, t in enumerate(texts):
        xs = [x * c + y * s for x, y in t["poly"]]
        ys = [y * c - x * s for x, y in t["poly"]]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        g[i] = [x0, x1, y0, y1, (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0]
    return g


def find_anchors(texts, g):
    """Header kolom: kotak teratas yang cocok dengan pola (fuzzy untuk pola panjang, persis untuk pendek)."""
    out = {}
    nts = [norm(t["text"]) for t in texts]
    for name, pat in ANCHORS.items():
        best = None
        for i, nt in enumerate(nts):
            if not nt:
                continue
            if len(pat) <= 4:
                ok = nt == pat
            else:
                ok = abs(len(nt) - len(pat)) <= 8 and SequenceMatcher(None, nt, pat).ratio() >= 0.8
            if ok and (best is None or g[i, 5] < g[best, 5]):
                best = i
        out[name] = best
    return out


def text_feats(t: str, score: float):
    s = t.strip()
    n = max(len(s), 1)
    dig = sum(ch.isdigit() for ch in s)
    alp = sum(ch.isalpha() for ch in s)
    up = sum(ch.isupper() for ch in s)
    return [len(s), len(s.split()), dig, dig / n, alp / n, up / max(alp, 1), s.startswith(":"), "/" in s,
            "," in s, bool(re.fullmatch(r"\d{16}", re.sub(r"\s", "", s))), bool(re.fullmatch(r"[-–—_.\s]*", s)),
            s[:1].isdigit(), score]


TEXT_FEATS = ["len", "ntok", "ndig", "dig_frac", "alpha_frac", "upper_frac", "colon0", "slash", "comma",
              "is16", "is_dash", "starts_digit", "score"]


def rows_of(g):
    """Kelompokkan kotak ke baris menurut cy (toleransi 0.6 x tinggi median)."""
    hmed = float(np.median(g[:, 7])) if len(g) else 10.0
    order = np.argsort(g[:, 5])
    row = np.zeros(len(g), int)
    r, last = 0, None
    for i in order:
        if last is not None and g[i, 5] - last > 0.6 * hmed:
            r += 1
        row[i] = r
        last = g[i, 5]
    return row


def featurize(doc: dict):
    """doc: {'texts': [...], 'W': int, 'H': int}. -> (X ndarray, names list, aux dict)"""
    texts, W, H = doc["texts"], doc["W"], doc["H"]
    th = skew(texts)
    g = geometry(texts, th)
    hmed = float(np.median(g[:, 7])) if len(g) else 10.0
    anc = find_anchors(texts, g)
    row = rows_of(g)
    n = len(texts)
    names = (["x0", "x1", "y0", "y1", "cx", "cy", "w", "h", "h_rel", "aspect"] + TEXT_FEATS)
    feats = []
    for i, t in enumerate(texts):
        x0, x1, y0, y1, cx, cy, w, h = g[i]
        f = [x0 / W, x1 / W, y0 / H, y1 / H, cx / W, cy / H, w / W, h / H, h / hmed, w / max(h, 1)]
        f += [float(v) for v in text_feats(t["text"], t["score"])]
        # struktur baris
        same = np.where(row == row[i])[0]
        f += [len(same), int((g[same, 4] < cx).sum()), int((g[same, 4] > cx).sum())]
        feats.append(f)
    names += ["row_n", "row_left", "row_right"]
    X = np.array(feats, float) if n else np.zeros((0, len(names)))
    # fitur relatif terhadap header
    for a in ANCHORS:
        j = anc[a]
        cols = np.full((n, 5), np.nan)
        cols[:, 0] = 0 if j is None else 1
        if j is not None:
            cols[:, 1] = (g[:, 4] - g[j, 4]) / W
            cols[:, 2] = (g[:, 0] - g[j, 0]) / W
            cols[:, 3] = (g[:, 1] - g[j, 1]) / W
            cols[:, 4] = (g[:, 5] - g[j, 5]) / hmed
        X = np.hstack([X, cols])
        names += [f"{a}_found", f"{a}_dxc", f"{a}_dxl", f"{a}_dxr", f"{a}_dy"]
    aux = {"g": g, "row": row, "hmed": hmed, "W": W, "H": H,
           "alnum": np.array([bool(re.search(r"[A-Za-z0-9]", x["text"])) for x in texts], bool)}
    return X, names, aux


def neighbors(g, row):
    """Indeks tetangga tiap kotak: kiri/kanan (baris sama), atas/bawah (kolom sama), -1 bila tidak ada."""
    n = len(g)
    nb = np.full((n, 4), -1)
    for i in range(n):
        same = np.where((row == row[i]) & (np.arange(n) != i))[0]
        if len(same):
            left = same[g[same, 1] <= g[i, 0] + 2]
            right = same[g[same, 0] >= g[i, 1] - 2]
            if len(left):
                nb[i, 0] = left[np.argmax(g[left, 1])]
            if len(right):
                nb[i, 1] = right[np.argmin(g[right, 0])]
        ovx = np.minimum(g[:, 1], g[i, 1]) - np.maximum(g[:, 0], g[i, 0])
        ov = np.where((np.arange(n) != i) & (ovx > 0.5 * np.minimum(g[:, 6], g[i, 6])))[0]
        up = ov[g[ov, 5] < g[i, 5] - 0.3 * g[i, 7]]
        dn = ov[g[ov, 5] > g[i, 5] + 0.3 * g[i, 7]]
        if len(up):
            nb[i, 2] = up[np.argmax(g[up, 5])]
        if len(dn):
            nb[i, 3] = dn[np.argmin(g[dn, 5])]
    return nb


def context_feats(P, nb, row):
    """Probabilitas tetangga (kiri, kanan, atas, bawah) + rata-rata baris + flag ada/tidak."""
    n, nc = P.shape
    parts = []
    for k in range(4):
        idx = nb[:, k]
        has = idx >= 0
        Q = np.zeros((n, nc))
        Q[has] = P[idx[has]]
        parts += [Q, has[:, None].astype(float)]
    u, inv = np.unique(row, return_inverse=True)
    s = np.zeros((len(u), nc))
    np.add.at(s, inv, P)
    cnt = np.bincount(inv)[:, None]
    parts.append((s / cnt)[inv])
    return np.hstack(parts)

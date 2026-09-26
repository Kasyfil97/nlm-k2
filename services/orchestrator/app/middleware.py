"""Rate limit dan CORS: dua hal yang kontrak §12 daftarkan tetapi struktur yang disalin tidak punya.

Keduanya hanya ada di service ini. Orchestrator satu-satunya yang terekspos ke luar jaringan
compose/namespace; guardrails dan tahap-tahapnya hanya dipanggil dari dalam, dan membatasi laju di
sana justru akan menolak pekerjaan yang sudah terlanjur diterima 202.
"""

import hashlib
import logging
import time
from collections import deque

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ocr_common.web.envelope import envelope
from ocr_common.web.request_id import REQUEST_ID_HEADER

from app.config import Settings

logger = logging.getLogger(__name__)

RATE_LIMITED = Counter(
    "http_rate_limited_total",
    "Requests refused with 429 by the rate limit, by route path",
    ["path"],
)

# Probe, metrics dan halaman dokumentasi: tidak dibatasi. Probe karena kubelet memanggilnya jauh lebih
# sering daripada klien mana pun dan membuatnya 429 berarti pod dikeluarkan dari Service justru saat
# sedang sibuk; /metrics karena scraper Prometheus punya alasan yang sama.
EXEMPT_PATHS = frozenset({"/health", "/ready", "/metrics", "/docs", "/redoc", "/openapi.json"})

TOO_MANY_REQUESTS = "Terlalu banyak permintaan, silakan coba lagi beberapa saat lagi"


def client_key(request) -> str:
    """Siapa yang dihitung: API key lebih dulu, alamat hanya kalau kunci tidak ada.

    Kuncinya di-hash. Nilainya tidak pernah masuk log atau label metrik dari sini, tetapi kunci mentah
    sebagai kunci dict adalah rahasia yang hidup di heap lebih lama daripada requestnya, dan muncul utuh
    di setiap dump memori atau traceback yang memuat state middleware ini.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return "k:" + hashlib.sha256(api_key.encode()).hexdigest()[:32]
    client = request.client
    return "a:" + (client.host if client else "unknown")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Jendela geser per (pemanggil, proses), `RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`.

    **Per proses, bukan per deployment.** Dengan N replika batas efektifnya N x nilai yang disetel, dan
    dua request dari klien yang sama bisa mendarat di replika berbeda. Ini disengaja: penghitung bersama
    menuntut Redis, dan satu-satunya yang dilindungi di sini adalah proses ini sendiri -- unggahan 5 MB
    yang dibaca ke memori lalu diteruskan ke model guardrails. Kuota lintas tenant adalah pekerjaan
    gateway di depan, bukan pekerjaan ini.

    Ditambahkan SETELAH `create_app`, jadi ia berjalan di luar `RequestIdMiddleware` dan
    `MetricsMiddleware`. Itu memang tempat yang benar -- request ditolak sebelum badannya dibaca --
    tetapi berarti dua hal: `request_id` pada amplop 429 diambil dari header `X-Request-ID` sendiri
    (dan null kalau pemanggil tidak mengirimnya), dan 429 TIDAK terhitung di `http_requests_total`.
    Karena itu `http_rate_limited_total` ada.
    """

    def __init__(self, app, *, requests: int, window_seconds: float) -> None:
        super().__init__(app)
        self._requests = requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._swept = 0.0

    async def dispatch(self, request, call_next):
        if request.url.path in EXEMPT_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        now = time.monotonic()
        self._sweep(now)
        hits = self._hits.setdefault(client_key(request), deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._requests:
            return self._refuse(request, retry_after=hits[0] + self._window - now)
        hits.append(now)
        return await call_next(request)

    def _refuse(self, request, *, retry_after: float) -> JSONResponse:
        path = getattr(request.scope.get("route"), "path", None) or request.url.path
        RATE_LIMITED.labels(path).inc()
        request_id = request.headers.get(REQUEST_ID_HEADER)
        logger.warning("429 %s %s (rate limit %d/%.0fs)", request.method, path, self._requests, self._window)
        response = JSONResponse(
            status_code=429,
            content=envelope(429, TOO_MANY_REQUESTS, None, request_id, errors=TOO_MANY_REQUESTS),
        )
        response.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        return response

    def _sweep(self, now: float) -> None:
        """Buang pemanggil yang sudah diam lebih lama dari satu jendela.

        Tanpa ini dict-nya tumbuh satu entri per API key atau alamat yang pernah terlihat dan tidak
        pernah menyusut -- kebocoran yang lambat tetapi tidak terbatas pada proses yang hidup berhari-hari.
        Disapu paling sering sekali per jendela, bukan per request.
        """
        if now - self._swept < self._window:
            return
        self._swept = now
        cutoff = now - self._window
        for key in [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]


def add_edge_middleware(app: FastAPI, settings: Settings) -> None:
    """Pasang rate limit lalu CORS. Urutannya penting: yang ditambahkan terakhir berjalan paling luar,
    jadi CORS membungkus rate limit -- preflight tidak terhitung kuota, dan 429 tetap membawa header CORS
    sehingga browser membaca statusnya alih-alih melaporkan galat CORS yang menyesatkan."""
    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    else:
        logger.warning("RATE_LIMIT_ENABLED=false: service ini tidak membatasi laju permintaan")

    origins = settings.cors_origins
    if not origins:
        return
    # allow_credentials sengaja tidak dinyalakan: autentikasi di sini adalah header X-API-Key, bukan
    # cookie, jadi tidak ada kredensial browser yang perlu diizinkan -- dan menyalakannya bersama `*`
    # adalah kombinasi yang dilarang spesifikasi CORS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["X-API-Key", "X-Request-ID", "Content-Type"],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
        max_age=600,
    )

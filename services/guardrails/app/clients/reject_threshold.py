"""The threshold that turns `probability_bad` into a verdict -- R15.

Five rungs, highest first:

1. the `threshold` form field of this request (§5.1), applied in `GuardrailsService.check`;
2. the central orchestrator's endpoint, `GUARDRAILS_THRESHOLD_URL` + `GUARDRAILS_THRESHOLD_PATH`,
   cached for `GUARDRAILS_THRESHOLD_CACHE_SECONDS` -- this is the rung that lets the threshold be
   moved without a deploy here, which is why the verdict echoes the one it actually used;
3. `GUARDRAILS_THRESHOLD` (§13.3);
4. the value stored with the weights -- see the note on `default_threshold`;
5. 0.5, the contract's default.

K2Quality's own database-backed threshold and its `PUT /config` are gone with R15: a value a
running service can rewrite in its own database is a second source of truth for the one number that
decides whether a document enters the pipeline at all.
"""

import asyncio
import logging
import math
import time
from collections.abc import Callable
from typing import Any

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.errors import ServiceError

logger = logging.getLogger(__name__)

#: §13.3. The last rung, and the only one that is a constant rather than a configured value.
DEFAULT_THRESHOLD = 0.5


def default_threshold(configured: float | None, model: Any) -> float:
    """Rungs 3 to 5: `GUARDRAILS_THRESHOLD`, else the model's own, else 0.5.

    The model's own is `reject_threshold`, and it is `None` on every backend that has no stored
    threshold -- which today is all of them: K2Quality keeps its operating point in `config.yaml`,
    not in any of the six artifacts, so `kk_quality` reads an optional `decision_threshold` key from
    `blur_cnn_meta.json` that the current export does not contain. `None` rather than `0.5` is what
    keeps rung 4 honest: a rung that defaults to the value of the rung below it cannot be
    distinguished from a chain that is quietly broken.
    """
    if configured is not None:
        return configured
    stored = getattr(model, "reject_threshold", None)
    return DEFAULT_THRESHOLD if stored is None else float(stored)


def parse_threshold(body: Any) -> float:
    """`{"reject_threshold": 0.5}` into 0.5. Raises ValueError for anything else, including a value
    outside (0, 1): 0 would reject every document, 1 almost none."""
    value = body.get("reject_threshold") if isinstance(body, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"no numeric reject_threshold in {body!r}")
    if not 0 < value < 1:
        raise ValueError(f"reject_threshold must be between 0 and 1, got {value}")
    return float(value)


class RejectThreshold:
    """Rungs 2 to 5. With a client (`GUARDRAILS_THRESHOLD_URL` set), `GET` on the orchestrator's
    endpoint, kept for `cache_seconds` so a document does not wait on an extra call. When that call
    fails or answers something that is not a threshold, the last value the orchestrator gave stays
    in force (the default if it never gave one), and the endpoint is tried again after
    `cache_seconds`."""

    def __init__(
        self,
        client: RemoteModelClient | None,
        path: str,
        default: float,
        *,
        cache_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._path = path
        self.default = default
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._value: float | None = None
        self._checked_at: float | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> float:
        if self._client is None:
            return self.default
        if not self._due():
            return self._current()
        async with self._lock:
            if self._due():  # another request may have fetched it while this one waited for the lock
                await self._fetch(self._client)
        return self._current()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _due(self) -> bool:
        return self._checked_at is None or self._clock() - self._checked_at >= self._cache_seconds

    def _current(self) -> float:
        return self.default if self._value is None else self._value

    async def _fetch(self, client: RemoteModelClient) -> None:
        try:
            value = parse_threshold(await client.get_json(self._path))
        except (ServiceError, ValueError) as exc:
            message = exc.message if isinstance(exc, ServiceError) else str(exc)
            logger.warning(
                "reject threshold from the orchestrator unavailable (%s); using %s", message, self._current()
            )
        else:
            if value != self._value:
                logger.info("reject threshold from the orchestrator: %s", value)
            self._value = value
        self._checked_at = self._clock()

"""The verdict on one Kartu Keluarga image (§5.2).

A Kartu Keluarga is always a single image, so everything the previous pipeline had here for documents
with pages is gone: no `render_pages`, no page list, no `_aggregate`, no `GUARDRAILS_DOCUMENT_POLICY`.
`_reject_reason()` was rewritten rather than renamed -- it read `n_reject` / `n_pages`, and both
columns were deleted from `GuardrailsDocument`, so there was nothing left of it to keep.

This service judges and never refuses: §5.2 is always 200, with the verdict in `data.passed`.
"""

import logging
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.clients.reject_threshold import REJECT, RejectThreshold, Threshold, default_threshold
from app.config import Settings
from app.ml.base import UnassessableImage

logger = logging.getLogger(__name__)

VERDICT_ACCEPTED = "accepted"
VERDICT_REJECT = "reject"
VERDICT_UNASSESSABLE = "unassessable"

#: §3.5 lists this string as the guardrails row of the rejection-message table, and the orchestrator
#: puts it in `message` of its 400 unchanged, so it is shown to an end user as it stands.
REASON_REJECT = "Kualitas gambar terlalu rendah, mohon unggah foto yang lebih jelas"

#: R14a. Deliberately NOT the sentence above: "we judged this bad" and "we could not judge it" need
#: different things from the person holding the phone, and the quality wording is the wrong
#: diagnosis for a file that never decoded. Not in §3.5 yet -- it belongs in that table.
REASON_UNASSESSABLE = "Gambar tidak dapat dinilai, mohon unggah ulang foto Kartu Keluarga yang utuh"


def reason_for(verdict: str) -> str | None:
    """The Indonesian `reason` for a verdict; None when the document passed."""
    if verdict == VERDICT_ACCEPTED:
        return None
    return REASON_UNASSESSABLE if verdict == VERDICT_UNASSESSABLE else REASON_REJECT


class GuardrailsService:
    """The model's verdict on a document, and nothing else: type, size and the choice between
    `file` and `file_url` are settled before this is called."""

    def __init__(self, model: Any, settings: Settings, threshold: RejectThreshold | None = None):
        """Without `threshold`, a chain with no remote source: `GUARDRAILS_THRESHOLD`, else the
        model's own, else 0.5."""
        self._model = model
        self._settings = settings
        self._threshold = threshold or RejectThreshold(
            None, "", default_threshold(settings.guardrails_threshold, model), cache_seconds=0
        )

    async def check(
        self, filename: str, content_type: str | None, content: bytes, *, override: Threshold | None = None
    ) -> dict[str, Any]:
        """The §5.2 `data` block: `{passed, reason, document}`. `override` is the request's own threshold and
        side; without it the configured chain decides, on the reject side."""
        if hasattr(self._model, "check_document"):
            # The remote service judges under its own threshold, so neither the per-request
            # override nor the chain applies; forcing one here would report a threshold that did
            # not decide anything.
            document = await self._model.check_document(filename, content, content_type)
        else:
            threshold = override if override is not None else Threshold(await self._threshold.get(), REJECT)
            document = await run_in_threadpool(self._assess, filename, content_type, content, threshold)

        verdict = document["verdict"]
        return {"passed": verdict == VERDICT_ACCEPTED, "reason": reason_for(verdict), "document": document}

    def _assess(self, filename: str, content_type: str | None, content: bytes, threshold: Threshold) -> dict[str, Any]:
        """Runs the in-process model and turns its one probability into the document block."""
        try:
            probability_bad = round(float(self._model.assess(filename, content_type, content)), 4)
        except UnassessableImage as exc:
            # `exc` describes the file, not its contents, so it is safe to log -- but the message
            # is for an operator, never for the caller, who gets REASON_UNASSESSABLE instead.
            logger.info("guardrails could not assess the image: %s", exc)
            return {
                "verdict": VERDICT_UNASSESSABLE,
                "confidence": None,
                "probability_bad": None,
                # The threshold that was in force, even though nothing was compared against it:
                # the field records the configuration the request ran under.
                "threshold_used": threshold.value,
                "threshold_target": threshold.target,
            }

        rejected = threshold.rejects(probability_bad)
        return {
            "verdict": VERDICT_REJECT if rejected else VERDICT_ACCEPTED,
            # Confidence in the verdict, not in "bad": the rejection is as confident as the
            # probability, the acceptance is as confident as its complement.
            "confidence": probability_bad if rejected else round(1.0 - probability_bad, 4),
            "probability_bad": probability_bad,
            "threshold_used": threshold.value,
            "threshold_target": threshold.target,
        }

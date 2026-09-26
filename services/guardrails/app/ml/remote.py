"""The `remote` backend: the ML team's quality service over HTTP.

Kept rather than dropped because it is the deployment shape the ML team already runs (K2Quality is
a service before it is a library), and because it is the only way to put the model on a GPU host
while this service stays small. It is a `DocumentChecker`, not a `QualityModel`: the document goes
over whole and the verdict comes back already made, under the remote service's own threshold.
"""

from typing import Any

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.errors import InternalError

VERDICTS = ("accepted", "reject", "unassessable")


class RemoteGuardrailsModel:
    name = "remote"
    PREDICT_PATH = "/v1/predict/json"

    def __init__(self, client: RemoteModelClient):
        self._client = client

    async def check_document(self, filename: str, content: bytes, content_type: str | None) -> dict[str, Any]:
        body = await self._client.post_multipart(
            self.PREDICT_PATH,
            filename=filename or "upload",
            content=content,
            content_type=content_type or "image/jpeg",
        )
        return parse_document(body, self._client.name)

    async def aclose(self) -> None:
        await self._client.aclose()


def _probability(value: Any, *, allow_null: bool) -> float | None:
    if value is None and allow_null:
        return None
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"probability out of range: {number}")
    return round(number, 4)


def parse_document(body: Any, name: str) -> dict[str, Any]:
    """The remote `{data: {document}}` into the §5.2 `document` block.

    Only the three fields the contract names are taken through; anything else the service adds is
    dropped here rather than forwarded, because this block travels unchanged to scoring and an
    unknown key there would outlive everyone who knew what it meant.
    """
    try:
        document = body["data"]["document"]
        verdict = document["verdict"]
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict: {verdict!r}")
        unassessable = verdict == "unassessable"
        probability_bad = _probability(document.get("probability_bad"), allow_null=unassessable)
        if probability_bad is None and not unassessable:
            raise ValueError(f"verdict {verdict!r} without a probability_bad")
        confidence = probability_bad if verdict == "reject" else None
        if verdict == "accepted" and probability_bad is not None:
            confidence = round(1.0 - probability_bad, 4)
        return {
            "verdict": verdict,
            "confidence": confidence,
            "probability_bad": probability_bad,
            # The remote service applies its own threshold. It states which one when it can; when it
            # does not, the field is null rather than this service's threshold, which was not used.
            "threshold_used": _optional_threshold(document.get("threshold_used")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise InternalError(f"{name} returned an unexpected response") from exc


def _optional_threshold(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not 0 < number < 1:
        raise ValueError(f"threshold_used out of range: {number}")
    return number

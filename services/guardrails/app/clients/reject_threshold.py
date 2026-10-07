"""The threshold that turns `probability_bad` into a verdict.

Only the request decides: the `threshold` and `threshold_target` form fields (§5.1), applied in
`GuardrailsService.check`. A request without them is not judged against any threshold -- the document passes,
and `probability_bad` is answered for the caller to read. The earlier chain (the central orchestrator's
endpoint, `GUARDRAILS_THRESHOLD`, the value stored with the weights, 0.5) is gone: a threshold the caller did
not send no longer refuses its document.
"""

from dataclasses import dataclass

ACCEPT = "accept"
REJECT = "reject"


@dataclass(frozen=True)
class Threshold:
    """A threshold and the side it applies to, as nilam's guardrails takes it per request.

    `reject`: rejected when `probability_bad >= value`. `accept`: rejected when the probability the image
    is good, `1 - probability_bad`, is below `value`. The KK model gives one probability, so the accept side
    is its complement."""

    value: float
    target: str = REJECT

    def rejects(self, probability_bad: float) -> bool:
        if self.target == ACCEPT:
            return round(1.0 - probability_bad, 4) < self.value
        return probability_bad >= self.value

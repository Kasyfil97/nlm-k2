"""The `mock` backend for local development, tests and the end-to-end smoke run: the verdict comes
from the file name and nothing is decoded.

The trigger words are a contract of their own -- the orchestrator's own tests, the compose smoke run
and R25 all send a file named after the outcome they expect -- so they are listed here once and the
numbers they produce are fixed. `notkk` replaces the trigger the previous pipeline used for "not the
expected document type".

The trigger words give a bad probability; like any bad image they are rejected only when the request
sends a threshold (without one every document passes). `reject_threshold` is None: the mock has no checkpoint.
"""

from typing import Any

#: A file name containing any of these gets the bad probability. Lower-cased before matching.
REJECT_TRIGGERS = ("blur", "invalid", "notkk")

#: The two probabilities the mock ever returns. They are the contract's own example numbers, so a
#: fixture built from §5.2 and a response from the mock agree digit for digit.
PROBABILITY_BAD_REJECT = 0.8821
PROBABILITY_BAD_ACCEPT = 0.0179


class MockQualityModel:
    name = "mock"
    reject_threshold: float | None = None
    metadata: dict[str, Any] = {"architecture": "mock", "triggers": list(REJECT_TRIGGERS)}

    def assess(self, filename: str, content_type: str | None, content: bytes) -> float:
        name = (filename or "").lower()
        rejected = any(trigger in name for trigger in REJECT_TRIGGERS)
        return PROBABILITY_BAD_REJECT if rejected else PROBABILITY_BAD_ACCEPT

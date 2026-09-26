"""What a guardrails backend has to look like, and the one failure it is allowed to signal.

Two protocols, because there are two kinds of backend and they differ in more than transport.

`QualityModel` runs in this process and is given the **bytes**, not a decoded image. That is a
deliberate change from the shape this file had in the pipeline this repository was copied from, where
the protocol took a list of already-rendered `PIL.Image` pages and answered `(proba_approve,
proba_reject)` per page. Three things forced it:

* A Kartu Keluarga is one image, so a list of pages and a document-level aggregation over that list
  had nothing left to aggregate -- §5.2 has no page block at all, and `GuardrailsDocument` lost
  `n_pages` / `n_approve` / `n_reject` with it.
* The KK stack answers with **one** number, `probability_bad`. `proba_approve` is `1 - probability_bad`
  by definition, so carrying both in the interface only creates a pair a backend can make
  inconsistent.
* The decisive one: the KK model core decodes the bytes itself. It reads the header before decoding
  to bound the allocation, it runs the classical CV features on the full-resolution array, and its
  minimum/maximum dimension checks are part of the judgement (they are what produces `unassessable`).
  Handing it a `PIL.Image` that some other layer decoded would mean decoding twice and would put the
  failure that R14a is about -- an image that cannot be read -- in a layer that can only answer with
  an HTTP error.

`DocumentChecker` is unchanged in spirit: the model is served elsewhere, the whole document goes over
HTTP and the verdict comes back already made. `GuardrailsService` still picks between them with
`hasattr(model, "check_document")` rather than `isinstance`, so a backend does not have to import
this module to satisfy it.
"""

from typing import Any, Protocol


class UnassessableImage(Exception):
    """The model could not judge this image at all -- R14a.

    Not an error path in the HTTP sense: §5.2 requires guardrails to answer 200 with the verdict in
    `data.passed`, so this becomes `verdict: "unassessable"` with `probability_bad: null` and
    `passed: false`. It is deliberately distinct from `reject`: "we judged this bad" and "we could
    not judge it" need different alerts and different fixes.

    The K2Quality core this service ports raised `ImageValidationError` / `ImageLoadError` in more
    than twenty places for exactly this -- undecodable bytes, a format that is not JPEG/PNG/PDF,
    dimensions outside the range the checkpoint was trained on -- and the inherited answer to
    all of them was a 400, which §5.2 forbids.

    `message` is the operator-facing detail and is NOT what the caller sees: the caller gets the
    Indonesian `reason` from `GuardrailsService`. Keep the image's own data out of it.
    """


class QualityModel(Protocol):
    """A model that runs in this process: bytes in, one probability out."""

    name: str
    #: The threshold stored alongside the weights, or None when the artifacts carry none. This is
    #: the fourth rung of the R15 chain, below `GUARDRAILS_THRESHOLD` and above the 0.5 floor, and
    #: None means the rung is simply absent rather than "0.5".
    reject_threshold: float | None
    metadata: dict[str, Any]

    def assess(self, filename: str, content_type: str | None, content: bytes) -> float:
        """`probability_bad` in [0, 1]. Raises `UnassessableImage` when the image cannot be judged."""
        ...


class DocumentChecker(Protocol):
    """A model served elsewhere (the ML team's quality service): the document is sent whole and the
    verdict comes back already made, under its own threshold."""

    name: str

    async def check_document(self, filename: str, content: bytes, content_type: str | None) -> dict[str, Any]:
        """The `document` block of §5.2: `{verdict, probability_bad, threshold_used}`."""
        ...

    async def aclose(self) -> None: ...


QualityBackend = QualityModel | DocumentChecker

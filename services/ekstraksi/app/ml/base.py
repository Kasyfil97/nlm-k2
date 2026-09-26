"""What an OCR backend of this stage looks like.

There are **two** Protocols, not one, and the split is load-bearing rather than tidiness.

`OcrRecognizer` is a model that runs **in this process**, and it is deliberately *synchronous*.
`StagePipeline._run_bound` does `result = dict(await work())` on the event loop, and the Unit 3
heartbeat that keeps a long job's lease warm is an ordinary loop task: it only beats while `work()`
releases the loop. A KK detector plus recogniser is seconds of CPU, which is exactly the case the
heartbeat exists for -- so if inference were awaited directly it would block the beat, another
replica's reaper would reclaim the job, and the corruption class Unit 3 removed would come back in
production with the real backend after passing every test that only used the mock. Declaring the
in-process interface synchronous makes that impossible to get wrong by accident: there is no `await`
to reach for, and `EkstraksiService` is the one place that decides how it leaves the loop
(`run_in_threadpool`).

`RemoteOcrModel` is a model served elsewhere (the ML team's OCR service). It is inherently async and
already off the loop, so it keeps its own `extract`. `EkstraksiService` tells the two apart by
duck-typing on `extract`, the same way guardrails tells `DocumentChecker` from `PageClassifier`.
"""

from typing import Protocol, runtime_checkable

from ocr_common.types import OcrEngineResult


@runtime_checkable
class OcrRecognizer(Protocol):
    """A detector + recogniser running in this process. **Synchronous on purpose** -- see the module docstring.

    Returns `{texts, model}` with `texts` in the frozen §7.1 shape (`{text, score, poly}`, `poly` four
    points of two floats). An empty `texts` is a valid answer, not a failure: an image with no readable
    text is rejected by the structuring rules (§7.4), never here (§6.3).

    Raises `InternalError` when the model itself broke or its runtime is not installed.
    """

    name: str

    def read(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult: ...


@runtime_checkable
class RemoteOcrModel(Protocol):
    """A model served by another process: already off this event loop, so it stays async."""

    name: str

    async def extract(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult: ...

    async def aclose(self) -> None: ...


OcrEngine = OcrRecognizer | RemoteOcrModel

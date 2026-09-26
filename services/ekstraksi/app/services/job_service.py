import asyncio
from collections.abc import Mapping
from typing import Any

from ocr_common.clients.fetch_url import STRICT_URL_POLICY, FetchUrlError, UrlPolicy, fetch
from ocr_common.errors import BadRequest
from ocr_common.kk import DOCUMENT_TYPE
from ocr_common.pipeline import STAGE_STRUCTURING, HandoffPayload, StagePipeline, Work
from ocr_common.simulation import simulated_delay_seconds
from ocr_common.types import OcrResult

from app.services.ekstraksi_service import EkstraksiService

UploadedFile = tuple[bytes, str, str | None]
Source = UploadedFile | str
Handoff = HandoffPayload

INLINE_UPLOAD_GONE = (
    "cannot run this job again: the document was uploaded inline and was lost with the process that died; "
    "send the request again (a document sent as file_url can be fetched again)"
)


class EkstraksiJobService:
    def __init__(
        self,
        pipeline: StagePipeline,
        ekstraksi: EkstraksiService,
        max_upload_bytes: int,
        url_policy: UrlPolicy = STRICT_URL_POLICY,
        *,
        simulate_delay: bool = False,
        handoff_by_reference: bool = False,
    ):
        self._pipeline = pipeline
        self._ekstraksi = ekstraksi
        self._max_upload_bytes = max_upload_bytes
        self._url_policy = url_policy
        self._simulate_delay = simulate_delay
        self._handoff_by_reference = handoff_by_reference

    async def submit(
        self, request_id: str, document_type: str, guardrails: dict[str, Any] | None, source: Source
    ) -> dict[str, Any]:
        work, handoff = self._spec(request_id, document_type, guardrails, source)
        return await self._pipeline.submit(
            request_id,
            work,
            handoff_payload=handoff,
            next_stage=STAGE_STRUCTURING,
            input={
                "document_type": document_type,
                "guardrails": guardrails,
                "file_url": source if isinstance(source, str) else None,
            },
        )

    async def resume(self, request_id: str, input: dict[str, Any] | None) -> None:
        """Run again a job a dead process left `PROCESSING`. Only a document sent as `file_url` can be
        fetched again; an inline upload is gone, so that job is failed with a message asking to resend."""
        input = input or {}
        file_url = input.get("file_url")
        if not file_url:

            async def gone() -> dict[str, Any]:
                raise BadRequest(INLINE_UPLOAD_GONE)

            await self._pipeline.resume(request_id, gone)
            return
        work, handoff = self._spec(
            request_id, input.get("document_type") or DOCUMENT_TYPE, input.get("guardrails"), file_url
        )
        await self._pipeline.resume(request_id, work, handoff_payload=handoff, next_stage=STAGE_STRUCTURING)

    async def get(self, request_id: str) -> dict[str, Any]:
        return await self._pipeline.get(request_id)

    def _spec(
        self, request_id: str, document_type: str, guardrails: dict[str, Any] | None, source: Source
    ) -> tuple[Work, Handoff]:
        async def work() -> OcrResult:
            content, filename, content_type = await self._load(source)
            # `delay20s-kk.jpg` makes this job outlive PIPELINE_WAIT_SECONDS so the orchestrator's
            # 202 path can be walked without a slow model. It lives here rather than in the mock
            # backend because it is a property of the job, not of the model, and `simulate_delay`
            # is `settings.simulation_hooks_enabled` -- the file name is caller-controlled input,
            # so the hook is gated at the config layer and exists only with ENVIRONMENT=local.
            delay = simulated_delay_seconds(filename, enabled=self._simulate_delay)
            if delay:
                await asyncio.sleep(delay)
            # `extract` puts an in-process model in the threadpool: this coroutine runs on the
            # event loop, and the Unit 3 heartbeat that keeps this job's lease warm only beats
            # while the loop is free.
            return await self._ekstraksi.extract(filename, content_type, content)

        def handoff(ocr: Mapping[str, Any]) -> dict[str, Any]:
            body: dict[str, Any] = {"request_id": request_id, "document_type": document_type, "guardrails": guardrails}
            if not self._handoff_by_reference:
                body["ocr"] = dict(ocr)
            return body

        return work, handoff

    async def _load(self, source: Source) -> UploadedFile:
        if not isinstance(source, str):
            return source
        try:
            return await fetch(source, limit=self._max_upload_bytes, policy=self._url_policy)
        except FetchUrlError as exc:
            raise BadRequest(str(exc)) from exc

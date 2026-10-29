"""Overload protection (ocr_common.overload) and where it is wired in: the bulkhead, the `Retry-After` of a
passed-through remote error, and the Retry-After header of the error envelope. Ported from nilam-ocr-npwp."""

import asyncio

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from ocr_common.clients.remote import RemoteClientError, RemoteModelClient
from ocr_common.config import BaseServiceSettings
from ocr_common.errors import SERVICE_BUSY, ServiceBusy, UnprocessableEntity
from ocr_common.overload import Bulkhead, retry_after_header
from ocr_common.web.app import create_app


def test_retry_after_header_rounds_up_to_whole_seconds():
    assert retry_after_header(None) is None
    assert retry_after_header(0.2) == "1"
    assert retry_after_header(4.1) == "5"


# --- bulkhead --------------------------------------------------------------------------------------------------


async def test_bulkhead_runs_limit_queues_the_rest_and_refuses_beyond():
    bulkhead = Bulkhead("t", limit=2, queue=1, retry_after=3.0)
    gate = asyncio.Event()
    running = 0
    peak = 0

    async def job(ticket):
        nonlocal running, peak
        async with ticket:
            running += 1
            peak = max(peak, running)
            await gate.wait()
            running -= 1

    tasks = [asyncio.create_task(job(bulkhead.reserve())) for _ in range(3)]
    await asyncio.sleep(0)
    assert (bulkhead.running, bulkhead.reserved) == (2, 3)
    with pytest.raises(ServiceBusy) as caught:
        bulkhead.reserve()
    assert caught.value.status_code == 503
    assert caught.value.code == SERVICE_BUSY
    assert caught.value.retry_after == 3.0

    gate.set()
    await asyncio.gather(*tasks)
    assert peak == 2
    assert (bulkhead.running, bulkhead.reserved) == (0, 0)


async def test_bulkhead_without_limit_never_refuses():
    bulkhead = Bulkhead("t", limit=0)
    tickets = [bulkhead.reserve() for _ in range(1000)]
    for ticket in tickets:
        async with ticket:
            pass
    assert bulkhead.reserved == 0


async def test_released_ticket_frees_its_place_once():
    bulkhead = Bulkhead("t", limit=1, queue=0)
    ticket = bulkhead.reserve()
    ticket.release()
    ticket.release()
    assert bulkhead.reserved == 0
    bulkhead.reserve().release()


async def test_bulkhead_error_can_be_replaced():
    bulkhead = Bulkhead(
        "t", limit=1, queue=0, retry_after=2.0, error=lambda m, after: UnprocessableEntity(m, retry_after=after)
    )
    bulkhead.reserve()
    with pytest.raises(UnprocessableEntity) as caught:
        bulkhead.reserve()
    assert (caught.value.status_code, caught.value.retry_after) == (422, 2.0)


async def test_cancelled_while_waiting_gives_the_place_back():
    bulkhead = Bulkhead("t", limit=1, queue=5)
    holder = bulkhead.reserve()
    await holder.__aenter__()
    waiter = bulkhead.reserve()

    async def wait():
        async with waiter:
            pass

    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bulkhead.reserved == 1
    await holder.__aexit__(None, None, None)
    assert bulkhead.reserved == 0


async def test_an_error_inside_gives_the_slot_back():
    bulkhead = Bulkhead("t", limit=1, queue=0)
    with pytest.raises(RuntimeError):
        async with bulkhead.slot():
            raise RuntimeError("model failed")
    assert (bulkhead.running, bulkhead.reserved) == (0, 0)
    async with bulkhead.slot():
        pass


# --- remote client and error envelope ----------------------------------------------------------------------------


async def test_remote_client_keeps_retry_after_of_the_remote():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"errors": "SERVICE_BUSY", "message": "busy"}, headers={"Retry-After": "7"})

    client = RemoteModelClient(
        "http://stage.test", 5, name="stage", transport=httpx.MockTransport(handler), passthrough_statuses=(503,)
    )
    with pytest.raises(RemoteClientError) as caught:
        await client.get_json("/x")
    assert (caught.value.status_code, caught.value.retry_after, caught.value.remote_code) == (503, 7.0, "SERVICE_BUSY")


def test_error_envelope_carries_retry_after():
    router = APIRouter()

    @router.get("/v1/busy")
    async def busy():
        raise ServiceBusy("too much", retry_after=4.2)

    app = create_app(
        settings=BaseServiceSettings(api_key="k", environment="local", _env_file=None),
        title="Demo",
        description="demo",
        routers=[router],
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/v1/busy", headers={"X-API-Key": "k"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["errors"] == SERVICE_BUSY

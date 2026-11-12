"""LEAD-1904 addendum 3: exact recorded notification reply, no external calls."""

import asyncio
import json

import httpx2
import pytest

from sextant.tradingview import account


async def send(response, *, rpc="notifications/initialized", request_id=None):
    gate = account.HTTPGate()
    message = {"jsonrpc": "2.0", "method": rpc}
    if request_id is not None:
        message["id"] = request_id
    async with httpx2.AsyncClient(
        transport=account.GuardedHTTPTransport(
            httpx2.MockTransport(lambda request: response), gate
        ),
        event_hooks={"request": [gate.request], "response": [gate.response]},
    ) as client:
        result = await client.send(
            client.build_request("POST", "https://offline.invalid/mcp", json=message), stream=True
        )
        await result.aclose()
    assert gate.pending is None


@pytest.mark.parametrize("request_id", [None, 1])
def test_recorded_202_json_empty_notification_and_request_are_accepted(capsys, request_id):
    asyncio.run(
        send(
            httpx2.Response(202, headers={"content-type": "application/json"}, content=b""),
            request_id=request_id,
        )
    )
    state = account.Account().snapshot()
    assert state["calls"] == 1 and state["stopped"] is False and state["quota_stop"] is False
    assert state["audit"] == [] and capsys.readouterr().err == ""


def test_202_never_reads_its_body(capsys):
    class Unreadable(httpx2.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("SDK does not read a 202 body")
            yield b""

    asyncio.run(
        send(
            httpx2.Response(202, headers={"content-type": "application/json"}, stream=Unreadable())
        )
    )
    state = account.Account().snapshot()
    assert state["calls"] == 1 and state["quota_stop"] is False
    assert state["audit"] == [] and capsys.readouterr().err == ""


@pytest.mark.parametrize("status", [200, 201, 204, 206])
@pytest.mark.parametrize("request_id", [None, 1])
def test_empty_other_2xx_json_body_is_not_parsed(capsys, status, request_id):
    asyncio.run(
        send(
            httpx2.Response(status, headers={"content-type": "application/json"}, content=b""),
            request_id=request_id,
        )
    )
    state = account.Account().snapshot()
    assert state["calls"] == 1 and state["stopped"] is False and state["quota_stop"] is False
    assert state["audit"] == [] and capsys.readouterr().err == ""


@pytest.mark.parametrize("request_id", [None, 1])
def test_nonempty_invalid_2xx_json_still_latches(request_id):
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(
            send(
                httpx2.Response(
                    200, headers={"content-type": "application/json"}, content=b"invalid json"
                ),
                request_id=request_id,
            )
        )
    state = account.Account().snapshot()
    assert state["calls"] == 1 and state["stopped"] is True and state["quota_stop"] is True
    assert state["audit"][0]["rule"] == "JSONDecodeError"


@pytest.mark.parametrize("status,body", [(200, {"text": "quota reached"}), (429, {})])
def test_quota_and_429_remain_stops(status, body):
    with pytest.raises(account.AccountError):
        asyncio.run(send(httpx2.Response(status, json=body)))
    state = account.Account().snapshot()
    assert state["calls"] == 1 and state["stopped"] is True and state["quota_stop"] is True

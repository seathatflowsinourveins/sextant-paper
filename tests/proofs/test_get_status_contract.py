"""R1 status contract from the 5664756f review; local MockTransport only.

Catches broadening the four named GET status exceptions or admitting a POST
after any other HTTP error, including an error whose body cannot be read.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from sextant.tradingview import account


@pytest.fixture
def offline_account(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    parameters = json.loads(account.DEFAULT_PARAMETERS.read_text())
    values = {
        "account_state_path": str(state_path),
        "account_calls_per_utc_day": 10,
        "account_request_min_interval_s": 2,
        "account_request_timeout_s": 60,
    }
    for row in parameters["rows"]:
        row["value"] = values[row["name"]]
        row["source"] = "offline review fixture; no operating authority"
    parameter_path = tmp_path / "account.params.json"
    parameter_path.write_text(json.dumps(parameters))
    monkeypatch.setattr(account, "DEFAULT_PARAMETERS", parameter_path)
    clock = [datetime(2026, 11, 11, 15, tzinfo=UTC)]
    monkeypatch.setattr(account, "_utcnow", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(account, "_sleep", sleep)
    state_path.write_text(
        json.dumps(
            {
                "schema": "tv-account.state/2",
                "day": "2026-11-11",
                "calls": 0,
                "stopped": False,
                "quota_stop": False,
                "last_call": None,
                "cache": {},
                "audit": [],
            }
        )
    )


@pytest.mark.parametrize(
    "status,expected_latch",
    [(status, status not in {400, 404, 405, 406}) for status in [*range(400, 600), 600, 700, 999]],
)
def test_only_named_get_status_exceptions_change(offline_account, status, expected_latch):
    stream_url = "https://offline.invalid/mcp"
    gate = account.HTTPGate(stream_url=stream_url)
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx2.Response(status if request.method == "GET" else 202, text="offline")

    async def exercise():
        async with httpx2.AsyncClient(
            transport=account.GuardedHTTPTransport(httpx2.MockTransport(server), gate),
            event_hooks={"request": [gate.request], "response": [gate.response]},
        ) as client:
            try:
                await client.get(
                    stream_url,
                    headers={
                        "mcp-session-id": "offline-session",
                        "accept": "application/json, text/event-stream",
                    },
                )
            except account.AccountError:
                pass
            state = account.Account().snapshot()
            try:
                await client.post(stream_url, json={"method": "notifications/initialized"})
            except account.AccountError:
                pass
            return state

    state = asyncio.run(exercise())
    print(
        f"GET {status}: stopped={state['stopped']} quota_stop={state['quota_stop']} "
        f"action={state['audit'][-1]['action']} physical_calls={calls}"
    )
    assert state["quota_stop"] is expected_latch
    assert state["stopped"] is expected_latch
    assert calls == (["GET"] if expected_latch else ["GET", "POST"])


@pytest.mark.parametrize("status", [403, 500])
def test_other_get_status_stays_latched_when_its_json_body_cannot_be_read(offline_account, status):
    gate = account.HTTPGate(stream_url="https://offline.invalid/mcp")
    calls = []

    class BrokenBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            raise httpx2.ReadError("offline body failure")
            yield b""

    def server(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx2.Response(
                status, headers={"content-type": "application/json"}, stream=BrokenBody()
            )
        return httpx2.Response(202)

    async def exercise():
        async with httpx2.AsyncClient(
            transport=account.GuardedHTTPTransport(httpx2.MockTransport(server), gate),
            event_hooks={"request": [gate.request], "response": [gate.response]},
        ) as client:
            with pytest.raises(httpx2.ReadError):
                await client.get(
                    "https://offline.invalid/mcp",
                    headers={"mcp-session-id": "offline-session", "accept": "text/event-stream"},
                )
            with pytest.raises(account.AccountError, match="latched"):
                await client.post("https://offline.invalid/mcp", json={"method": "ping", "id": 1})

    asyncio.run(exercise())
    state = account.Account().snapshot()
    assert state["stopped"] is True and state["quota_stop"] is True
    assert state["calls"] == 1 and calls == ["GET"]
    assert state["audit"][-1]["action"] == "stop"
    assert state["audit"][-1]["status_code"] == status


@pytest.mark.parametrize("kind", [httpx2.ConnectError, httpx2.ReadTimeout])
def test_no_response_transport_failure_stays_latched(offline_account, kind):
    stream_url = "https://offline.invalid/mcp"
    gate = account.HTTPGate(stream_url=stream_url)
    calls = []

    def server(request):
        calls.append(request.method)
        if request.method == "GET":
            raise kind("offline transport failure")
        return httpx2.Response(202)

    async def exercise():
        async with httpx2.AsyncClient(
            transport=account.GuardedHTTPTransport(httpx2.MockTransport(server), gate),
            event_hooks={"request": [gate.request], "response": [gate.response]},
        ) as client:
            with pytest.raises(kind):
                await client.get(
                    stream_url,
                    headers={"mcp-session-id": "offline-session", "accept": "text/event-stream"},
                )
            state = account.Account().snapshot()
            try:
                await client.post(stream_url, json={"method": "notifications/initialized"})
            except account.AccountError:
                pass
        assert gate.pending is None
        return state

    state = asyncio.run(exercise())
    assert state["stopped"] is True and state["quota_stop"] is True
    assert calls == ["GET"] and account.Account().snapshot()["calls"] == 1
    assert state["audit"][-1]["action"] == "stop"
    assert state["audit"][-1]["status_code"] is None
    assert state["audit"][-1]["rule"] == kind.__name__

"""D-80: exercise the unchanged SDK GET loop over a local in-memory server.

SDK 2.3.0 streamable_http.py:59-60,224-273 supplies the retry/return behavior;
our guard must meter and record the attempts without turning no-stream into a stop.
"""

import asyncio
import json

import anyio
import httpx2
import pytest
from mcp.client import streamable_http
from mcp.shared._context_streams import create_context_streams
from mcp.shared.message import SessionMessage

from sextant.tradingview import account

PRIVATE = "fixture-private-list-and-token"
URL = f"https://offline.invalid/mcp?token={PRIVATE}"


@pytest.fixture(autouse=True)
def process_ordinal(monkeypatch):
    monkeypatch.setattr(account, "_ORDINAL", 0)


async def sdk_get(handler, monkeypatch):
    delays = []

    async def retry_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(anyio, "sleep", retry_sleep)
    gate = account.HTTPGate()
    # Configure the endpoint without requiring a new constructor API in RED.
    gate.stream_url = URL
    transport = streamable_http.StreamableHTTPTransport(URL)
    transport.session_id = PRIVATE
    writer, reader = create_context_streams[SessionMessage | Exception](0)
    async with (
        writer,
        reader,
        httpx2.AsyncClient(
            transport=account.GuardedHTTPTransport(httpx2.MockTransport(handler), gate),
            event_hooks={"request": [gate.request], "response": [gate.response]},
        ) as client,
    ):
        await transport.handle_get_stream(client, writer)
    return delays


def records(capsys):
    state = account.Account().snapshot()
    output = capsys.readouterr()
    assert output.out == ""
    assert [json.loads(line.split("=", 1)[1]) for line in output.err.splitlines()] == state["audit"]
    for text in (json.dumps(state["audit"]), output.err):
        for forbidden in (PRIVATE, "offline.invalid", "Bearer", "Authorization", "params"):
            assert forbidden not in text
    return state


@pytest.mark.parametrize(
    "status,expected_latch",
    [(400, False), (403, True), (404, False), (405, False), (406, False), (500, True)],
)
def test_sdk_optional_get_statuses_preserve_other_http_stops(
    monkeypatch, capsys, status, expected_latch
):
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx2.Response(status, text=PRIVATE)

    delays = asyncio.run(sdk_get(server, monkeypatch))
    state = records(capsys)
    assert delays == [1.0]  # Native SDK: two attempts, including a refused retry after a stop.
    if expected_latch:
        assert state["stopped"] is True and state["quota_stop"] is True
        assert state["calls"] == 1 and calls == ["GET"]
        assert len(state["audit"]) == 1
        assert state["audit"][0]["action"] == "stop"
        assert state["audit"][0]["status_code"] == status
        with pytest.raises(account.AccountError, match="latched"):
            account.Account().preflight()
        return
    assert state["stopped"] is False and state["quota_stop"] is False
    assert state["calls"] == 2 and calls == ["GET", "GET"]
    assert state["audit"] == [
        {
            "action": "no_stream",
            "at": "2026-11-11T15:00:00+00:00",
            "http_method": "GET",
            "status_code": status,
            "rpc_method": None,
            "rule": "status",
            "request_ordinal": 1,
        },
        {
            "action": "no_stream",
            "at": "2026-11-11T15:00:02+00:00",
            "http_method": "GET",
            "status_code": status,
            "rpc_method": None,
            "rule": "status",
            "request_ordinal": 2,
        },
    ]
    account.Account().preflight()


@pytest.mark.parametrize(
    "status,text,rule",
    [
        (429, PRIVATE, "status"),
        (400, f"QUOTA {PRIVATE}", "quota_text"),
        (404, f"Suspended {PRIVATE}", "quota_text"),
        (405, f"quota {PRIVATE}", "quota_text"),
    ],
)
def test_sdk_optional_get_quota_or_suspension_still_latches(
    monkeypatch, capsys, status, text, rule
):
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx2.Response(status, text=text)

    asyncio.run(sdk_get(server, monkeypatch))
    state = records(capsys)
    assert state["stopped"] is True and state["quota_stop"] is True
    assert calls == ["GET"] and state["calls"] == 1
    assert len(state["audit"]) == 1
    assert state["audit"][0] == {
        "action": "stop",
        "at": "2026-11-11T15:00:00+00:00",
        "http_method": "GET",
        "status_code": status,
        "rpc_method": None,
        "rule": rule,
        "request_ordinal": 1,
    }
    with pytest.raises(account.AccountError, match="latched"):
        account.Account().preflight()


@pytest.mark.parametrize("kind", [httpx2.ReadTimeout, httpx2.ConnectError])
def test_sdk_optional_get_failures_without_response_stay_latched(monkeypatch, capsys, kind):
    calls = []

    def server(request):
        calls.append(request.method)
        raise kind(PRIVATE)

    assert asyncio.run(sdk_get(server, monkeypatch)) == [1.0]
    state = records(capsys)
    assert state["stopped"] is True and state["quota_stop"] is True
    assert calls == ["GET"] and state["calls"] == 1
    assert [row["action"] for row in state["audit"]] == ["stop"]
    assert [row["rule"] for row in state["audit"]] == [kind.__name__]
    assert [row["status_code"] for row in state["audit"]] == [None]
    assert [row["request_ordinal"] for row in state["audit"]] == [1]
    with pytest.raises(account.AccountError, match="latched"):
        account.Account().preflight()


def test_sdk_optional_get_cancellation_still_latches(monkeypatch, capsys):
    def server(request):
        raise asyncio.CancelledError(PRIVATE)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sdk_get(server, monkeypatch))
    state = records(capsys)
    assert state["stopped"] is True and state["quota_stop"] is True
    assert state["calls"] == 1 and len(state["audit"]) == 1
    assert state["audit"][0]["rule"] == "CancelledError"


@pytest.mark.parametrize(
    "method,url", [("GET", "https://offline.invalid/metadata"), ("POST", URL), ("DELETE", URL)]
)
def test_optional_get_alignment_does_not_relax_other_requests(capsys, method, url):
    gate = account.HTTPGate()
    gate.stream_url = URL

    async def run():
        async with httpx2.AsyncClient(
            transport=account.GuardedHTTPTransport(
                httpx2.MockTransport(lambda request: httpx2.Response(404, text=PRIVATE)), gate
            ),
            event_hooks={"request": [gate.request], "response": [gate.response]},
        ) as client:
            await client.request(method, url)

    with pytest.raises(account.AccountError):
        asyncio.run(run())
    state = records(capsys)
    assert state["stopped"] is True and state["quota_stop"] is True
    assert state["calls"] == 1 and state["audit"][0]["action"] == "stop"


def test_official_client_configures_the_sdk_stream_endpoint(monkeypatch):
    from test_tradingview_account import normal_http_handler, wire_http

    from sextant.tradingview import auth

    log = []
    first_get = asyncio.Event()
    respond = normal_http_handler(log, get_status=400, delete_status=405)

    def server(request):
        if request.method == "GET":
            first_get.set()
        return respond(request)

    async def retry_sleep(delay):
        pass

    monkeypatch.setattr(anyio, "sleep", retry_sleep)
    wire_http(monkeypatch, server)

    async def run():
        async with auth.official_client(True):
            await asyncio.wait_for(first_get.wait(), timeout=2)
            assert account.Account().snapshot()["quota_stop"] is False

    asyncio.run(run())
    state = account.Account().snapshot()
    assert state["calls"] == len(log)
    assert state["quota_stop"] is False
    assert any(row["action"] == "no_stream" and row["status_code"] == 400 for row in state["audit"])


def test_sdk_same_origin_stream_redirect_keeps_no_stream_handling(monkeypatch, capsys):
    calls = []

    def server(request):
        calls.append(request.url.path)
        if request.url.path == "/mcp":
            return httpx2.Response(307, headers={"location": "/canonical-mcp"})
        return httpx2.Response(404, text=PRIVATE)

    assert asyncio.run(sdk_get(server, monkeypatch)) == [1.0]
    state = records(capsys)
    assert state["quota_stop"] is False and state["stopped"] is False
    assert calls == ["/mcp", "/canonical-mcp", "/mcp", "/canonical-mcp"]
    assert state["calls"] == 4
    assert [row["action"] for row in state["audit"]] == ["no_stream", "no_stream"]
    assert [row["request_ordinal"] for row in state["audit"]] == [2, 4]


def test_sdk_get_retry_cannot_exceed_the_shared_daily_cap(monkeypatch, capsys):
    parameters = account.DEFAULT_PARAMETERS
    registry = json.loads(parameters.read_text())
    for row in registry["rows"]:
        if row["name"] == "account_calls_per_utc_day":
            row["value"] = 1
    parameters.write_text(json.dumps(registry))
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx2.Response(400, text=PRIVATE)

    asyncio.run(sdk_get(server, monkeypatch))
    state = records(capsys)
    assert calls == ["GET"] and state["calls"] == 1
    assert state["quota_stop"] is False and state["stopped"] is False
    assert len(state["audit"]) == 1 and state["audit"][0]["action"] == "no_stream"
    with pytest.raises(account.AccountError, match="budget exhausted"):
        account.Account().preflight()


@pytest.mark.parametrize("text", [f"quota {PRIVATE}", f"suspended {PRIVATE}"])
def test_quota_text_on_a_non_sse_200_get_still_latches(monkeypatch, capsys, text):
    asyncio.run(sdk_get(lambda request: httpx2.Response(200, text=text), monkeypatch))
    state = records(capsys)
    assert state["quota_stop"] is True and state["stopped"] is True
    assert state["calls"] == 1 and state["audit"][0]["rule"] == "quota_text"


def test_sdk_invalid_stream_content_type_is_counted_and_recorded_as_no_stream(monkeypatch, capsys):
    assert asyncio.run(
        sdk_get(lambda request: httpx2.Response(200, text=PRIVATE), monkeypatch)
    ) == [1.0]
    state = records(capsys)
    assert state["quota_stop"] is False and state["stopped"] is False
    assert state["calls"] == 2
    assert [row["action"] for row in state["audit"]] == ["no_stream", "no_stream"]
    assert [row["rule"] for row in state["audit"]] == ["SSEError", "SSEError"]


def test_get_429_latches_even_when_its_body_cannot_be_read(monkeypatch, capsys):
    class BrokenBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            raise httpx2.ReadError(PRIVATE)
            yield b""

    asyncio.run(sdk_get(lambda request: httpx2.Response(429, stream=BrokenBody()), monkeypatch))
    state = records(capsys)
    assert state["quota_stop"] is True and state["stopped"] is True
    assert state["calls"] == 1 and state["audit"][0]["rule"] == "status"

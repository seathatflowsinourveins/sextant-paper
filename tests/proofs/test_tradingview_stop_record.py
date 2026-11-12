"""LEAD-1904: local httpx2 fake server, no sockets, login or external calls.

Breaks caught: dropping/misclassifying a latch record, leaking request/response
content, losing a deferred HTTP status, or treating optional GET errors as accepted.
"""

import asyncio
import json

import httpx2
import pytest

from sextant.tradingview import account

# Deliberately distinct content at every forbidden boundary; these are fake data.
PRIVATE = "fixture-private-list-and-token"
URL = f"https://offline.invalid/mcp?token={PRIVATE}"


@pytest.fixture(autouse=True)
def process_ordinal(monkeypatch):
    monkeypatch.setattr(account, "_ORDINAL", 0, raising=False)


def assert_record(capsys, *, method, status, rpc, rule, ordinal=1):
    state = account.Account().snapshot()
    assert state["stopped"] is True and state["quota_stop"] is True
    assert state["audit"] == [
        {
            "action": "stop",
            "at": "2026-11-11T15:00:00+00:00" if ordinal == 1 else state["last_call"],
            "http_method": method,
            "status_code": status,
            "rpc_method": rpc,
            "rule": rule,
            "request_ordinal": ordinal,
        }
    ]
    output = capsys.readouterr()
    assert output.out == ""
    lines = output.err.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0].split("stop=", 1)[1]) == state["audit"][0]
    for text in (json.dumps(state["audit"]), output.err):
        for forbidden in (PRIVATE, "offline.invalid", "Authorization", "Bearer", "params"):
            assert forbidden not in text


async def http_case(handler, *, method="POST", rpc: str | None = "tools/call", known=False):
    gate = account.HTTPGate(known_response=lambda response: known)
    async with httpx2.AsyncClient(
        transport=account.GuardedHTTPTransport(httpx2.MockTransport(handler), gate),
        event_hooks={"request": [gate.request], "response": [gate.response]},
    ) as client:
        request = client.build_request(
            method,
            URL,
            headers={"Authorization": f"Bearer {PRIVATE}"},
            json={"jsonrpc": "2.0", "id": 1, "method": rpc, "params": {"name": PRIVATE}},
        )
        return await client.send(request)


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_http_status_latch_has_only_safe_context(capsys, status):
    with pytest.raises(account.AccountError, match="stop latched"):
        asyncio.run(http_case(lambda request: httpx2.Response(status, text=PRIVATE)))
    assert_record(capsys, method="POST", status=status, rpc="tools/call", rule="status")


@pytest.mark.parametrize("payload", [{"isError": True}, {"error": PRIVATE}, {"success": False}])
def test_json_error_flag_records_the_rule(capsys, payload):
    with pytest.raises(account.AccountError):
        asyncio.run(
            http_case(
                lambda request: httpx2.Response(200, json={"nested": [payload], "text": PRIVATE}),
                rpc=None,
            )
        )
    assert_record(capsys, method="POST", status=200, rpc=None, rule="is_error")


@pytest.mark.parametrize("text", [f"QUOTA {PRIVATE}", f"Suspended {PRIVATE}"])
@pytest.mark.parametrize("known", [False, True])
def test_quota_text_records_only_its_class(capsys, text, known):
    with pytest.raises(account.AccountError):
        asyncio.run(
            http_case(
                lambda request: httpx2.Response(200, json={"content": [{"text": text}]}),
                rpc=None,
                known=known,
            )
        )
    assert_record(capsys, method="POST", status=200, rpc=None, rule="quota_text")


@pytest.mark.parametrize("kind", [httpx2.ReadTimeout, TimeoutError, asyncio.CancelledError])
def test_transport_exception_records_class_without_exception_text(capsys, kind):
    def fail(request):
        raise kind(PRIVATE)

    with pytest.raises(kind):
        asyncio.run(http_case(fail))
    assert_record(capsys, method="POST", status=None, rpc="tools/call", rule=kind.__name__)


@pytest.mark.parametrize("http", [False, True])
@pytest.mark.parametrize(
    "payload,rule", [({"isError": True}, "is_error"), ({"text": "suspended"}, "quota_text")]
)
def test_dispatcher_retains_class_and_deferred_http_status(capsys, http, payload, rule):
    gate = account.HTTPGate() if http else None

    class Dispatcher:
        async def send_raw_request(self, method, params, opts):
            if gate is not None:
                request = httpx2.Request("POST", URL, json={"id": 1, "method": method})
                await gate.request(request)
                await gate.response(httpx2.Response(200, request=request))
            return payload

    dispatcher = account.MeteredDispatcher(Dispatcher(), account.Account(), gate)
    with pytest.raises(account.AccountError):
        asyncio.run(dispatcher.send_raw_request("tools/call", {"name": PRIVATE}))
    assert_record(
        capsys,
        method="POST" if http else None,
        status=200 if http else None,
        rpc="tools/call",
        rule=rule,
    )


def test_in_process_send_exception_records_without_an_http_request(capsys):
    async def fail():
        raise TimeoutError(PRIVATE)

    with pytest.raises(TimeoutError):
        asyncio.run(account.Account().request(fail))
    assert_record(capsys, method=None, status=None, rpc=None, rule="TimeoutError")


@pytest.mark.parametrize("status", [400, 404, 406])
def test_unconfigured_get_errors_still_latch_and_prevent_another_call(capsys, status):
    # D-80 configures only the SDK stream origin/session/SSE requests in auth;
    # a bare gate's other GETs retain the recording-only commit's strict policy.
    calls = []

    def server(request):
        calls.append(request.method)
        return httpx2.Response(status, text=PRIVATE)

    async def run():
        with pytest.raises(account.AccountError):
            await http_case(server, method="GET")
        with pytest.raises(account.AccountError, match="latched"):
            await http_case(server, method="GET")

    asyncio.run(run())
    assert calls == ["GET"]
    assert account.Account().snapshot()["calls"] == 1
    assert_record(capsys, method="GET", status=status, rpc=None, rule="status")


@pytest.mark.parametrize("method,status", [("GET", 405), ("DELETE", 405), ("GET", 200)])
def test_optional_endpoint_acceptance_is_unchanged_and_has_no_stop_record(capsys, method, status):
    asyncio.run(http_case(lambda request: httpx2.Response(status), method=method))
    state = account.Account().snapshot()
    assert state["stopped"] is False and state["quota_stop"] is False
    assert state["audit"] == [] and capsys.readouterr().err == ""


def test_request_ordinal_spans_gate_instances_and_successful_calls(capsys):
    async def run():
        await http_case(lambda request: httpx2.Response(202), rpc=None)
        await account.Account().request(success)
        with pytest.raises(account.AccountError):
            await http_case(lambda request: httpx2.Response(404), method="GET")

    async def success():
        return {}

    asyncio.run(run())
    assert_record(capsys, method="GET", status=404, rpc=None, rule="status", ordinal=3)


def test_arbitrary_rpc_method_and_exception_class_names_do_not_leak(capsys):
    unusual_error = type(PRIVATE, (RuntimeError,), {})

    def fail(request):
        raise unusual_error(PRIVATE)

    with pytest.raises(unusual_error):
        asyncio.run(http_case(fail, rpc=PRIVATE))
    assert_record(capsys, method="POST", status=None, rpc="unknown", rule="UnrecognizedError")


def test_response_read_exception_retains_status_without_body(capsys):
    class BrokenBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            raise httpx2.ReadError(PRIVATE)
            yield b""  # async iterator contract; no response data is yielded.

    with pytest.raises(httpx2.ReadError):
        asyncio.run(
            http_case(
                lambda request: httpx2.Response(
                    200, headers={"content-type": "application/json"}, stream=BrokenBody()
                ),
                rpc=None,
            )
        )
    assert_record(capsys, method="POST", status=200, rpc=None, rule="ReadError")


def test_malformed_json_latches_with_its_exception_class(capsys):
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(
            http_case(
                lambda request: httpx2.Response(
                    200, headers={"content-type": "application/json"}, content=PRIVATE
                ),
                rpc=None,
            )
        )
    assert_record(capsys, method="POST", status=200, rpc=None, rule="JSONDecodeError")


def test_notification_stop_names_the_rpc_notification_without_logging_params(capsys):
    gate = account.HTTPGate()
    request = httpx2.Request(
        "POST",
        URL,
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {"name": PRIVATE}},
    )

    async def run():
        await gate.request(request)
        with pytest.raises(account.AccountError):
            await gate.response(httpx2.Response(400, request=request))

    asyncio.run(run())
    assert_record(capsys, method="POST", status=400, rpc="notifications/initialized", rule="status")


def test_stop_preserves_prior_audit_and_a_rejected_retry_adds_no_record(capsys):
    item = account.Account()
    with item.locked() as state:
        state["audit"].append({"action": "fixture prior event"})
        account.write_atomic(item.path, state)
    with pytest.raises(account.AccountError):
        asyncio.run(http_case(lambda request: httpx2.Response(404), method="GET"))
    first = item.snapshot()
    assert first["audit"][0] == {"action": "fixture prior event"}
    assert len(first["audit"]) == 2
    capsys.readouterr()
    with pytest.raises(account.AccountError, match="latched"):
        item.preflight()
    assert item.snapshot() == first
    assert capsys.readouterr().err == ""

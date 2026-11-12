"""LEAD-0937 Part A: account guard proofs, offline with no symbol resolver."""

import asyncio
import fcntl
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server import Server

from sextant.tradingview import account
from sextant.tradingview.account import (
    Account,
    AccountError,
    MeteredClient,
    MeteredDispatcher,
    main,
)
from sextant.tradingview.sync import OfficialTools, SyncError

NOW = datetime(2026, 11, 11, 15, tzinfo=UTC)


def policy(value: int | None = 10, **changes):
    path = account.DEFAULT_PARAMETERS
    data = json.loads(path.read_text())
    data["rows"][0].update(value=value, **changes)
    path.write_text(json.dumps(data))
    return path


async def success():
    return {"ok": True}


@pytest.mark.parametrize("error", [AccountError("quota"), TimeoutError(), asyncio.CancelledError()])
def test_dispatched_failure_latches_across_restarts_days_and_clock_jumps(error):
    policy()
    calls = []

    async def fail():
        calls.append("sent")
        raise error

    with pytest.raises(type(error)):
        asyncio.run(Account(now=lambda: NOW).request(fail))
    for days in (1, 7, 365):
        later = Account(now=lambda days=days: NOW + timedelta(days=days))
        with pytest.raises(AccountError, match="latched"):
            asyncio.run(later.request(fail))
    assert calls == ["sent"]


def test_two_clients_share_cap_and_alternate_registry_cannot_reset_it(tmp_path):
    path = policy(2)
    assert asyncio.run(Account().request(success)) == {"ok": True}
    assert asyncio.run(Account().request(success)) == {"ok": True}
    with pytest.raises(AccountError, match="budget exhausted"):
        asyncio.run(Account().request(success))
    assert Account().snapshot()["calls"] == 2
    other = tmp_path / "other.params.json"
    other.write_bytes(path.read_bytes())
    with pytest.raises(AccountError, match="canonical"):
        Account(parameters=other)


@pytest.mark.parametrize("bad", [None, "truncated"])
def test_missing_or_corrupt_state_refuses_without_reset(tmp_path, bad):
    path = tmp_path / "state.json"
    if bad is None:
        path.unlink()
    else:
        path.write_text(bad)
    with pytest.raises(AccountError, match="state"):
        asyncio.run(Account().request(success))
    assert (path.read_text() if path.exists() else None) == bad


def test_concurrent_writer_and_regressing_day_cannot_reset_usage(tmp_path):
    asyncio.run(Account().request(success))
    before = (tmp_path / "state.json").read_bytes()
    with (tmp_path / "state.json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AccountError, match="writer"):
            Account().preflight()
    with pytest.raises(AccountError, match="clock"):
        Account(now=lambda: NOW - timedelta(days=1)).preflight()
    assert (tmp_path / "state.json").read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"value": None},
        {"value": True},
        {"value": 2.5},
        {"value": 0},
        {"source": ""},
        {"calibration_state": "MEASURED"},
        {"calibration_state": "FIXED"},
        {"unit": "seconds"},
    ],
)
def test_invalid_or_unavailable_budget_refuses_before_dispatch(changes):
    policy(**changes)
    with pytest.raises(AccountError):
        asyncio.run(Account().request(success))
    assert Account().snapshot()["calls"] == 0


@pytest.mark.parametrize("spacing", [None, 0.75, 1.5])
def test_spacing_requires_authority_slower_than_both_failed_incident_runs(spacing):
    data = json.loads(account.DEFAULT_PARAMETERS.read_text())
    data["rows"][2]["value"] = spacing
    account.DEFAULT_PARAMETERS.write_text(json.dumps(data))
    with pytest.raises(AccountError):
        asyncio.run(Account().request(success))
    assert Account().snapshot()["calls"] == 0


def test_pacing_is_durable_across_clients_and_shared_with_sync(monkeypatch):
    at, stamps = [NOW], []

    async def sleep(seconds):
        at[0] += timedelta(seconds=seconds)

    async def send(*args, **kwargs):
        stamps.append(at[0].timestamp())
        return SimpleNamespace(is_error=False, structured_content={"watchlists": []}, content=[])

    monkeypatch.setattr(account, "_utcnow", lambda: at[0])
    monkeypatch.setattr(account, "_sleep", sleep)
    asyncio.run(Account().request(send))
    asyncio.run(Account().request(send))
    tools = OfficialTools(cast(Any, SimpleNamespace(session=SimpleNamespace(call_tool=send))))
    tools.schemas["list_watchlists"] = {"type": "object"}
    tools.wire["list_watchlists"] = "list_watchlists"
    assert asyncio.run(tools.call("list_watchlists", {})) == {"watchlists": []}
    assert stamps == [NOW.timestamp(), NOW.timestamp() + 2, NOW.timestamp() + 4]
    assert Account().snapshot()["calls"] == 3


def test_named_lead_clear_is_audited_and_preserves_usage_and_spacing():
    policy()

    async def fail():
        raise AccountError("quota")

    with pytest.raises(AccountError):
        asyncio.run(Account(now=lambda: NOW).request(fail))
    before = Account().snapshot()
    item = Account(now=lambda: NOW)
    item.clear(lead="fixture lead", authority="offline reviewed clear")
    state = item.snapshot()
    assert state["calls"] == before["calls"] == 1
    assert state["last_call"] == before["last_call"]
    assert state["audit"][-1] == {
        "action": "clear",
        "lead": "fixture lead",
        "authority": "offline reviewed clear",
        "at": NOW.isoformat(),
    }
    assert asyncio.run(Account().request(success)) == {"ok": True}


def test_explicit_init_stays_latched_and_refuses_existing_state(tmp_path):
    (tmp_path / "state.json").unlink()
    args = ["--lead", "fixture lead", "--authority", "offline init"]
    assert main(["init", *args]) == 0
    with pytest.raises(AccountError, match="latched"):
        Account().preflight()
    assert main(["init", *args]) == 2
    assert main(["clear", "--lead", "", "--authority", "offline clear"]) == 2
    assert main(["clear", *args]) == 0
    Account().preflight()
    assert [r["action"] for r in Account().snapshot()["audit"]] == ["init", "clear"]


def test_sdk_setup_and_notification_consume_cap_before_tool_discovery():
    policy(2)

    async def exercise():
        async with MeteredClient(Server("offline"), mode="legacy") as client:
            assert Account().snapshot()["calls"] == 2
            with pytest.raises(AccountError, match="budget exhausted"):
                await client.list_tools(cache_mode="refresh")

    asyncio.run(exercise())
    assert Account().snapshot()["calls"] == 2


def test_latched_sdk_client_never_opens_transport(tmp_path):
    state = Account().snapshot()
    state["quota_stop"] = True
    (tmp_path / "state.json").write_text(json.dumps(state))
    entered = []

    @asynccontextmanager
    async def lifespan(server):
        entered.append(True)
        yield {}

    async def exercise():
        with pytest.raises(AccountError, match="latched"):
            async with MeteredClient(Server("offline", lifespan=lifespan), mode="legacy"):
                pytest.fail("latched client opened")

    asyncio.run(exercise())
    assert entered == []


@pytest.mark.parametrize(
    "result",
    [
        {"isError": True},
        {"outputSchema": {"error": "TOOL_QUOTA_EXCEEDED"}},
        {"structuredContent": {"error": {"code": "TOOL_QUOTA_EXCEEDED"}}},
        {"content": [{"type": "text", "text": "Account MCP access is suspended"}]},
    ],
)
def test_sdk_quota_envelopes_latch_and_stop_every_sync_tool(result):
    class Upstream:
        async def send_raw_request(self, *args):
            return result

    with pytest.raises(AccountError):
        asyncio.run(MeteredDispatcher(Upstream(), Account()).send_raw_request("tools/call", {}))
    with pytest.raises(AccountError, match="latched"):
        Account(now=lambda: NOW + timedelta(days=7)).preflight()
    with pytest.raises(SyncError):
        asyncio.run(OfficialTools(cast(Any, object())).discover())


def wire_http(monkeypatch, handler):
    import httpx2

    from sextant.tradingview import auth

    clients, original = [], httpx2.AsyncClient

    def http_client(**options):
        options.setdefault("transport", httpx2.MockTransport(handler))
        client = original(trust_env=False, **options)
        clients.append(client)
        return client

    callback = SimpleNamespace(
        uri="http://127.0.0.1:1/callback",
        redirect=None,
        wait=None,
        server=SimpleNamespace(server_close=lambda: None),
        result=None,
    )
    monkeypatch.setattr(auth, "OwnerCallback", lambda: callback)
    monkeypatch.setattr(auth, "OAuthClientProvider", lambda **kwargs: None)
    monkeypatch.setattr(auth, "SERVER", "https://offline.invalid/mcp")
    monkeypatch.setattr(auth.httpx2, "AsyncClient", http_client)
    monkeypatch.setattr(auth.httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handler))

    return clients


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
def test_http_requests_are_charged_spaced_and_blocked_after_quota(monkeypatch, sse):
    import httpx2

    from sextant.tradingview import auth

    log = []
    monkeypatch.setattr(Account, "timeout", lambda self: 2)

    def handler(request):
        method = request.method
        log.append((method, account._utcnow().timestamp()))
        if request.url.path == "/metadata":
            result = {"issuer": "offline"}
        elif method == "GET":
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=b"")
        elif method == "DELETE":
            result = {}
        else:
            body = json.loads(request.content)
            method = body.get("method", method)
            if method == "initialize" and request.url.path == "/mcp":
                return httpx2.Response(307, headers={"location": "/target"})
            if "id" not in body:
                return httpx2.Response(202)
            result = (
                {
                    "protocolVersion": body["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "offline", "version": "0"},
                }
                if method == "initialize"
                else {
                    "content": [{"type": "text", "text": "TOOL_QUOTA_EXCEEDED; suspended"}],
                    "isError": True,
                }
            )
            result = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        headers = {"mcp-session-id": "offline"}
        if sse and method == "tools/call":
            return httpx2.Response(
                200,
                headers={**headers, "content-type": "text/event-stream"},
                content=("data: " + json.dumps(result) + "\n\n").encode(),
            )
        return httpx2.Response(200, headers=headers, json=result)

    clients = wire_http(monkeypatch, handler)

    async def exercise():
        async with auth.official_client(True) as client:
            await clients[0].get("https://offline.invalid/metadata", auth=None)
            await clients[0].delete("https://offline.invalid/resource", auth=None)
            tools = OfficialTools(client)
            tools.schemas["list_watchlists"] = {"type": "object"}
            tools.wire["list_watchlists"] = "list_watchlists"
            with pytest.raises(SyncError):
                await tools.call("list_watchlists", {})
            assert Account().snapshot()["quota_stop"] is True
            before = len(log)
            for method in ("GET", "POST", "DELETE"):
                try:
                    await clients[0].request(method, auth.SERVER, json={})
                except AccountError:
                    continue
            await asyncio.sleep(3.5)  # R6: transport GET reconnects during idle.
        return before

    before = asyncio.run(exercise())
    print("ledger/http/after", Account().snapshot()["calls"], len(log), len(log[before:]))
    assert log[before:] == []
    assert Account().snapshot()["calls"] == len(log)
    assert all(b[1] - a[1] >= 2 for a, b in pairwise(log))


def normal_http_handler(log, *, get_status=200, delete_status=200):
    import httpx2

    from sextant.tradingview.sync import WIRE

    def handler(request):
        log.append((request.method, time.monotonic()))
        if request.method in {"GET", "DELETE"}:
            status = get_status if request.method == "GET" else delete_status
            return httpx2.Response(
                status, headers={"content-type": "text/event-stream"}, content=b""
            )
        body = json.loads(request.content)
        if "id" not in body:
            return httpx2.Response(202)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "offline", "version": "0"},
            }
        else:
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in WIRE]}
        return httpx2.Response(
            200,
            headers={"mcp-session-id": "offline"},
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    return handler


@pytest.mark.parametrize(("get_status", "delete_status"), [(405, 200), (200, 405)])
def test_optional_get_or_delete_405_does_not_stop_the_account(
    monkeypatch, get_status, delete_status
):
    from sextant.tradingview import auth

    log = []
    wire_http(
        monkeypatch, normal_http_handler(log, get_status=get_status, delete_status=delete_status)
    )

    async def exercise():
        for _ in range(2):
            async with auth.official_client(True):
                await asyncio.sleep(0.02)
            assert Account().snapshot()["stopped"] is False
            assert Account().snapshot()["quota_stop"] is False
            Account().preflight()

    asyncio.run(exercise())
    assert {method for method, _ in log} == {"POST", "GET", "DELETE"}
    assert Account().snapshot()["calls"] == len(log)


@pytest.mark.parametrize(("method", "status"), [("POST", 405), ("GET", 429), ("DELETE", 403)])
def test_optional_endpoint_exception_does_not_accept_other_refusals(method, status):
    import httpx2

    gate = account.HTTPGate()
    request = httpx2.Request(method, "https://offline.invalid/mcp")

    async def exercise():
        await gate.request(request)
        with pytest.raises(AccountError, match="stop latched"):
            await gate.response(httpx2.Response(status, request=request))

    asyncio.run(exercise())
    assert Account().snapshot()["calls"] == 1
    with pytest.raises(AccountError, match="latched"):
        Account().preflight()


def test_first_discovery_after_open_uses_real_spacing_without_writer_refusal(monkeypatch):
    from sextant.tradingview import auth

    started = time.monotonic()
    monkeypatch.setattr(
        account, "_utcnow", lambda: NOW + timedelta(seconds=time.monotonic() - started)
    )
    monkeypatch.setattr(account, "_sleep", asyncio.sleep)
    monkeypatch.setattr(Account, "policy", lambda self: (10000, 0.05))  # Offline fixture only.
    log, seen = [], {}
    wire_http(monkeypatch, normal_http_handler(log))

    async def exercise():
        async with auth.official_client(True) as client:
            try:
                Account().snapshot()
                seen["snapshot"] = "acquired"
            except AccountError as error:
                seen["snapshot"] = str(error)
            try:
                await OfficialTools(client).discover()
                seen["discovery"] = "ok"
            except SyncError as error:
                seen["discovery"] = str(error)
            await asyncio.sleep(0.2)

    asyncio.run(exercise())
    print("real-spacing startup", seen, "requests", len(log))
    assert seen == {"snapshot": "acquired", "discovery": "ok"}
    assert len(log) >= 5
    assert all(b[1] - a[1] >= 0.04 for a, b in pairwise(log))
    assert Account().snapshot()["calls"] == len(log)
    assert Account().snapshot()["quota_stop"] is False


@pytest.mark.parametrize("field", ["inputSchema", "outputSchema"])
def test_tool_schema_error_property_is_metadata_not_a_quota_answer(field):
    result = {
        "tools": [
            {
                "name": "offline",
                "inputSchema": {"type": "object"},
                field: {"type": "object", "properties": {"error": {"type": "string"}}},
            }
        ]
    }

    class Upstream:
        async def send_raw_request(self, *args):
            return result

    assert (
        asyncio.run(MeteredDispatcher(Upstream(), Account()).send_raw_request("tools/list", {}))
        == result
    )
    assert Account().snapshot()["quota_stop"] is False
    Account().preflight()


@pytest.fixture
def attended_registry(tmp_path, monkeypatch):
    """Use shipped authority unchanged; isolate only the durable state location."""
    shipped = Path(__file__).resolve().parents[2] / "lanes/tradingview/account.params.json"
    data = json.loads(shipped.read_text())
    for row in data["rows"]:
        if row["name"] == "account_state_path":
            row["value"] = str(tmp_path / "state.json")
    path = tmp_path / "attended.params.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(account, "DEFAULT_PARAMETERS", path)
    return data


def test_attended_registry_stops_before_request_183(attended_registry):
    async def exercise():
        item = Account()
        for _ in range(182):
            await item.request(success)
        with pytest.raises(AccountError, match="budget exhausted"):
            await item.request(success)
        assert item.snapshot()["calls"] == 182

    asyncio.run(exercise())


def test_attended_registry_spaces_requests_six_seconds_across_clients(attended_registry):
    asyncio.run(Account().request(success))
    first = datetime.fromisoformat(Account().snapshot()["last_call"])
    asyncio.run(Account().request(success))
    second = datetime.fromisoformat(Account().snapshot()["last_call"])
    assert second - first == timedelta(seconds=6)


def test_attended_registry_uses_sourced_sdk_timeout(attended_registry):
    assert Account().timeout() == 30


def test_attended_registry_records_uncalibrated_authority(attended_registry):
    for row in attended_registry["rows"]:
        if row["name"] == "account_state_path":
            continue
        assert row["calibration_state"] == "UNCALIBRATED"
        assert row["last_change"] == "2026-11-11"
        for source in (
            "LEAD-1614",
            "7e92258392599bd6b3e6295c58327de88199150c503c8cf113561450ccbddec0",
            "https://www.tradingview.com/mcp/docs",
            "read 2026-11-11",
            "REQUEST-NEED.md",
            "f4d025e77dfc5fb9aa7385a17bda3dc59ea8b0447e1f5e094c121dfb4a5d1409",
        ):
            assert source in row["source"]

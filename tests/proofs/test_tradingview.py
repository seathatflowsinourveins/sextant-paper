"""ADR-0021/26 witnesses against the real SDK's in-memory transport; no account."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import socket
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest
from mcp import Client
from mcp.server import MCPServer

NOW = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


def api():
    try:
        return importlib.import_module("sextant.tradingview.inputs"), importlib.import_module(
            "sextant.tradingview.sync"
        )
    except ModuleNotFoundError:
        pytest.fail("The fixture-tested TradingView sync is not implemented")


def member(symbol: str, venue: str | None = "NASDAQ") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "tradingview_symbol": f"{venue}:{symbol}" if venue else None,
        "labels": ["early:1"],
    }


def unit(path: Path, *, movers=None, focus=None) -> Path:
    movers = [member("AAPL"), member("MSFT")] if movers is None else movers
    focus = [member("MSFT")] if focus is None else focus
    generation = {"as_of": "2026-11-06T14:29:00+00:00", "sha256": "a" * 64}
    common = {
        "session_date": "2026-11-06",
        "generated_at": "2026-11-06T14:29:30+00:00",
        "as_of": "2026-11-06T14:29:10+00:00",
        "as_of_snapshot": generation["as_of"],
        "snapshot_status": "observed",
        "status": "fresh",
        "generation": generation,
    }
    documents = {
        "top100.json": {"schema": "morning-pack.candidates/1", **common, "candidates": movers},
        "focus.json": {"schema": "top100.focus/1", **common, "candidates": focus},
        "display-lists.json": {
            "schema": "ns-display-lists/1",
            **common,
            "ns_movers": movers,
            "ns_focus": focus,
            "bounds": {"gap_bound": "0.05", "rvol_bound": "2", "max_age_s": 600},
            "counts": {"ns_movers": len(movers), "ns_focus": len(focus)},
            "excluded_unknown": 0,
            "armed": "UNVERIFIED",
            "sources": {"snapshot": {"path": "snapshot.json", **generation}, "armed_sets": []},
        },
    }
    path.mkdir(parents=True, exist_ok=True)
    for name, doc in documents.items():
        (path / name).write_text(json.dumps(doc))
    return path


def change(path: Path, name: str, **values: Any) -> None:
    doc = json.loads((path / name).read_text())
    doc.update(values)
    (path / name).write_text(json.dumps(doc))


class Peer:
    """Pinned upstream test_sync.py/test_readback_verify.py's SDK fixture peer."""

    def __init__(self):
        self.server = MCPServer("fixture official TradingView", log_level="CRITICAL")
        self.lists = {
            "1": {"id": "1", "name": "NS movers", "symbols": ["NASDAQ:MSFT", "NYSE:IBM"]},
            "2": {"id": "2", "name": "NS focus", "symbols": []},
            "3": {"id": "3", "name": "Owner favorites", "symbols": ["NYSE:IBM"]},
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reject: set[str] = set()
        self.extras: set[str] = set()
        self.stubborn = False
        self.ambiguous = False
        self.malformed = False
        self.bad_create = False
        self.write_response: dict[str, Any] | None = None

        @self.server.tool(name="mcp-watchlist-list-watchlists")
        def list_watchlists() -> dict[str, Any]:
            self.calls.append(("list_watchlists", {}))
            return (
                {"partial": True, "watchlists": []}
                if self.malformed
                else {"watchlists": copy.deepcopy(list(self.lists.values()))}
            )

        @self.server.tool(name="mcp-watchlist-get-watchlist")
        def get_watchlist(watchlist_id: str) -> dict[str, Any]:
            self.calls.append(("get_watchlist", {"watchlist_id": watchlist_id}))
            return {"watchlist": copy.deepcopy(self.lists[watchlist_id])}

        @self.server.tool(name="mcp-watchlist-create-watchlist")
        def create_watchlist(name: str, symbols: list[str]) -> dict[str, Any]:
            self.calls.append(("create_watchlist", {"name": name, "symbols": symbols}))
            key = str(max((int(key) for key in self.lists), default=0) + 1)
            self.lists[key] = {
                "id": key,
                "name": name,
                "symbols": sorted(set(symbols) - self.reject),
            }
            row = copy.deepcopy(self.lists[key])
            if self.bad_create:
                row["name"] = "Unexpected list"
            return {"watchlist": row}

        @self.server.tool(name="mcp-watchlist-add-to-watchlist")
        def add_to_watchlist(watchlist_id: str, symbols: list[str]) -> dict[str, Any]:
            self.calls.append(
                ("add_to_watchlist", {"watchlist_id": watchlist_id, "symbols": symbols})
            )
            self.lists[watchlist_id]["symbols"].extend(sorted(set(symbols) - self.reject))
            self.lists[watchlist_id]["symbols"].extend(sorted(self.extras))
            if self.write_response is not None:
                return copy.deepcopy(self.write_response)
            if self.ambiguous:
                return {"unknown": "a write may have succeeded"}
            return {
                "watchlist_id": watchlist_id,
                "symbols": copy.deepcopy(self.lists[watchlist_id]["symbols"]),
            }

        @self.server.tool(name="mcp-watchlist-remove-from-watchlist")
        def remove_from_watchlist(watchlist_id: str, symbols: list[str]) -> dict[str, Any]:
            self.calls.append(
                ("remove_from_watchlist", {"watchlist_id": watchlist_id, "symbols": symbols})
            )
            if not (self.stubborn and set(symbols) & self.extras):
                self.lists[watchlist_id]["symbols"] = [
                    value for value in self.lists[watchlist_id]["symbols"] if value not in symbols
                ]
            if self.write_response is not None:
                return copy.deepcopy(self.write_response)
            if self.ambiguous:
                return {"unknown": "a write may have succeeded"}
            return {
                "watchlist_id": watchlist_id,
                "symbols": copy.deepcopy(self.lists[watchlist_id]["symbols"]),
            }


def run_sync(path: Path, peer: Peer, *, apply=True, clock=None):
    inputs, sync = api()
    data = inputs.read_research_inputs(path, now=NOW)

    async def run():
        error = None
        result = None
        async with Client(peer.server) as client:
            tools = sync.OfficialTools(client, interval=0, now=clock or (lambda: NOW))
            # SDK task groups wrap errors raised through __aexit__; test the
            # adapter's error after closing the real transport normally.
            try:
                result = await sync.sync_research(tools, data, apply=apply)
            except sync.SyncError as exc:
                error = exc
        if error:
            raise error
        assert result is not None
        return result

    return asyncio.run(run())


def test_membership_sync_preserves_survivor_order_and_owner_lists(tmp_path: Path):
    peer = Peer()
    result = run_sync(unit(tmp_path), peer)
    assert peer.lists["1"]["symbols"] == ["NASDAQ:MSFT", "NASDAQ:AAPL"]
    assert peer.lists["2"]["symbols"] == ["NASDAQ:MSFT"]
    assert peer.lists["3"]["symbols"] == ["NYSE:IBM"]
    assert result["applied"] is True
    assert result["readback"]["NS movers"]["stored"] == ["NASDAQ:AAPL", "NASDAQ:MSFT"]
    peer.calls.clear()
    again = run_sync(tmp_path, peer)
    assert again["operations"] == []
    assert [name for name, _ in peer.calls] == [
        "list_watchlists",
        "get_watchlist",
        "get_watchlist",
        "get_watchlist",
        "get_watchlist",
    ]


def test_watchlist_writes_accept_the_measured_shape(tmp_path: Path):
    peer = Peer()
    result = run_sync(unit(tmp_path), peer)
    assert result["applied"] is True
    names = [name for name, _ in peer.calls]
    assert names.count("add_to_watchlist") == 2
    assert names.count("remove_from_watchlist") == 1


@pytest.mark.parametrize(
    "response",
    [
        {"success": True},
        {"watchlist_id": "1", "symbols": ["NASDAQ:AAPL"], "note": "ok"},
        {"watchlist_id": 1, "symbols": ["NASDAQ:AAPL"]},
        {"watchlist_id": "1", "symbols": "NASDAQ:AAPL"},
        {"watchlist_id": "1"},
        {"removed": ["NASDAQ:MSFT"]},
        {"watchlist_id": "1", "symbols": ["NASDAQ:AAPL"], "SECRET-key": "SECRET-7f3a"},
    ],
    ids=[
        "success-only",
        "documented-plus-extra",
        "non-string-id",
        "non-list-symbols",
        "documented-subset",
        "unrelated-object",
        "secret-bearing-extra",
    ],
)
def test_write_shapes_wider_than_the_measured_shape_still_stop(tmp_path: Path, response):
    _, sync = api()
    peer = Peer()
    peer.write_response = response
    with pytest.raises(sync.SyncError) as error:
        run_sync(unit(tmp_path), peer)
    assert str(error.value) == sync.AMBIGUOUS_MUTATION
    assert error.value.stage == "apply"
    assert error.value.detail.startswith(("structured_content:", "content:"))
    assert "SECRET" not in error.value.detail
    assert "NASDAQ:AAPL" not in error.value.detail
    assert [name for name, _ in peer.calls].count("add_to_watchlist") == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            {"watchlist_id": "1", "symbols": ["NASDAQ:AAPL"]},
            "structured_content:object{watchlist_id,symbols}"
            "[watchlist_id:string|symbols:array[string]]",
        ),
        (
            {"watchlist_id": 1, "symbols": []},
            "structured_content:object{watchlist_id,symbols}[watchlist_id:number|symbols:array[]]",
        ),
        (
            {"watchlist_id": "1", "symbols": ["A", 2]},
            "structured_content:object{watchlist_id,symbols}"
            "[watchlist_id:string|symbols:array[mixed]]",
        ),
        (
            {"watchlist_id": "1", "symbols": "A"},
            "structured_content:object{watchlist_id,symbols}[watchlist_id:string|symbols:string]",
        ),
        (
            {"watchlist_id": "1", "symbols": ["A"], "note": "x"},
            "structured_content:object{watchlist_id,symbols,extra}"
            "[watchlist_id:string|symbols:array[string]]",
        ),
        (
            {"watchlist_id": "1"},
            "structured_content:object{watchlist_id}[watchlist_id:string]",
        ),
        (
            {"symbols": ["A"]},
            "structured_content:object{symbols}[symbols:array[string]]",
        ),
        ({"success": True}, "structured_content:object{success}"),
        ({"data": {}, "success": True}, "structured_content:object{data,success}"),
        ({"watchlist": {}, "note": "x"}, "structured_content:object{watchlist,note}"),
        ({"partial": True, "watchlists": []}, "structured_content:object{partial,watchlists}"),
        ({"watchlists": []}, "structured_content:object{watchlists}"),
        ({"result": {}}, "structured_content:object{result}"),
        ({"removed": ["A"]}, "structured_content:object{unknown}"),
        ({}, "structured_content:object{}"),
        ([], "structured_content:array"),
        ("A", "structured_content:string"),
        (7, "structured_content:number"),
        (True, "structured_content:boolean"),
        (None, "structured_content:null"),
    ],
)
def test_write_shape_names_only_a_fixed_class(value, expected):
    _, sync = api()
    assert sync._write_shape(value, "structured_content") == expected


def test_unparsed_write_content_names_only_its_class():
    _, sync = api()
    from mcp.types import CallToolResult, TextContent

    class Session:
        async def call_tool(self, *args, **kwargs):
            return CallToolResult(content=[TextContent(type="text", text="SECRET-9f is not JSON")])

    client = SimpleNamespace(session=Session())

    async def run():
        tools = sync.OfficialTools(client, interval=0)
        tools.schemas["add_to_watchlist"] = {"type": "object"}
        tools.wire["add_to_watchlist"] = "mcp-watchlist-add-to-watchlist"
        return await tools.call_raw(
            "add_to_watchlist", {"watchlist_id": "1", "symbols": ["NASDAQ:AAPL"]}
        )

    _, shape = asyncio.run(run())
    assert shape == "content:unparsed"
    assert "SECRET" not in shape


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "stale"),
        ("snapshot_status", "missing"),
        ("generated_at", "2026-11-06T14:31:00+00:00"),
        ("as_of_snapshot", "2026-11-06T14:00:00+00:00"),
        ("generated_at", "2026-11-06T14:29:30"),
        ("session_date", "2026-11-05"),
        ("generation", {"as_of": "2026-11-06T14:29:00+00:00", "sha256": "b" * 64}),
    ],
)
def test_invalid_or_mixed_unit_is_unavailable(tmp_path: Path, field, value):
    inputs, _ = api()
    unit(tmp_path)
    change(tmp_path, "focus.json", **{field: value})
    with pytest.raises(inputs.InputError):
        inputs.read_research_inputs(tmp_path, now=NOW)


def test_missing_display_and_empty_movers_never_become_a_cleanup(tmp_path: Path):
    inputs, _ = api()
    unit(tmp_path)
    (tmp_path / "display-lists.json").unlink()
    with pytest.raises(inputs.InputError):
        inputs.read_research_inputs(tmp_path, now=NOW)
    unit(tmp_path, movers=[])
    with pytest.raises(inputs.InputError):
        inputs.read_research_inputs(tmp_path, now=NOW)


def test_five_unmapped_omitted_six_refused_and_cross_list_venues_match(tmp_path: Path):
    inputs, _ = api()
    rows = [member("AAPL"), *[member(f"U{n}", None) for n in range(5)]]
    unit(tmp_path, movers=rows, focus=[])
    result = inputs.read_research_inputs(tmp_path, now=NOW)
    assert result.memberships["NS movers"] == frozenset({"NASDAQ:AAPL"})
    assert result.unmapped == frozenset({"U0", "U1", "U2", "U3", "U4"})
    unit(tmp_path, movers=[*rows, member("U5", None)], focus=[])
    with pytest.raises(inputs.InputError):
        inputs.read_research_inputs(tmp_path, now=NOW)
    unit(tmp_path, focus=[member("AAPL", "NYSE")])
    with pytest.raises(inputs.InputError):
        inputs.read_research_inputs(tmp_path, now=NOW)


def test_display_focus_may_include_producer_armed_names_outside_movers(tmp_path: Path):
    inputs, _ = api()
    result = inputs.read_research_inputs(unit(tmp_path, focus=[member("IBM", "NYSE")]), now=NOW)
    assert result.memberships["NS focus"] == frozenset({"NYSE:IBM"})


def test_rejections_are_reported_once_and_extras_get_one_correction(tmp_path: Path):
    peer = Peer()
    peer.reject = {"NASDAQ:AAPL"}
    peer.extras = {"NYSE:EXTRA"}
    result = run_sync(unit(tmp_path), peer)
    assert result["readback"]["NS movers"]["rejected"] == ["NASDAQ:AAPL"]
    assert result["readback"]["NS focus"]["corrective_removals"] == ["NYSE:EXTRA"]
    assert len([1 for name, _ in peer.calls if name == "add_to_watchlist"]) == 2
    assert peer.lists["2"]["symbols"] == ["NASDAQ:MSFT"]


@pytest.mark.parametrize(
    "failure",
    ["six_rejected", "stubborn", "ambiguous", "partial", "duplicate", "alias", "readonly"],
)
def test_refusal_stops_without_automatic_replay(tmp_path: Path, failure):
    _, sync = api()
    peer = Peer()
    path = unit(tmp_path)
    if failure == "six_rejected":
        rows = [member(f"U{n}") for n in range(6)]
        unit(path, movers=rows, focus=[])
        peer.reject = {f"NASDAQ:U{n}" for n in range(6)}
    elif failure == "stubborn":
        peer.extras, peer.stubborn = {"NYSE:EXTRA"}, True
    elif failure == "ambiguous":
        peer.ambiguous = True
    elif failure == "partial":
        peer.malformed = True
    elif failure == "duplicate":
        peer.lists["3"]["name"] = "NS movers"
    elif failure == "alias":
        peer.lists["3"]["id"] = "1"
    else:
        peer.lists["2"]["read_only"] = True
    with pytest.raises(sync.SyncError):
        run_sync(path, peer)
    if failure in {"partial", "duplicate", "alias", "readonly"}:
        assert all(name in {"list_watchlists", "get_watchlist"} for name, _ in peer.calls)
    if failure == "ambiguous":
        assert [name for name, _ in peer.calls].count("add_to_watchlist") == 1


def test_create_readback_and_dry_run_do_not_claim_same_success(tmp_path: Path):
    peer = Peer()
    del peer.lists["2"]
    result = run_sync(unit(tmp_path), peer, apply=False)
    assert result["applied"] is False and result["readback"] == {}
    assert all(name in {"list_watchlists", "get_watchlist"} for name, _ in peer.calls)
    result = run_sync(tmp_path, peer)
    assert result["readback"]["NS focus"]["stored"] == ["NASDAQ:MSFT"]


def test_expiry_before_write_and_non_display_tool_refused(tmp_path: Path):
    _, sync = api()
    peer = Peer()
    with pytest.raises(sync.SyncError):
        run_sync(unit(tmp_path), peer, clock=lambda: NOW + timedelta(minutes=30))
    assert peer.lists["1"]["symbols"] == ["NASDAQ:MSFT", "NYSE:IBM"]

    async def run():
        async with Client(peer.server) as client:
            tools = sync.OfficialTools(client, interval=0, now=lambda: NOW)
            await tools.discover()
            with pytest.raises(sync.SyncError):
                await tools.call("place_order", {})

    asyncio.run(run())


@pytest.mark.parametrize("namespaced", [False, True])
def test_official_discovery_ignores_every_non_membership_tool(
    monkeypatch: pytest.MonkeyPatch, namespaced: bool
):
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, Tool

    _, sync = api()
    documented = (
        "list_watchlists",
        "get_watchlist",
        "get_active_watchlist",
        "create_watchlist",
        "delete_watchlist",
        "add_to_watchlist",
        "remove_from_watchlist",
        "update_watchlist",
        "get_ohlcv",
        "get_economic_data",
        "get_economic_symbols",
        "search_symbols",
        "get_symbol_data",
        "run_screener",
        "get_symbol_data_batch",
        "get_screener_columns",
        "get_technicals_rating",
        "get_news",
        "get_news_story",
        "get_forecasts",
        "get_financials",
        "get_financial_history",
        "get_documents",
        "get_document_view",
        "get_earnings_calendar",
        "get_economic_calendar",
        "get_dividends_calendar",
        "create_alert",
        "update_alert",
        "delete_alert",
        "stop_alerts",
        "restart_alerts",
        "list_alerts",
        "get_alerts",
        "get_alerts_log",
    )
    membership = {
        "list_watchlists": {},
        "get_watchlist": {"watchlist_id": "1"},
        "create_watchlist": {"name": "NS movers", "symbols": []},
        "add_to_watchlist": {"watchlist_id": "1", "symbols": []},
        "remove_from_watchlist": {"watchlist_id": "1", "symbols": []},
    }
    wire = {
        name: "mcp-watchlist-" + name.replace("_", "-") if namespaced else name
        for name in membership
    }
    refused = (set(documented) - membership.keys()) | {
        "place_order",
        "mcp-watchlist-delete-watchlist",
        "mcp-watchlist-update-watchlist",
        "mcp-watchlist-get-active-watchlist",
    }
    requests = []

    def deny_connection(*args, **kwargs):
        pytest.fail("The official MCP inventory proof must not open a socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_connection)
    monkeypatch.setattr(socket, "create_connection", deny_connection)

    async def list_tools(_ctx, _params):
        requests.append("tools/list")
        return ListToolsResult(
            tools=[
                Tool(
                    name=wire.get(name, name),
                    input_schema={"type": "object", "additionalProperties": True},
                )
                for name in (*documented, "place_order")
            ]
        )

    async def call_tool(_ctx, params):
        requests.append(params.name)
        assert params.name == wire["list_watchlists"] and params.arguments == {}
        return CallToolResult(content=[], structured_content={"watchlists": []})

    server = Server(
        "2026-11-09 official inventory", on_list_tools=list_tools, on_call_tool=call_tool
    )

    async def run():
        async with Client(server) as client:
            tools = sync.OfficialTools(client, interval=0)
            await tools.discover()
            assert tools.schemas.keys() == membership.keys()
            assert tools.wire == wire
            for name, arguments in membership.items():
                tools.validate(name, arguments)
            for name in sorted(refused):
                with pytest.raises(sync.SyncError, match="Non-display operation"):
                    tools.validate(name, {})
                with pytest.raises(sync.SyncError, match="Non-display operation"):
                    await tools.call(name, {})
            assert requests == ["tools/list"]
            assert await tools.call("list_watchlists", {}) == {"watchlists": []}

    assert len(documented) == len(set(documented)) == 35
    asyncio.run(run())
    assert requests == ["tools/list", wire["list_watchlists"]]


def test_three_discovery_pages_and_first_tool_share_the_limiter(monkeypatch: pytest.MonkeyPatch):
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, Tool

    _, sync = api()
    current = 10.0
    requests = []
    names = list(sync.WIRE)

    async def list_tools(_ctx, params):
        cursor = params.cursor if params else None
        page = {None: 0, "second": 1, "third": 2}[cursor]
        requests.append(("discovery", current))
        return ListToolsResult(
            tools=[
                Tool(name=name, input_schema={"type": "object"})
                for name in names[2 * page : 2 * page + 2]
            ],
            next_cursor=["second", "third", None][page],
        )

    async def call_tool(_ctx, params):
        assert params.name == "mcp-watchlist-list-watchlists" and params.arguments == {}
        requests.append(("tool", current))
        return CallToolResult(content=[], structured_content={"watchlists": []})

    async def sleep(seconds):
        nonlocal current
        current += seconds

    from sextant.tradingview import account

    monkeypatch.setattr(
        account,
        "_utcnow",
        lambda: datetime(2026, 11, 11, 15, tzinfo=UTC) + timedelta(seconds=current),
    )
    monkeypatch.setattr(account, "_sleep", sleep)
    server = Server("three-page fixture", on_list_tools=list_tools, on_call_tool=call_tool)

    async def run():
        async with Client(server) as client:
            tools = sync.OfficialTools(client)
            await tools.discover()
            return await tools.call("list_watchlists", {})

    assert asyncio.run(run()) == {"watchlists": []}
    assert requests == [
        ("discovery", 10.0),
        ("discovery", 12.0),
        ("discovery", 14.0),
        ("tool", 16.0),
    ]


def test_expiry_is_checked_after_pacing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    inputs, sync = api()
    data = inputs.read_research_inputs(unit(tmp_path), now=NOW)
    peer = Peer()
    current = NOW

    async def sleep(_seconds):
        nonlocal current
        current = NOW + timedelta(minutes=30)

    async def run():
        async with Client(peer.server) as client:
            tools = sync.OfficialTools(client, now=lambda: current)
            await tools.discover()
            from sextant.tradingview import account

            monkeypatch.setattr(account, "_sleep", sleep)
            with pytest.raises(sync.SyncError):
                await tools.call(
                    "add_to_watchlist",
                    {"watchlist_id": "1", "symbols": ["NASDAQ:AAPL"]},
                    inputs=data,
                )

    asyncio.run(run())
    assert peer.calls == []


def test_all_operation_schemas_are_preflighted_before_first_write(tmp_path: Path):
    _, sync = api()
    peer = Peer()
    peer.server.remove_tool("mcp-watchlist-remove-from-watchlist")

    @peer.server.tool(name="mcp-watchlist-remove-from-watchlist")
    def incompatible_remove(watchlist_id: str, symbols: list[str], extra_required: str) -> dict:
        pytest.fail("An incompatible mutation must never reach the peer")

    with pytest.raises(sync.SyncError):
        run_sync(unit(tmp_path), peer)
    assert peer.lists["1"]["symbols"] == ["NASDAQ:MSFT", "NYSE:IBM"]
    assert all(name in {"list_watchlists", "get_watchlist"} for name, _ in peer.calls)


def test_presets_have_immutable_ids_and_detect_modified_source(tmp_path: Path):
    try:
        presets = importlib.import_module("sextant.tradingview.presets")
    except ModuleNotFoundError:
        pytest.fail("The dated Pine preset package is not implemented")
    root = Path(__file__).parents[2] / "lanes/tradingview/presets"
    result = presets.preset_receipt(root)
    assert {row["script_id"] for row in result} == {
        "NS 2026-11-06 · ema_cross",
        "NS 2026-11-06 · hurst_vpin",
        "NS 2026-11-06 · quote_imbalance",
        "NS 2026-11-06 · exit_levels",
    }
    manifest = json.loads((root / "manifest.json").read_text())
    for item in manifest:
        path = root / item["file"]
        text = path.read_text()
        # Formula bytes are the pinned upstream artifact, with only its title changed.
        normalized = text.replace(item["script_id"], item["upstream_title"], 1)
        assert hashlib.sha256(normalized.encode()).hexdigest() == item["upstream_sha256"]
        (tmp_path / item["file"]).write_text(text)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "exit_levels.pine").write_text("tampered")
    with pytest.raises(ValueError):
        presets.preset_receipt(tmp_path)


@pytest.mark.parametrize("corruption", ["upstream_sha256", "upstream_title", "packaged_formula"])
def test_preset_receipt_refuses_mismatched_upstream_provenance(tmp_path: Path, corruption: str):
    presets = importlib.import_module("sextant.tradingview.presets")
    root = Path(__file__).parents[2] / "lanes/tradingview/presets"
    manifest = json.loads((root / "manifest.json").read_text())
    for item in manifest:
        (tmp_path / item["file"]).write_bytes((root / item["file"]).read_bytes())
    if corruption == "packaged_formula":
        path = tmp_path / manifest[0]["file"]
        source = path.read_bytes() + b"\n// unreviewed change\n"
        path.write_bytes(source)
        manifest[0]["sha256"] = hashlib.sha256(source).hexdigest()
    else:
        manifest[0][corruption] = "0" * 64 if corruption == "upstream_sha256" else "Wrong title"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        presets.preset_receipt(tmp_path)


def test_preset_roster_keeps_managed_tickets_plain_limit():
    """The roster is the owner-manual ticket contract, never a machine order path."""
    root = Path(__file__).parents[2]
    docs = (root / "docs/tradingview.md").read_text()
    rows = [line for line in docs.splitlines() if line.startswith("| `NS ")]
    assert len(rows) == 3, f"the documented roster is not the three named presets: {rows}"
    by_name = {
        row.split("|")[1].strip().strip("`"): [cell.strip() for cell in row.split("|")[2:-1]]
        for row in rows
    }
    assert set(by_name) == {
        "NS ALPACA DAY LIMIT MANAGED",
        "NS ALPACA DAY LIMIT BRACKET",
        "NS IBKR DAY LIMIT BRACKET",
    }
    managed = by_name["NS ALPACA DAY LIMIT MANAGED"]
    assert managed[:2] == ["LIMIT", "0 relative ticks (at the touch, not a fixed price)"]
    assert managed[2:] == ["DAY", "none", "ON", "managed-paper entries"]
    for name in ("NS ALPACA DAY LIMIT BRACKET", "NS IBKR DAY LIMIT BRACKET"):
        bracket = by_name[name]
        assert bracket[0] == "LIMIT"
        assert bracket[2] == "DAY"
        assert bracket[3] == "TP/SL"
        assert bracket[5] == "owner-managed positions only"
    assert "TV presets can place brackets without confirmation" in docs
    assert "managed-paper entries stay plain LIMIT, never a bracket" in docs
    assert "at the touch, not a fixed price" in docs
    assert "20:00\u201304:00 ET" in docs
    assert "20:00 ET if unfilled" in docs
    assert "extended_hours" in docs and "false/omitted" in docs
    presets = root / "lanes/tradingview/presets"
    for pine in sorted(presets.glob("*.pine")):
        source = pine.read_text()
        for forbidden in ("strategy(", "alert(", "alertcondition("):
            assert forbidden not in source, f"{pine.name} carries an order path: {forbidden}"
    for path in sorted((root / "lanes/tradingview").rglob("*")):
        if path.is_file() and path.suffix != ".pine":
            text = path.read_text(errors="ignore").lower()
            assert '"webhook"' not in text and "autotrade" not in text
            assert "hooks.slack" not in text and "hooks.discord" not in text


def test_preset_import_card_matches_the_reviewed_manifest_hashes():
    """The owner import card must match the pinned preset manifest exactly."""
    root = Path(__file__).parents[2]
    docs = (root / "docs/tradingview.md").read_text()
    assert "## Owner import card" in docs
    card = docs.split("## Owner import card", 1)[1].split("\n## ", 1)[0]
    presets = root / "lanes/tradingview/presets"
    manifest = json.loads((presets / "manifest.json").read_text())
    assert len(manifest) == 4
    for item in manifest:
        assert f"`{item['file']}`" in card
        assert f"`{item['script_id']}`" in card
        assert item["sha256"] in card
    for phrase in (
        "NEW",
        "paste",
        "add it to the owner's chart",
        "No install automation",
        "lanes/tradingview/presets/manifest.json",
    ):
        assert phrase in card, f"import card is missing {phrase!r}"


def test_oauth_requires_explicit_owner_flag_before_any_transport():
    try:
        auth = importlib.import_module("sextant.tradingview.auth")
    except ModuleNotFoundError:
        pytest.fail("RAM-only owner-consented SDK OAuth is not implemented")
    _, sync = api()

    async def run():
        with pytest.raises(sync.SyncError, match="owner"):
            async with auth.official_client():
                pytest.fail("No session is allowed without owner browser consent")

    asyncio.run(run())


def test_cli_invalid_input_never_opens_oauth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    try:
        cli = importlib.import_module("sextant.tradingview.__main__")
    except ModuleNotFoundError:
        pytest.fail("The bounded TradingView CLI is not implemented")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid input must stop before OAuth")

    monkeypatch.setattr(cli, "official_client", forbidden)
    assert cli.main(["--research-dir", str(tmp_path)]) == 2


@pytest.mark.parametrize("manifest", [None, "RAW RECEIPT ERROR", "[]"])
def test_cli_preset_failures_report_only_offline_receipt_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, manifest
):
    cli = importlib.import_module("sextant.tradingview.__main__")
    if manifest is not None:
        (tmp_path / "manifest.json").write_text(manifest)

    def forbidden(*args, **kwargs):
        pytest.fail("Offline receipts must never open OAuth or read research")

    monkeypatch.setattr(cli, "official_client", forbidden)
    monkeypatch.setattr(cli, "read_research_inputs", forbidden)
    assert cli.main(["--presets", str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert "receipt" in output.out.lower()
    assert "no session opened" in output.out.lower()
    assert "earlier writes" not in output.out.lower()
    assert "RAW RECEIPT ERROR" not in output.out
    assert output.err == ""


def test_cli_preset_success_is_a_local_receipt_without_installation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    cli = importlib.import_module("sextant.tradingview.__main__")

    def forbidden(*args, **kwargs):
        pytest.fail("Offline receipts must never open OAuth or read research")

    monkeypatch.setattr(cli, "official_client", forbidden)
    monkeypatch.setattr(cli, "read_research_inputs", forbidden)
    root = Path(__file__).parents[2] / "lanes/tradingview/presets"
    assert cli.main(["--presets", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["installed"] is False
    assert len(result["artifacts"]) == 4


def test_nanosecond_clock_disagreement_refuses_the_generation(tmp_path: Path):
    inputs, _ = api()
    unit(tmp_path)
    change(tmp_path, "focus.json", generated_at="2026-11-06T14:29:30.000000001+00:00")
    with pytest.raises(inputs.InputError):
        inputs.read_research_inputs(tmp_path, now=NOW)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1970-01-01T00:00:00Z", 0),
        ("1969-12-31T23:59:59.999999999Z", -1),
        ("1970-01-01T00:00:00.000000001+0000", 1),
        ("1970-01-01T05:30:00+05:30", 0),
        ("1970-01-01T00:00:00+00:59", -3_540_000_000_000),
        ("1970-01-01 00:00:00-0059", 3_540_000_000_000),
        ("1970-01-01T00:00:00+2359", -86_340_000_000_000),
        ("1970-01-01T00:00:00-23:59", 86_340_000_000_000),
        ("2026-11-06T14:29:30.123456789Z", 1_793_975_370_123_456_789),
        ("2026-11-06T09:29:30.123456789-05:00", 1_793_975_370_123_456_789),
        (datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC), 1_000),
        (datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), -1_000),
        (datetime(1970, 1, 1, tzinfo=timezone(timedelta(microseconds=1))), -1_000_000_000),
        # The engine's exact pair keeps a Timestamp's nanoseconds (was 0: the
        # drop that refused about half the producer's ticks, 2026-11-11).
        (pd.Timestamp("1970-01-01T00:00:00.000000123Z"), 123),
        ("0001-01-01T00:00:00Z", -62_135_596_800_000_000_000),
        ("9999-12-31T23:59:59.999999Z", 253_402_300_799_999_999_000),
    ],
)
def test_native_clock_has_zero_difference_from_reviewed_witnesses(value, expected):
    # Literal outputs captured before replacing the bridge; a 1 ns rounding,
    # offset, or public-parser range change must fail these consumer witnesses.
    inputs, _ = api()
    assert inputs._ns(value) == expected
    assert inputs.timestamp(value) == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=expected // 1000
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        0,
        "2026-11-06T14:30:00",
        "2026-11-06T14:30:00.1234567890Z",
        "1970-01-01T00:00:00.123456789+00:60",
        "1970-01-01 00:00:00.123456789-00:60",
        "1970-01-01T00:00:00+0060",
        "1970-01-01T00:00:00-0060",
        "1970-01-01T00:00:00+24:00",
        "1970-01-01T00:00:00-24:00",
        "1970-01-01T00:00:00+2400",
        "1970-01-01T00:00:00-2400",
        NOW.replace(tzinfo=None),
    ],
)
def test_native_clock_keeps_explicit_timezone_and_wire_precision_requirements(value):
    inputs, _ = api()
    with pytest.raises(inputs.InputError):
        inputs._ns(value)


@pytest.mark.parametrize("year", [1, 9999])
def test_unsupported_calendar_bounds_refuse_research_as_input_error(tmp_path: Path, year: int):
    inputs, _ = api()
    unit(tmp_path)
    now = datetime(year, 6, 15, 14, 30, tzinfo=UTC)
    for name in ("top100.json", "focus.json", "display-lists.json"):
        change(
            tmp_path,
            name,
            as_of_snapshot=now.isoformat(),
            as_of=now.isoformat(),
            generated_at=now.isoformat(),
        )
    with pytest.raises(inputs.InputError, match="No supported XNYS trading date for this input"):
        inputs.read_research_inputs(tmp_path, now=now)


def test_five_readback_rejections_are_tolerated_across_the_pair(tmp_path: Path):
    peer = Peer()
    rows = [member("AAPL"), *[member(f"U{n}") for n in range(5)]]
    peer.reject = {f"NASDAQ:U{n}" for n in range(5)}
    result = run_sync(unit(tmp_path, movers=rows, focus=[member("U0")]), peer)
    assert result["applied"] is True
    assert result["readback"]["NS movers"]["rejected"] == [
        "NASDAQ:U0",
        "NASDAQ:U1",
        "NASDAQ:U2",
        "NASDAQ:U3",
        "NASDAQ:U4",
    ]
    assert result["readback"]["NS focus"]["rejected"] == ["NASDAQ:U0"]


def test_sunday_overnight_unit_uses_monday_xnys_date(tmp_path: Path):
    inputs, _ = api()
    unit(tmp_path)
    clocks = {
        "as_of_snapshot": "2026-11-09T01:29:00+00:00",
        "as_of": "2026-11-09T01:29:10+00:00",
        "generated_at": "2026-11-09T01:29:30+00:00",
    }
    generation = {"as_of": clocks["as_of_snapshot"], "sha256": "a" * 64}
    context = {
        "session": "overnight",
        "session_date": "2026-11-09",
        "intraday_feed": "boats",
        "daily_history_feed": "sip",
        "daily_history_scope": "prior completed XNYS sessions; not BOATS intraday evidence",
    }
    for name in ("top100.json", "focus.json", "display-lists.json"):
        change(
            tmp_path,
            name,
            **clocks,
            session_date="2026-11-09",
            generation=generation,
            ranking_context=context,
            research_inputs={},
        )
    change(tmp_path, "display-lists.json", sources={"snapshot": generation, "armed_sets": []})
    data = inputs.read_research_inputs(tmp_path, now=datetime(2026, 11, 9, 1, 30, tzinfo=UTC))
    assert data.memberships["NS movers"] == frozenset({"NASDAQ:AAPL", "NASDAQ:MSFT"})


@pytest.fixture
def offline_owner_session(monkeypatch: pytest.MonkeyPatch):
    """Replace only callback/HTTP transport; keep the SDK Client task groups real."""
    from sextant.tradingview import auth

    class Callback:
        uri = "http://127.0.0.1:1/callback"
        result = None
        server = SimpleNamespace(server_close=lambda: None)

        async def redirect(self, _url):
            pytest.fail("No browser is authorized in this fixture")

        async def wait(self):
            pytest.fail("No OAuth callback is authorized in this fixture")

    monkeypatch.setattr(auth, "OwnerCallback", Callback)

    def connect(server):
        monkeypatch.setattr(auth, "streamable_http_client", lambda *args, **kwargs: server)
        return auth.official_client(authorize=True)

    return connect


@pytest.mark.parametrize(
    "failure,message",
    [("stubborn", "Readback mismatch"), ("ambiguous", "Ambiguous watchlist mutation")],
)
def test_owner_session_preserves_trusted_sync_errors(
    tmp_path: Path, offline_owner_session, failure, message
):
    inputs, sync = api()
    data = inputs.read_research_inputs(unit(tmp_path), now=NOW)
    peer = Peer()
    if failure == "stubborn":
        peer.extras, peer.stubborn = {"NYSE:EXTRA"}, True
    else:
        peer.ambiguous = True
    original = None

    async def run():
        nonlocal original
        with pytest.raises(sync.SyncError, match=message) as error:
            async with offline_owner_session(peer.server) as client:
                try:
                    await sync.sync_research(
                        sync.OfficialTools(client, interval=0, now=lambda: NOW), data, apply=True
                    )
                except sync.SyncError as exc:
                    original = exc
                    raise
        assert error.value is original

    asyncio.run(run())


@pytest.mark.parametrize("initial", [0, 55])
@pytest.mark.parametrize("first_exit", [0, 1])
@pytest.mark.parametrize("cancel_first", [False, True])
def test_overlapping_owner_sessions_restore_logging_only_after_last_exit(
    offline_owner_session, initial, first_exit, cancel_first, caplog
):
    import logging

    entered = [asyncio.Event(), asyncio.Event()]
    leave = [asyncio.Event(), asyncio.Event()]

    async def session(index):
        async with offline_owner_session(Peer().server):
            entered[index].set()
            await leave[index].wait()

    async def run():
        tasks = []
        try:
            for index in range(2):
                tasks.append(asyncio.create_task(session(index)))
                await entered[index].wait()
            assert logging.root.manager.disable == max(initial, logging.CRITICAL)
            if cancel_first:
                tasks[first_exit].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[first_exit]
            else:
                leave[first_exit].set()
                await tasks[first_exit]
            assert logging.root.manager.disable == max(initial, logging.CRITICAL)
            logging.critical("overlapping session diagnostic must stay private")
            leave[1 - first_exit].set()
            await tasks[1 - first_exit]
            assert logging.root.manager.disable == initial
        finally:
            for event in leave:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    previous = logging.root.manager.disable
    try:
        logging.disable(initial)
        asyncio.run(run())
        assert "overlapping session diagnostic" not in caplog.text
    finally:
        logging.disable(previous)


def test_owner_session_failure_never_emits_upstream_diagnostics(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import logging
    from types import SimpleNamespace

    from sextant.tradingview import auth
    from sextant.tradingview.sync import SyncError

    class Callback:
        uri = "http://127.0.0.1:1/callback"
        result = None
        server = SimpleNamespace(server_close=lambda: None)

        async def redirect(self, _url):
            pytest.fail("A browser is not needed in this failure fixture")

        async def wait(self):
            pytest.fail("A callback is not needed in this failure fixture")

    def refuse_transport(*args, **kwargs):
        logging.critical("fixture diagnostic must stay private")
        raise RuntimeError("fixture diagnostic must stay private")

    previous = logging.root.manager.disable
    monkeypatch.setattr(auth, "OwnerCallback", Callback)
    monkeypatch.setattr(auth, "streamable_http_client", refuse_transport)

    async def run():
        with pytest.raises(SyncError) as error:
            async with auth.official_client(authorize=True):
                pytest.fail("Fixture transport always refuses")
        assert error.value.__context__ is None
        assert error.value.__cause__ is None

    asyncio.run(run())
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert logging.root.manager.disable == previous


def test_owner_session_names_the_safe_class_and_stage_of_an_oauth_failure(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import asyncio
    from types import SimpleNamespace

    from mcp.client.auth.exceptions import OAuthTokenError

    from sextant.tradingview import auth
    from sextant.tradingview.sync import SyncError

    class Callback:
        uri = "http://127.0.0.1:1/callback"
        result = None
        server = SimpleNamespace(server_close=lambda: None)

        async def redirect(self, _url):
            pytest.fail("A browser is not needed in this failure fixture")

        async def wait(self):
            pytest.fail("A callback is not needed in this failure fixture")

    def refuse_transport(*args, **kwargs):
        raise OAuthTokenError(
            "Token exchange failed (400): access_token=SECRET https://example.invalid/?state=x"
        )

    monkeypatch.setattr(auth, "OwnerCallback", Callback)
    monkeypatch.setattr(auth, "streamable_http_client", refuse_transport)

    async def run():
        with pytest.raises(SyncError) as error:
            async with auth.official_client(authorize=True):
                pytest.fail("Fixture transport always refuses")
        assert error.value.label == "OAuthTokenError"
        assert error.value.stage == "token"
        assert error.value.detail == "HTTP 400 at token endpoint"
        assert "SECRET" not in str(error.value) and "example.invalid" not in str(error.value)
        assert error.value.__context__ is None
        assert error.value.__cause__ is None

    asyncio.run(run())
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_owner_callback_names_authorize_and_callback_refusals(monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from sextant.tradingview import auth
    from sextant.tradingview.sync import SyncError

    monkeypatch.setattr(auth.webbrowser, "open", lambda _url: False)
    callback = auth.OwnerCallback()
    try:
        with pytest.raises(SyncError) as error:
            asyncio.run(callback.redirect("https://www.tradingview.com/mcp/oauth/authorize?ok=1"))
        assert error.value.stage == "authorize"
    finally:
        callback.server.server_close()

    callback = auth.OwnerCallback()
    try:
        callback.failed = True
        with pytest.raises(SyncError) as error:
            asyncio.run(callback.wait())
        assert error.value.stage == "callback"
    finally:
        callback.server.server_close()


def test_cli_stop_names_only_the_safe_class_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    cli = importlib.import_module("sextant.tradingview.__main__")
    from sextant.tradingview.sync import SyncError

    def refuse(*args, **kwargs):
        raise SyncError(
            "TradingView session stopped; re-inventory before retry.",
            label="OAuthFlowError",
            stage="callback",
        )

    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "official_client", refuse)
    assert cli.main(["--research-dir", str(tmp_path), "--authorize"]) == 2
    output = capsys.readouterr()
    assert "Safe diagnostic: OAuthFlowError at callback." in output.out
    assert "SECRET" not in output.out and "https://" not in output.out and output.err == ""
    assert "earlier writes" in output.out


def test_tool_discovery_names_the_tool_list_stage():
    import asyncio
    from types import SimpleNamespace

    from sextant.tradingview import sync

    class Silent:
        async def list_tools(self, *, cursor=None, cache_mode=None):
            return SimpleNamespace(
                tools=[SimpleNamespace(name="mcp-watchlist-list-watchlists", input_schema={})],
                next_cursor=None,
            )

    tools = sync.OfficialTools(Silent(), interval=0)  # type: ignore[arg-type]
    with pytest.raises(sync.SyncError) as error:
        asyncio.run(tools.discover())
    assert error.value.stage == "tool list"


def test_diagnostics_keeps_a_trusted_sync_stage_through_a_group():
    from sextant.tradingview import diagnostics, sync

    error = ExceptionGroup("sdk", [sync.SyncError("stop", stage="callback")])
    assert diagnostics.stage(error) == "callback"
    assert diagnostics.leaf_label(error) == "SyncError"


def test_diagnostics_names_http_status_and_endpoint_class():
    import httpx2

    from sextant.tradingview import diagnostics

    def status_error(status, path):
        request = httpx2.Request("POST", f"https://mcp.tradingview.com{path}")
        return httpx2.HTTPStatusError(
            "refused", request=request, response=httpx2.Response(status, request=request)
        )

    assert diagnostics.detail(status_error(400, "/mcp/oauth/token")) == "HTTP 400 at token endpoint"
    assert diagnostics.detail(status_error(401, "/mcp")) == "HTTP 401 at session endpoint"
    assert diagnostics.detail(ExceptionGroup("sdk", [status_error(503, "/mcp")])) == (
        "HTTP 503 at session endpoint"
    )


def test_diagnostics_names_only_the_oauth_error_vocabulary():
    from mcp.client.auth.exceptions import OAuthTokenError

    from sextant.tradingview import diagnostics

    known = OAuthTokenError(
        'Token exchange failed (400): {"error": "invalid_grant", "error_description": "SECRET"}'
    )
    assert diagnostics.detail(known) == "HTTP 400 at token endpoint; OAuth error invalid_grant"
    assert "SECRET" not in diagnostics.detail(known)
    unknown = OAuthTokenError('Token exchange failed (401): {"error": "made_up_code"}')
    assert "made_up_code" not in diagnostics.detail(unknown)
    assert "OAuth error" not in diagnostics.detail(unknown)


def test_diagnostics_names_pydantic_field_names_only():
    from pydantic import BaseModel, ValidationError

    from sextant.tradingview import diagnostics

    class Token(BaseModel):
        access_token: str
        token_type: str

    with pytest.raises(ValidationError) as error:
        Token.model_validate({"detail": "SECRET"})
    result = diagnostics.detail(error.value)
    assert result == "fields access_token, token_type"
    assert "SECRET" not in result


def test_tool_discovery_reports_the_name_diff():
    import asyncio
    from types import SimpleNamespace

    from sextant.tradingview import sync

    class Drifting:
        async def list_tools(self, *, cursor=None, cache_mode=None):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="mcp-watchlist-list-watchlists", input_schema={}),
                    SimpleNamespace(name="mcp-watchlist-delete-watchlist", input_schema={}),
                ],
                next_cursor=None,
            )

    tools = sync.OfficialTools(Drifting(), interval=0)  # type: ignore[arg-type]
    with pytest.raises(sync.SyncError) as error:
        asyncio.run(tools.discover())
    assert error.value.stage == "tool list"
    assert "mcp-watchlist-delete-watchlist" in error.value.detail
    for name in (
        "mcp-watchlist-get-watchlist",
        "mcp-watchlist-create-watchlist",
        "mcp-watchlist-add-to-watchlist",
        "mcp-watchlist-remove-from-watchlist",
    ):
        assert name in error.value.detail


def test_cli_stop_prints_a_fixed_vocabulary_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    cli = importlib.import_module("sextant.tradingview.__main__")
    from sextant.tradingview.sync import SyncError

    def refuse(*args, **kwargs):
        raise SyncError(
            "TradingView session stopped; re-inventory before retry.",
            label="OAuthTokenError",
            stage="token",
            detail="HTTP 400 at token endpoint; OAuth error invalid_grant",
        )

    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "official_client", refuse)
    assert cli.main(["--research-dir", str(tmp_path), "--authorize"]) == 2
    output = capsys.readouterr()
    assert (
        "Safe diagnostic: OAuthTokenError at token "
        "(HTTP 400 at token endpoint; OAuth error invalid_grant)." in output.out
    )
    assert "SECRET" not in output.out and "https://" not in output.out


def test_cli_prints_each_fixed_sync_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    _, sync = api()
    cli = importlib.import_module("sextant.tradingview.__main__")
    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    for message in sync.SAFE_STOP_MESSAGES:

        def refuse(*args, _message=message, **kwargs):
            raise sync.SyncError(_message, stage="tool list")

        monkeypatch.setattr(cli, "official_client", refuse)
        assert cli.main(["--research-dir", str(tmp_path), "--authorize"]) == 2
        output = capsys.readouterr()
        assert f"Safe diagnostic: SyncError at tool list: {message}" in output.out
        # A stored-login stop also pages its fixed owner step; nothing else reaches stderr.
        assert output.err == (f"{message}\n" if message in sync.LOGIN_STOPS else "")


def test_cli_withholds_a_message_outside_the_fixed_sync_literals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    cli = importlib.import_module("sextant.tradingview.__main__")
    from sextant.tradingview.sync import SyncError

    def refuse(*args, **kwargs):
        raise SyncError(
            "TradingView session stopped; SECRET-7f3a",
            label="OAuthFlowError",
            stage="callback",
        )

    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "official_client", refuse)
    assert cli.main(["--research-dir", str(tmp_path), "--authorize"]) == 2
    output = capsys.readouterr()
    assert "Safe diagnostic: OAuthFlowError at callback." in output.out
    assert "TradingView session stopped" not in output.out
    assert "SECRET-7f3a" not in output.out
    assert output.err == ""


def test_cli_names_only_the_shape_class_for_an_unaccepted_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    inputs, sync = api()
    cli = importlib.import_module("sextant.tradingview.__main__")
    peer = Peer()
    peer.write_response = {
        "watchlist_id": "SECRET-77",
        "symbols": ["NASDAQ:SECRET-AAPL"],
        "TOKEN-9": "SECRET-BODY",
    }
    data = inputs.ResearchInputs(
        memberships={"NS movers": frozenset({"NASDAQ:AAPL"}), "NS focus": frozenset()},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        unmapped=frozenset(),
        generation={},
    )
    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: data)
    monkeypatch.setattr(cli, "OfficialTools", lambda client: sync.OfficialTools(client, interval=0))

    @asynccontextmanager
    async def session(*args, **kwargs):
        async with Client(peer.server) as client:
            yield client

    monkeypatch.setattr(cli, "official_client", session)
    assert cli.main(["--research-dir", str(tmp_path), "--authorize", "--apply"]) == 2
    output = capsys.readouterr().out
    assert "Ambiguous watchlist mutation" in output
    assert "object{watchlist_id,symbols,extra}" in output
    assert "SECRET" not in output
    assert "NASDAQ" not in output
    assert [name for name, _ in peer.calls].count("add_to_watchlist") == 1


def test_envelope_refusals_name_only_the_json_shape():
    _, sync = api()

    class Inventory:
        async def call(self, name, arguments):
            return {"watchlists": 7}

    with pytest.raises(sync.SyncError) as error:
        asyncio.run(sync._inventory(Inventory()))
    assert str(error.value) == sync.UNKNOWN_INVENTORY_ENVELOPE
    assert error.value.detail == "JSON number"

    class Membership:
        async def call(self, name, arguments):
            return {"name": "NS movers", "id": "1", "symbols": 7}

    with pytest.raises(sync.SyncError) as error:
        asyncio.run(sync._membership(Membership(), "NS movers", "1"))
    assert str(error.value) == sync.UNKNOWN_MEMBERSHIP_ENVELOPE
    assert error.value.detail == "JSON number"

    with pytest.raises(sync.SyncError) as error:
        sync._unwrap({"result": {"stored": True, "has_more": True}})
    assert str(error.value) == sync.INCOMPLETE_RESPONSE
    assert error.value.detail == "JSON object, 2 keys"


def test_tool_result_error_names_public_tool_and_one_fixed_word():
    _, sync = api()
    from mcp.types import CallToolResult, TextContent

    class Session:
        async def call_tool(self, *args, **kwargs):
            return CallToolResult(
                content=[TextContent(type="text", text="Quota exceeded for this SECRET-9 account")],
                is_error=True,
            )

    client = SimpleNamespace(session=Session())

    async def run():
        tools = sync.OfficialTools(client, interval=0)
        tools.schemas["add_to_watchlist"] = {"type": "object"}
        tools.wire["add_to_watchlist"] = "mcp-watchlist-add-to-watchlist"
        with pytest.raises(sync.SyncError) as error:
            await tools.call("add_to_watchlist", {"watchlist_id": "1", "symbols": ["NASDAQ:AAPL"]})
        return error.value

    error = asyncio.run(run())
    assert str(error) == sync.CALL_FAILED
    assert error.detail == "tool mcp-watchlist-add-to-watchlist; quota"
    assert "SECRET" not in error.detail


def test_jsonrpc_error_names_only_its_integer_code():
    _, sync = api()
    from mcp.shared.exceptions import MCPError

    class Session:
        async def call_tool(self, *args, **kwargs):
            raise MCPError(code=-32602, message="bad params SECRET-9")

    client = SimpleNamespace(session=Session())

    async def run():
        tools = sync.OfficialTools(client, interval=0)
        tools.schemas["add_to_watchlist"] = {"type": "object"}
        tools.wire["add_to_watchlist"] = "mcp-watchlist-add-to-watchlist"
        with pytest.raises(sync.SyncError) as error:
            await tools.call("add_to_watchlist", {"watchlist_id": "1", "symbols": ["NASDAQ:AAPL"]})
        return error.value

    error = asyncio.run(run())
    assert str(error) == sync.SESSION_REFUSED
    assert error.detail == "JSON-RPC error -32602"
    assert "SECRET" not in error.detail


def test_same_owner_session_can_sync_another_cycle(tmp_path: Path):
    inputs, sync = api()
    data = inputs.read_research_inputs(unit(tmp_path), now=NOW)
    peer = Peer()

    async def run():
        async with Client(peer.server) as client:
            tools = sync.OfficialTools(client, interval=0, now=lambda: NOW)
            await sync.sync_research(tools, data, apply=True)
            return await sync.sync_research(tools, data, apply=True)

    result = asyncio.run(run())
    assert result["applied"] is True and result["operations"] == []


def test_oversize_parent_is_not_an_authoritative_top100(tmp_path: Path):
    inputs, _ = api()
    unit(tmp_path)
    change(tmp_path, "top100.json", candidates=[member(f"S{n}") for n in range(101)])
    with pytest.raises(inputs.InputError):
        inputs.read_research_inputs(tmp_path, now=NOW)


def test_ambiguous_create_identity_stops_before_creating_another_list(tmp_path: Path):
    _, sync = api()
    peer = Peer()
    del peer.lists["1"]
    del peer.lists["2"]
    peer.bad_create = True
    with pytest.raises(sync.SyncError):
        run_sync(unit(tmp_path), peer)
    assert [name for name, _ in peer.calls].count("create_watchlist") == 1


@pytest.mark.parametrize("response", ["header_mismatch", "input_required"])
def test_sdk_does_not_negotiate_and_resend_mutations(monkeypatch: pytest.MonkeyPatch, response):
    from mcp.shared.exceptions import MCPError
    from mcp_types import HEADER_MISMATCH, InputRequiredResult

    _, sync = api()
    peer = Peer()
    attempts = []

    async def run():
        async with Client(peer.server) as client:
            tools = sync.OfficialTools(client, interval=0, now=lambda: NOW)
            await tools.discover()

            async def rejected(*args, **kwargs):
                attempts.append(args)
                if response == "header_mismatch":
                    raise MCPError(code=HEADER_MISMATCH, message="fixture schema mismatch")
                return InputRequiredResult(input_requests={}, request_state="fixture")

            monkeypatch.setattr(client.session, "send_request", rejected)
            with pytest.raises(sync.SyncError):
                await tools.call("create_watchlist", {"name": "NS focus", "symbols": []})

    asyncio.run(run())
    assert len(attempts) == 1
    assert peer.calls == []


# A lasting sign-in: the owner's D-23 word, kept in the repo's encrypted store (ADR-0021 amendment).

LOGIN_NAME = "TRADINGVIEW_MCP_OAUTH"
LOGIN_ACCESS = "fixture-access-2b1f6c"
LOGIN_REFRESH = "fixture-refresh-9d4a2e"
LOGIN_CLIENT = "fixture-client-7c3e11"


def stored_login():
    try:
        return importlib.import_module("sextant.tradingview.stored_login")
    except ModuleNotFoundError:
        pytest.fail("The encrypted TradingView login store is not implemented")


def a_token(access: str = LOGIN_ACCESS, refresh: str = LOGIN_REFRESH, expires_in: int = 3600):
    from mcp.shared.auth import OAuthToken

    return OAuthToken(
        access_token=access,
        refresh_token=refresh,
        token_type="Bearer",  # noqa: S106 - fixture value, not a credential
        expires_in=expires_in,
    )


def a_registration():
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    return OAuthClientInformationFull(
        client_id=LOGIN_CLIENT,
        redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")],
        token_endpoint_auth_method="none",  # noqa: S106 - public client, no secret
    )


def a_registered_login(tmp_path: Path):
    """The real store under a temporary HOME, or a host-absence skip (the local run witnesses)."""
    module = stored_login()
    login = module.StoredLogin(home=tmp_path)
    try:
        login.save(a_token(), a_registration())
    except module.StoredLoginError:
        pytest.skip(
            "the systemd-creds user store is unavailable here; the local ci run is the witness"
        )
    return login


def test_stored_login_is_encrypted_at_rest_rotates_and_forgets(tmp_path: Path, capfd, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    capfd.readouterr()
    module = stored_login()
    login = a_registered_login(tmp_path)
    try:
        path = tmp_path / ".config" / "sextant" / "credentials" / LOGIN_NAME
        assert path.is_file()
        assert (path.stat().st_mode & 0o777) == 0o600
        raw = path.read_bytes()
        for secret in (LOGIN_ACCESS, LOGIN_REFRESH, LOGIN_CLIENT):
            assert secret.encode() not in raw

        loaded = login.load()
        assert loaded is not None
        assert loaded.tokens.access_token == LOGIN_ACCESS
        assert loaded.tokens.refresh_token == LOGIN_REFRESH
        assert loaded.client_info.client_id == LOGIN_CLIENT

        rotated = a_token(access="rotated-access", refresh="rotated-refresh")
        asyncio.run(module.StoredTokenStorage(login).set_tokens(rotated))
        after = login.load()
        assert after is not None
        assert after.tokens.refresh_token == "rotated-refresh"  # noqa: S105 - fixture
        assert b"rotated-refresh" not in path.read_bytes()

        assert login.forget() is True
        assert login.load() is None
        assert not path.exists()

        # File-descriptor capture also sees a key-store subprocess that writes to the
        # terminal; each stream is searched as text, not as a tuple of whole strings.
        out, err = capfd.readouterr()
        assert_no_secret(out + err + caplog.text)
    finally:
        login.forget()


def test_keep_login_without_a_stored_login_still_requires_owner_consent(tmp_path: Path):
    auth = importlib.import_module("sextant.tradingview.auth")
    login = stored_login().StoredLogin(home=tmp_path)

    async def run():
        with pytest.raises(auth.SyncError) as raised:
            async with auth.official_client(authorize=False, login=login):
                pytest.fail("no stored login and no owner consent must not open a session")
        return raised.value

    assert asyncio.run(run()).stage == "authorize"


def test_real_sdk_refresh_writes_the_rotated_token_back(tmp_path: Path):
    import httpx2
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    module = stored_login()
    login = a_registered_login(tmp_path)
    # Already past its lifetime: the store's issue time primes the SDK's expiry, so the
    # next process refreshes through the stored refresh token instead of re-consenting.
    login.save(
        a_token(access="stale-access", refresh="old-refresh", expires_in=-60), a_registration()
    )
    seen: list[tuple[str, str | None]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if str(request.url).endswith("/token"):
            body = (await request.aread()).decode()
            assert "grant_type=refresh_token" in body
            assert "refresh_token=old-refresh" in body
            return httpx2.Response(
                200,
                content=json.dumps(
                    {
                        "access_token": "fresh-access",
                        "refresh_token": "fresh-refresh",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                ).encode(),
            )
        return httpx2.Response(200, content=b"{}")

    async def run():
        storage = module.StoredTokenStorage(login)
        provider = OAuthClientProvider(
            server_url="https://mcp.tradingview.com/mcp",
            client_metadata=OAuthClientMetadata(
                client_name="fixture", redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")]
            ),
            storage=storage,
            redirect_handler=pytest.fail,
            callback_handler=pytest.fail,
        )
        storage.prime_expiry(provider.context)
        async with httpx2.AsyncClient(
            auth=provider, transport=httpx2.MockTransport(handler)
        ) as client:
            return await client.post("https://mcp.tradingview.com/mcp", json={})

    assert asyncio.run(run()).status_code == 200
    stored = login.load()
    assert stored is not None
    assert stored.tokens.refresh_token == "fresh-refresh"  # noqa: S105 - fixture
    assert [url for url, _ in seen] == [
        "https://mcp.tradingview.com/token",
        "https://mcp.tradingview.com/mcp",
    ]
    assert seen[0][1] is None
    assert seen[1][1] == "Bearer fresh-access"


def test_a_refused_stored_login_falls_back_once_and_never_loops():
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    class Storage:
        def __init__(self):
            self.tokens = a_token(expires_in=3600)
            self.client_info = a_registration()

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client_info

        async def set_client_info(self, client_info):
            self.client_info = client_info

    storage = Storage()
    redirects: list[str] = []
    states: list[str] = []
    callbacks: list[bool] = []
    mcp_calls: list[str | None] = []
    token_calls: list[str] = []
    other: list[str] = []

    async def redirect(url: str) -> None:
        redirects.append(url)
        states.append(parse_qs(urlsplit(url).query)["state"][0])

    async def callback():
        callbacks.append(True)
        return AuthorizationCodeResult(code="fixture-code", state=states[-1])

    async def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == "https://mcp.tradingview.com/mcp":
            mcp_calls.append(request.headers.get("authorization"))
            return httpx2.Response(401, content=b"")
        if url.endswith("/token"):
            token_calls.append((await request.aread()).decode())
            return httpx2.Response(
                200,
                content=json.dumps(
                    {
                        "access_token": "fresh-access",
                        "refresh_token": "fresh-refresh",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                ).encode(),
            )
        if "/.well-known/" in url:
            return httpx2.Response(404, content=b"")
        other.append(url)
        return httpx2.Response(404, content=b"")

    async def run():
        provider = OAuthClientProvider(
            server_url="https://mcp.tradingview.com/mcp",
            client_metadata=OAuthClientMetadata(
                client_name="fixture", redirect_uris=[AnyUrl("http://127.0.0.1:1/callback")]
            ),
            storage=storage,
            redirect_handler=redirect,
            callback_handler=callback,
        )
        async with httpx2.AsyncClient(
            auth=provider, transport=httpx2.MockTransport(handler)
        ) as client:
            return await client.post("https://mcp.tradingview.com/mcp", json={})

    assert asyncio.run(run()).status_code == 401
    assert other == []
    assert len(redirects) == 1
    assert redirects[0].startswith("https://mcp.tradingview.com/authorize?")
    assert callbacks == [True]
    assert len(token_calls) == 1 and "grant_type=authorization_code" in token_calls[0]
    assert mcp_calls == ["Bearer " + LOGIN_ACCESS, "Bearer fresh-access"]


def test_cli_forget_login_is_the_one_command_and_never_opens_the_sdk(monkeypatch, capsys):
    cli = importlib.import_module("sextant.tradingview.__main__")
    calls: list[str] = []

    class Login:
        def forget(self):
            calls.append("forget")
            return True

    def forbidden(*args, **kwargs):
        pytest.fail("forget must not open an OAuth session")

    monkeypatch.setattr(cli, "StoredLogin", Login)
    monkeypatch.setattr(cli, "official_client", forbidden)
    assert cli.main(["--forget-login"]) == 0
    assert calls == ["forget"]
    out = capsys.readouterr().out
    assert "Stored TradingView sign-in removed." in out


def test_cli_keep_login_passes_the_stored_login_to_one_cycle(tmp_path: Path, monkeypatch):
    cli = importlib.import_module("sextant.tradingview.__main__")
    _, sync = api()
    seen: dict[str, Any] = {}

    class Login:
        pass

    @asynccontextmanager
    async def fake_client(authorize, login=None):
        seen.update(authorize=authorize, login=login)
        raise sync.SyncError(sync.INVALID_INPUTS)
        yield  # pragma: no cover

    monkeypatch.setattr(cli, "StoredLogin", Login)
    monkeypatch.setattr(cli, "official_client", fake_client)
    monkeypatch.setattr(cli, "run_loop", pytest.fail)
    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    assert cli.main(["--research-dir", str(tmp_path), "--keep-login"]) == 2
    assert isinstance(seen["login"], Login)
    assert seen["authorize"] is False


def test_the_first_keep_login_authentication_is_saved_before_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from mcp.client.auth import AuthorizationCodeResult

    auth = importlib.import_module("sextant.tradingview.auth")
    cli = importlib.import_module("sextant.tradingview.__main__")
    module = stored_login()
    events: list[str] = []
    saved: list[Any] = []
    storage_types: list[str] = []
    states: list[str] = []
    state = SimpleNamespace(document=None)
    original_http_client = httpx2.AsyncClient

    class Login:
        def present(self):
            return state.document is not None

        def load(self):
            return state.document

        def save(self, tokens, client_info, obtained_at):
            events.append("save")
            saved.append((tokens, client_info, obtained_at))
            state.document = module.StoredDocument(tokens, client_info, obtained_at)

        def forget(self):
            events.append("forget")
            removed = state.document is not None
            state.document = None
            return removed

    class Callback:
        uri = "http://127.0.0.1:1/callback"
        result = None
        server = SimpleNamespace(server_close=lambda: None)

        async def redirect(self, url):
            events.append("consent")
            states.append(parse_qs(urlsplit(url).query)["state"][0])

        async def wait(self):
            return AuthorizationCodeResult(code="fixture-code", state=states[-1])

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/mcp":
            if request.headers.get("authorization") == "Bearer first-access":
                events.append("authenticated")
                return httpx2.Response(200, json={"authenticated": True})
            return httpx2.Response(401)
        if request.url.path == "/.well-known/oauth-authorization-server":
            # The legacy origin document (oauth2.py:696-718); a stored login needs one.
            base = "https://mcp.tradingview.com"
            return httpx2.Response(
                200,
                json={
                    "issuer": base,
                    "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token",
                    "registration_endpoint": base + "/register",
                },
            )
        if "/.well-known/" in request.url.path:
            return httpx2.Response(404)
        if request.url.path == "/register":
            return httpx2.Response(
                201,
                json={
                    "client_id": "witness-client",
                    "redirect_uris": [Callback.uri],
                    "token_endpoint_auth_method": "none",
                },
            )
        if request.url.path == "/token":
            return httpx2.Response(
                200,
                json={
                    "access_token": "first-access",
                    "refresh_token": "first-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        pytest.fail(f"unexpected mocked path {request.url.path}", pytrace=False)

    def http_client(**options):
        options.pop("transport", None)  # This fixture supplies its own MockTransport.
        storage_types.append(type(options["auth"].context.storage).__name__)
        return original_http_client(
            transport=httpx2.MockTransport(handler), trust_env=False, **options
        )

    def transport(server, http_client):
        assert server == auth.SERVER
        return http_client

    class Client:
        def __init__(self, transport, **options):
            self.transport = transport

        async def __aenter__(self):
            response = await self.transport.post(auth.SERVER, json={})
            assert response.status_code == 200
            return self

        async def __aexit__(self, *args):
            return False

    async def sync(*args, **kwargs):
        events.append("sync")
        return {}

    monkeypatch.setattr(auth, "OwnerCallback", Callback)
    monkeypatch.setattr(auth.httpx2, "AsyncClient", http_client)
    monkeypatch.setattr(auth, "streamable_http_client", transport)
    monkeypatch.setattr(auth, "Client", Client)
    monkeypatch.setattr(cli, "StoredLogin", Login)
    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "sync_research", sync)

    assert cli.main(["--research-dir", str(tmp_path), "--authorize", "--keep-login"]) == 0
    assert "authenticated" in events
    assert "sync" in events
    if not saved:
        pytest.fail(
            "the first successful --keep-login reached sync without saving the login; "
            f"storage={storage_types}, events={events}",
            pytrace=False,
        )
    assert events.index("save") < events.index("sync")
    assert storage_types == ["StoredTokenStorage"]
    assert state.document is not None
    assert state.document.tokens.access_token == "first-access"  # noqa: S105 - fixture
    assert state.document.client_info is not None
    assert state.document.client_info.client_id == "witness-client"

    events.clear()
    assert cli.main(["--research-dir", str(tmp_path), "--keep-login"]) == 0
    assert "consent" not in events
    assert "authenticated" in events
    assert storage_types[-1] == "StoredTokenStorage"


def test_keys_registers_the_tradingview_login_sealed_name():
    text = (Path(__file__).resolve().parents[2] / "scripts" / "keys.sh").read_text()
    known = text.split("KNOWN=(", 1)[1].split("\n)", 1)[0]
    assert LOGIN_NAME in known.split()


@pytest.mark.parametrize(
    ("invalid_section", "marker"),
    [
        ("tokens", "witness-refresh-token-value"),
        ("client_info", "witness-client-secret-value"),
    ],
)
def test_unreadable_stored_document_does_not_expose_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_section: str, marker: str
):
    import subprocess
    import traceback

    module = stored_login()
    payload = {
        "tokens": {"access_token": "witness-access-token-value"},
        "client_info": {"client_id": "witness-client-id-value"},
        "obtained_at": 1.0,
    }
    if invalid_section == "tokens":
        payload["tokens"] = {"refresh_token": marker}
    else:
        payload["client_info"] = {"client_secret": marker}
    login = module.StoredLogin(home=tmp_path)
    monkeypatch.setattr(login, "_state", lambda: "set")
    monkeypatch.setattr(
        module,
        "_run",
        lambda *arguments, **options: subprocess.CompletedProcess(
            arguments, 0, json.dumps(payload).encode(), b""
        ),
    )
    exposes_credential = False
    sanitized = False
    try:
        login.load()
    except module.StoredLoginError as error:
        exposes_credential = any(marker in line for line in traceback.format_exception(error))
        sanitized = (
            error.__cause__ is None
            and error.__context__ is None
            and error.__suppress_context__ is True
        )
    else:
        pytest.fail("malformed stored document was not refused", pytrace=False)
    if exposes_credential:
        pytest.fail(
            "the stored credential is exposed by the chained exception graph",
            pytrace=False,
        )
    assert sanitized


# Fix round on the stored login (issue #470 row 14). Each value below is a recognisable
# fake; a proof that output is clean searches every output stream and file for all of them.
FAKE_ACCESS = "fake-access-token-a91c"
FAKE_REFRESH = "fake-refresh-token-4be7"
ROTATED_ACCESS = "fake-rotated-access-c30d"
ROTATED_REFRESH = "fake-rotated-refresh-58f2"
FRESH_ACCESS = "fake-fresh-access-e6b0"
FRESH_REFRESH = "fake-fresh-refresh-71d4"
FAKE_CODE = "fake-auth-code-9a2e"
FAKE_CALLBACK = "http://127.0.0.1:1/callback"
FAKES = (
    LOGIN_ACCESS,
    LOGIN_REFRESH,
    LOGIN_CLIENT,
    FAKE_ACCESS,
    FAKE_REFRESH,
    ROTATED_ACCESS,
    ROTATED_REFRESH,
    FRESH_ACCESS,
    FRESH_REFRESH,
    FAKE_CODE,
    "127.0.0.1:1",
)


def assert_no_secret(text: str, root: Path | None = None) -> None:
    """Names the leaked fixtures only, so a failure never prints the surrounding output."""
    leaked = [value for value in FAKES if value in text]
    for path in sorted(root.rglob("*")) if root is not None else ():
        if path.is_file():
            data = path.read_bytes()
            leaked += [f"{path.name}:{value}" for value in FAKES if value.encode() in data]
    assert leaked == []


def literal(name: str) -> str:
    _, sync = api()
    return getattr(sync, name, f"<sync.{name} is not defined>")


# The service's shape as the lead measured it live (2026-11-11): the authorization server is on
# another host, and the SDK's fallback `<server>/token` answers 403 with a CSRF refusal.
ISSUER = "https://www.tradingview.com"
PUBLISHED_TOKEN = ISSUER + "/mcp/oauth/token"
CSRF = {"detail": "CSRF validation failed", "code": "CSRF_TOKEN_INVALID"}
METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/mcp/oauth/authorize",
    "token_endpoint": PUBLISHED_TOKEN,
    "registration_endpoint": ISSUER + "/mcp/oauth/register",
}
RESOURCE = {"resource": "https://mcp.tradingview.com/mcp", "authorization_servers": [ISSUER]}


def a_bound_registration(issuer: str | None = ISSUER):
    """The registration as the SDK stores it: bound to its issuer (SEP-2352, oauth2.py:766-781)."""
    return a_registration().model_copy(update={"issuer": issuer})


class MemoryLogin:
    """StoredLogin's four calls over one in-memory document, recorded in call order."""

    def __init__(
        self, events: list[str], tokens=None, *, obtained_at: float | None = None, client=None
    ):
        self.events = events
        self.document = None
        if tokens is not None:
            self.document = stored_login().StoredDocument(
                tokens,
                client or a_bound_registration(),
                time.time() if obtained_at is None else obtained_at,
            )

    def present(self):
        return self.document is not None

    def load(self):
        return self.document

    def save(self, tokens, client_info, obtained_at):
        self.events.append("save")
        self.document = stored_login().StoredDocument(tokens, client_info, obtained_at)

    def forget(self):
        self.events.append("forget")
        removed = self.document is not None
        self.document = None
        return removed


def stale_login(events: list[str], *, refresh: str | None = FAKE_REFRESH) -> MemoryLogin:
    """A stored login past its access lifetime, so the session refreshes first."""
    return MemoryLogin(
        events,
        a_token(access=FAKE_ACCESS, refresh=refresh, expires_in=60),  # type: ignore[arg-type]
        obtained_at=time.time() - 3600,
    )


def token_body(access: str, refresh: str, expires_in: int = 3600) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }


def oauth_service(
    events: list[str],
    *,
    refresh,
    accept=(FRESH_ACCESS, ROTATED_ACCESS),
    metadata=METADATA,
    prm_url=ISSUER + "/.well-known/oauth-protected-resource/mcp",
):
    """The service's shape: ``refresh(request)`` answers each refresh grant at the published
    endpoint; ``metadata`` is the issuer's published document, or a status code, or an exception;
    the 401 challenge names ``prm_url``, the protected-resource document."""
    import httpx2

    accepted = {f"Bearer {value}" for value in accept}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        host, path = request.url.host, request.url.path
        if (host, path) == ("mcp.tradingview.com", "/mcp"):
            if request.headers.get("authorization") in accepted:
                return httpx2.Response(200, json={})
            events.append("unauthorized")
            challenge = f'Bearer resource_metadata="{prm_url}"'
            return httpx2.Response(401, headers={"WWW-Authenticate": challenge})
        if str(request.url) == prm_url:
            return httpx2.Response(200, json=RESOURCE)
        if str(request.url) == ISSUER + "/.well-known/oauth-authorization-server":
            if isinstance(metadata, BaseException):
                raise metadata
            if isinstance(metadata, int):
                return httpx2.Response(metadata)
            return httpx2.Response(200, json=metadata)
        if "/.well-known/" in path:
            return httpx2.Response(404)
        if (host, path) == ("mcp.tradingview.com", "/token"):
            events.append("fallback")
            return httpx2.Response(403, json=CSRF)
        path = path.removeprefix("/mcp/oauth") if host == "www.tradingview.com" else "unexpected"
        if path == "/register":
            events.append("register")
            return httpx2.Response(
                201,
                json={
                    "client_id": "fake-reregistered-client",
                    "redirect_uris": [FAKE_CALLBACK],
                    "token_endpoint_auth_method": "none",
                },
            )
        if path == "/token":
            grant = parse_qs((await request.aread()).decode())["grant_type"][0]
            events.append(grant)
            if grant == "refresh_token":
                return refresh(request)
            return httpx2.Response(200, json=token_body(FRESH_ACCESS, FRESH_REFRESH))
        pytest.fail(f"unexpected mocked path {path}", pytrace=False)

    return handler


def wired_session(monkeypatch: pytest.MonkeyPatch, handler, events: list[str]):
    """The real official_client, provider, store adapter and hooks over a mocked service.

    Only the browser (``OwnerCallback``), the network transport and the MCP session
    object are fixtures; the session posts once on entry and fails any list or tool call.
    """
    import httpx2
    from mcp.client.auth import AuthorizationCodeResult

    auth = importlib.import_module("sextant.tradingview.auth")
    original = httpx2.AsyncClient
    states: list[str] = []

    class Callback:
        uri = FAKE_CALLBACK
        result = None
        server = SimpleNamespace(server_close=lambda: None)

        async def redirect(self, url: str) -> None:
            events.append("consent")
            states.append(parse_qs(urlsplit(url).query)["state"][0])

        async def wait(self):
            return AuthorizationCodeResult(code=FAKE_CODE, state=states[-1])

    class Session:
        def __init__(self, transport, **options):
            self.http = transport

        async def request(self) -> None:
            response = await self.http.post(auth.SERVER, json={})
            if response.status_code != 200:
                raise RuntimeError("fixture session refused")
            events.append("session")

        async def __aenter__(self):
            await self.request()
            return self

        async def __aexit__(self, *args):
            return False

        async def list_tools(self, *args, **kwargs):
            pytest.fail("this run must not list tools", pytrace=False)

        async def call_tool(self, *args, **kwargs):
            pytest.fail("this run must not call a tool", pytrace=False)

    def http_client(**options):
        options.pop("transport", None)  # This fixture supplies its own MockTransport.
        return original(transport=httpx2.MockTransport(handler), trust_env=False, **options)

    monkeypatch.setattr(auth, "OwnerCallback", Callback)
    monkeypatch.setattr(auth.httpx2, "AsyncClient", http_client)
    monkeypatch.setattr(auth, "streamable_http_client", lambda server, http_client: http_client)
    monkeypatch.setattr(auth, "Client", Session)
    return auth


def open_session(auth, *, authorize: bool, login) -> None:
    async def run():
        async with auth.official_client(authorize, login):
            pass

    asyncio.run(run())


def refused_session(auth, *, authorize: bool, login):
    with pytest.raises(auth.SyncError) as raised:
        open_session(auth, authorize=authorize, login=login)
    return raised.value


def answer(status: int, body) -> Any:
    import httpx2

    if isinstance(body, bytes):
        return lambda request: httpx2.Response(status, content=body)
    return lambda request: httpx2.Response(status, json=body)


@pytest.mark.parametrize("authorize", [True, False])
@pytest.mark.parametrize("status", [500, 503, 429])
def test_a_token_endpoint_outage_keeps_the_stored_login_and_stops(
    monkeypatch: pytest.MonkeyPatch, status: int, authorize: bool
):
    """Fix 1: the status decides; even a refusal code in a 5xx or 429 body keeps the login."""
    events: list[str] = []
    login = stale_login(events)
    before = login.document
    service = oauth_service(events, refresh=answer(status, {"error": "invalid_grant"}))
    auth = wired_session(monkeypatch, service, events)
    error = refused_session(auth, authorize=authorize, login=login)
    assert events == ["refresh_token"]
    assert login.document is before
    assert str(error) == literal("LOGIN_UNAVAILABLE")


@pytest.mark.parametrize("authorize", [True, False])
def test_a_transport_failure_at_the_token_endpoint_keeps_the_stored_login(
    monkeypatch: pytest.MonkeyPatch, authorize: bool
):
    import httpx2

    def refresh(request):
        raise httpx2.ConnectError("fixture outage", request=request)

    events: list[str] = []
    login = stale_login(events)
    before = login.document
    auth = wired_session(monkeypatch, oauth_service(events, refresh=refresh), events)
    error = refused_session(auth, authorize=authorize, login=login)
    assert events == ["refresh_token"]
    assert login.document is before
    assert str(error) == literal("LOGIN_UNAVAILABLE")


@pytest.mark.parametrize("authorize", [True, False])
@pytest.mark.parametrize(
    ("status", "body"),
    [(400, {"error": "invalid_request"}), (400, b"not json"), (403, {})],
)
def test_an_unclear_refresh_answer_keeps_the_stored_login_and_stops(
    monkeypatch: pytest.MonkeyPatch, status: int, body, authorize: bool
):
    events: list[str] = []
    login = stale_login(events)
    before = login.document
    auth = wired_session(monkeypatch, oauth_service(events, refresh=answer(status, body)), events)
    error = refused_session(auth, authorize=authorize, login=login)
    assert events == ["refresh_token"]
    assert login.document is before
    assert str(error) == literal("LOGIN_UNUSABLE")


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "invalid_grant"), (401, "invalid_client"), (400, "unauthorized_client")],
)
def test_a_definite_refusal_is_forgotten_before_the_one_browser_sign_in(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str
):
    """Fixes 1 and 5, through auth.py's real wiring: forget comes before any fallback."""
    events: list[str] = []
    login = stale_login(events)
    service = oauth_service(events, refresh=answer(status, {"error": code}))
    auth = wired_session(monkeypatch, service, events)
    open_session(auth, authorize=True, login=login)
    assert events[:2] == ["refresh_token", "forget"]
    assert events.count("consent") == 1
    assert events.index("consent") < events.index("save") < events.index("session")
    assert login.document is not None
    assert login.document.tokens.access_token == FRESH_ACCESS
    client = login.document.client_info
    assert client is not None
    if code == "invalid_client":
        # The registration itself was refused: the browser sign-in registers a new client.
        assert "register" in events
        assert client.client_id == "fake-reregistered-client"
    else:
        assert "register" not in events
        assert client.client_id == LOGIN_CLIENT


def test_keep_login_without_authorize_never_opens_a_browser_after_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fix 2: an unattended --keep-login run stops with the owner step instead."""
    events: list[str] = []
    login = stale_login(events)
    service = oauth_service(events, refresh=answer(400, {"error": "invalid_grant"}))
    auth = wired_session(monkeypatch, service, events)
    error = refused_session(auth, authorize=False, login=login)
    assert events == ["refresh_token", "forget"]
    assert login.document is None
    assert str(error) == literal("LOGIN_REFUSED")


@pytest.mark.parametrize("case", ["rejected access token", "no refresh token"])
def test_keep_login_without_authorize_refuses_when_the_service_wants_consent(
    monkeypatch: pytest.MonkeyPatch, case: str
):
    events: list[str] = []
    if case == "rejected access token":
        login = MemoryLogin(events, a_token(access=FAKE_ACCESS, refresh=FAKE_REFRESH))
    else:
        login = stale_login(events, refresh=None)
    before = login.document
    service = oauth_service(events, refresh=answer(500, {}), accept=())
    auth = wired_session(monkeypatch, service, events)
    error = refused_session(auth, authorize=False, login=login)
    assert events == ["unauthorized"]
    assert login.document is before
    assert str(error) == literal("LOGIN_NEEDS_CONSENT")


def test_keep_login_without_authorize_and_no_stored_login_refuses_before_a_session(
    monkeypatch: pytest.MonkeyPatch,
):
    events: list[str] = []
    auth = wired_session(monkeypatch, oauth_service(events, refresh=answer(500, {})), events)
    monkeypatch.setattr(auth, "OwnerCallback", lambda: pytest.fail("no session", pytrace=False))
    error = refused_session(auth, authorize=False, login=MemoryLogin(events))
    assert events == []
    assert error.stage == "authorize"
    assert str(error) == literal("LOGIN_NEEDS_CONSENT")


def token_requests(events: list[str], handler):
    """The handler, recording where each token request went."""

    async def recorded(request):
        if request.url.path.endswith("/token"):
            events.append(f"POST {request.url.host}{request.url.path}")
        return await handler(request)

    return recorded


@pytest.mark.parametrize("client", ["bound", "unbound"])
def test_a_fresh_process_refreshes_at_the_published_token_endpoint(
    monkeypatch: pytest.MonkeyPatch, client: str
):
    """Measured live: the SDK's fallback `<server>/token` answers 403 (CSRF); the published
    endpoint is on the issuer's host. Unbound: a registration stored without its issuer."""
    events: list[str] = []
    login = stale_login(events)
    if client == "unbound":
        assert login.document is not None
        login.document = stored_login().StoredDocument(
            login.document.tokens, a_bound_registration(None), login.document.obtained_at
        )
    # Unbound, only the server's own well-known (SEP-985) can name the issuer before a 401.
    prm = "https://mcp.tradingview.com/.well-known/oauth-protected-resource/mcp"
    refresh = answer(200, token_body(ROTATED_ACCESS, ROTATED_REFRESH))
    service = oauth_service(
        events, refresh=refresh, **({"prm_url": prm} if client == "unbound" else {})
    )
    auth = wired_session(monkeypatch, token_requests(events, service), events)
    open_session(auth, authorize=False, login=login)
    assert events[:2] == ["POST www.tradingview.com/mcp/oauth/token", "refresh_token"]
    assert "fallback" not in events
    assert "session" in events
    assert login.document is not None
    assert login.document.tokens.refresh_token == ROTATED_REFRESH


@pytest.mark.parametrize("authorize", [False, True])
@pytest.mark.parametrize("metadata", [404, 503, "transport"])
def test_without_the_published_token_endpoint_the_run_stops_and_keeps_the_login(
    monkeypatch: pytest.MonkeyPatch, metadata, authorize: bool
):
    """A refresh is never sent to a fallback endpoint; the store is left for the next run."""
    import httpx2

    if metadata == "transport":
        metadata = httpx2.ConnectError("fixture outage")
    events: list[str] = []
    login = stale_login(events)
    before = login.document
    service = oauth_service(events, refresh=answer(200, {}), metadata=metadata)
    auth = wired_session(monkeypatch, service, events)
    error = refused_session(auth, authorize=authorize, login=login)
    assert events == []
    assert login.document is before
    assert str(error) == literal("LOGIN_UNAVAILABLE")


def test_the_guard_never_lets_a_refresh_reach_an_unpublished_endpoint():
    import httpx2

    module = stored_login()
    guard = module.RefreshGuard(module.StoredTokenStorage(MemoryLogin([])), consent=True)
    guard.context = SimpleNamespace(oauth_metadata=None)
    form = {"grant_type": "refresh_token", "refresh_token": FAKE_REFRESH}
    request = httpx2.Request("POST", "https://mcp.tradingview.com/token", data=form)
    with pytest.raises(module.StoredLoginStop):
        asyncio.run(guard.request(request))
    assert guard.reason() == "unavailable"


@pytest.mark.parametrize("authorize", [False, True])
def test_a_corrupt_stored_login_stops_at_the_cli_naming_forget_then_authorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, authorize: bool
):
    """Fix 3: --authorize alone would fail the same way, so the step names --forget-login."""
    import subprocess

    cli = importlib.import_module("sextant.tradingview.__main__")
    auth = importlib.import_module("sextant.tradingview.auth")
    module = stored_login()
    login = module.StoredLogin(home=tmp_path)
    corrupt = json.dumps({"tokens": {"refresh_token": FAKE_REFRESH}}).encode()
    monkeypatch.setattr(login, "_state", lambda: "set")
    monkeypatch.setattr(
        module, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 0, corrupt, b"")
    )
    monkeypatch.setattr(auth, "OwnerCallback", lambda: pytest.fail("no sign-in", pytrace=False))
    monkeypatch.setattr(cli, "StoredLogin", lambda: login)
    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    argv = ["--research-dir", str(tmp_path), "--keep-login"] + (
        ["--authorize"] if authorize else []
    )
    assert cli.main(argv) == 2
    out, err = capfd.readouterr()
    assert literal("LOGIN_UNREADABLE") in out
    assert "--forget-login, then --authorize --keep-login" in literal("LOGIN_UNREADABLE")
    assert literal("LOGIN_UNREADABLE") in err  # the page
    assert_no_secret(out + err)


def test_with_keys_cannot_put_the_stored_login_in_an_environment(tmp_path: Path):
    """Fix 4: with-keys refuses the name; the sync's own loader still reads it."""
    import subprocess

    login = a_registered_login(tmp_path)
    try:
        done = subprocess.run(
            [
                "bash",
                str(Path(__file__).resolve().parents[2] / "scripts" / "with-keys.sh"),
                "-k",
                LOGIN_NAME,
                "--",
                "printenv",
                LOGIN_NAME,
            ],
            capture_output=True,
            text=True,
            check=False,
            env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        code = done.returncode
        assert code == 3
        assert_no_secret(done.stdout + done.stderr)
        loaded = login.load()
        assert loaded is not None
        assert loaded.tokens.refresh_token == LOGIN_REFRESH
    finally:
        login.forget()


@pytest.mark.parametrize(
    ("refresh", "code", "message"),
    [
        (answer(200, token_body(ROTATED_ACCESS, ROTATED_REFRESH)), 0, ""),
        (answer(503, {}), 2, "LOGIN_UNAVAILABLE"),
        (answer(400, {"error": "invalid_grant"}), 2, "LOGIN_REFUSED"),
    ],
)
def test_the_keep_login_cli_prints_no_token_code_or_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog, refresh, code, message
):
    """Fix 5: stdout, stderr, logs and files carry none of the fakes on any outcome."""
    import logging

    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    login = stale_login(events)
    wired_session(monkeypatch, oauth_service(events, refresh=refresh), events)
    cli = importlib.import_module("sextant.tradingview.__main__")

    async def sync(tools, inputs, *, apply):
        return {"applied": False}

    monkeypatch.setattr(cli, "StoredLogin", lambda: login)
    monkeypatch.setattr(cli, "read_research_inputs", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "sync_research", sync)
    monkeypatch.chdir(tmp_path)
    capfd.readouterr()
    assert cli.main(["--research-dir", str(tmp_path), "--keep-login"]) == code
    out, err = capfd.readouterr()
    assert "consent" not in events
    if message:
        assert literal(message) in out
        assert literal(message) in err  # the page
    else:
        assert login.document is not None
        assert login.document.tokens.refresh_token == ROTATED_REFRESH
    assert_no_secret(out + err + caplog.text, tmp_path)


def sign_in_cli(monkeypatch: pytest.MonkeyPatch, login):
    cli = importlib.import_module("sextant.tradingview.__main__")

    def forbidden(*args, **kwargs):
        pytest.fail("a sign-in-only run reads and writes no list", pytrace=False)

    monkeypatch.setattr(cli, "StoredLogin", lambda: login)
    for name in ("read_research_inputs", "sync_research", "OfficialTools", "run_loop"):
        monkeypatch.setattr(cli, name, forbidden)
    return cli


def test_sign_in_only_keeps_the_login_and_touches_no_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog
):
    """Addition 6: --authorize --keep-login with no display unit stores the sign-in only."""
    import logging

    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    login = MemoryLogin(events)
    service = oauth_service(events, refresh=answer(500, {}))
    wired_session(monkeypatch, service, events)
    cli = sign_in_cli(monkeypatch, login)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    capfd.readouterr()
    assert cli.main(["--authorize", "--keep-login"]) == 0
    out, err = capfd.readouterr()
    assert events.count("consent") == 1
    assert events.index("consent") < events.index("save") < events.index("session")
    assert login.document is not None
    assert login.document.tokens.refresh_token == FRESH_REFRESH
    assert out == getattr(cli, "SIGNED_IN", "<cli.SIGNED_IN is not defined>") + "\n"
    assert err == ""
    assert_no_secret(out + err + caplog.text, tmp_path)


def test_sign_in_only_writes_the_real_store_through_its_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd
):
    a_registered_login(tmp_path).forget()  # skips where the systemd-creds store is absent
    events: list[str] = []
    login = stored_login().StoredLogin(home=tmp_path)
    wired_session(monkeypatch, oauth_service(events, refresh=answer(500, {})), events)
    cli = sign_in_cli(monkeypatch, login)
    try:
        assert cli.main(["--authorize", "--keep-login"]) == 0
        stored = login.load()
        assert stored is not None
        assert stored.tokens.access_token == FRESH_ACCESS
        path = tmp_path / ".config" / "sextant" / "credentials" / LOGIN_NAME
        assert (path.stat().st_mode & 0o777) == 0o600
        out, err = capfd.readouterr()
        assert_no_secret(out + err, tmp_path)
    finally:
        login.forget()


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["--authorize"], "SIGN_IN_NEEDS_KEEP_LOGIN"),
        (["--authorize", "--keep-login", "--apply"], "SIGN_IN_CHANGES_NO_LIST"),
    ],
)
def test_sign_in_only_needs_keep_login_and_changes_no_list(
    monkeypatch: pytest.MonkeyPatch, capsys, argv: list[str], name: str
):
    cli = sign_in_cli(monkeypatch, object())
    monkeypatch.setattr(cli, "official_client", lambda *a, **k: pytest.fail("no session"))
    assert cli.main(argv) == 2
    assert capsys.readouterr().out == getattr(cli, name, f"<cli.{name} is not defined>") + "\n"

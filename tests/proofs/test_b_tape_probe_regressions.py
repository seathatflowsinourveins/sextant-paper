"""Codex's turn-3 review probes of #262 (repaired copies), carried in under B's own names.

The offline review harness is the autouse fixture below: no socket connect, a 15 s bound per test.
"""

import asyncio
import io
import json
import signal
import socket
import time
from datetime import datetime
from decimal import Decimal

import pytest
from alpaca.data.historical.stock import StockHistoricalDataClient
from test_b_outside_rth_feed import paced
from test_b_protector_outside_rth import (
    ET,
    POST_NS,
    FakeClient,
    FakeHistory,
    api_order,
    api_position,
    b_account,
    b_params,
    down_window_client,
    down_window_history,
    line_for,
    ns,
    protector,
    quote_at,
    start,
    tonight_client,
)
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

from sextant.paper.b_feed import BHistoricalTape, bound_transport
from sextant.paper.b_protector import Protector
from sextant.paper.b_protector_main import PacedHistoricalDataClient, PacedTradingClient


def quote_payload(at="2026-11-09T21:59:00Z", bid=38.70):
    return {"t": at, "bp": bid, "ap": bid + 0.01, "bs": 1, "as": 1}


def stub_http(monkeypatch, replies):
    calls = []

    def urlopen(pool, method, url, **kwargs):
        calls.append((method, url, kwargs.get("timeout")))
        status, payload = replies[len(calls) - 1]
        return HTTPResponse(
            body=io.BytesIO(json.dumps(payload).encode()),
            headers={"Content-Type": "application/json"},
            status=status,
            preload_content=False,
        )

    monkeypatch.setattr(HTTPSConnectionPool, "urlopen", urlopen)
    return calls


def test_transport_timeout_bounds_the_entire_start_read(monkeypatch):
    calls = stub_http(
        monkeypatch,
        [
            (429, {"code": 42910000, "message": "fixture rate limited"}),
            (429, {"code": 42910000, "message": "fixture rate limited"}),
            (200, {"quotes": {"AAPL": [quote_payload()]}}),
        ],
    )
    data = StockHistoricalDataClient("fixture", "fixture")
    timeout_s = Decimal("3.05")
    bound_transport(data, timeout_s)
    pacer = paced(1)
    history = BHistoricalTape(PacedHistoricalDataClient(data, pacer), 10_000)  # type: ignore  # an untyped review probe, kept as written
    manager = protector(tonight_client(), history=history, now_ns=POST_NS)
    began = time.monotonic()
    start(manager)
    elapsed = time.monotonic() - began
    line = line_for(manager, POST_NS, "36.60")
    observed = {
        "elapsed_s": round(elapsed, 3),
        "bound_s": str(timeout_s),
        "wire_requests": len(calls),
        "pacer_requests": pacer.wire_requests,
        "state": line,
        "pages": [page.reason for page in manager.pages],
    }
    assert elapsed <= float(timeout_s), observed


def test_every_sdk_page_consumes_an_account_pacer_admission(monkeypatch):
    # Repaired turn 3 (Codex r5): a continuation token is PARTIAL with the page, one request
    # per admission; the second page is never fetched.
    calls = stub_http(
        monkeypatch,
        [
            (200, {"quotes": {"AAPL": [quote_payload()]}, "next_page_token": "page-two"}),
            (200, {"quotes": {"AAPL": [quote_payload("2026-11-09T22:00:00Z", 38.60)]}}),
        ],
    )
    data = StockHistoricalDataClient("fixture", "fixture")
    bound_transport(data, Decimal("3.05"))
    pacer = paced(1)
    history = BHistoricalTape(PacedHistoricalDataClient(data, pacer), 10_000)  # type: ignore  # an untyped review probe, kept as written
    manager = protector(tonight_client(), history=history, now_ns=POST_NS)
    start(manager)
    observed = {
        "wire_requests": len(calls),
        "pacer_requests": pacer.wire_requests,
        "tape": manager._tapes.get("AAPL"),
        "pages": [page.reason for page in manager.pages],
    }
    assert len(calls) == pacer.wire_requests == 1, observed
    assert manager._tapes["AAPL"].status == "partial", observed
    assert [page.reason for page in manager.pages] == ["outside_rth_tape_unreadable"], observed


def test_a_refused_adoption_history_read_is_not_replayed_later():
    client = tonight_client()
    pacer = paced(1)
    paced_client = PacedTradingClient(client, pacer)
    manager = Protector(
        paced_client, b_account(), b_params(), now_ns=lambda: POST_NS, history=FakeHistory()
    )
    manager.rebuild_tapes()
    result = manager._tapes["AAPL"]
    assert result.status == "partial"
    queued_after_refusal = len(pacer.queue)
    pacer.bucket.tokens = 1
    pacer.drain()
    observed = {
        "queued_after_refusal": queued_after_refusal,
        "later_broker_calls": client.calls,
        "status": result.status,
    }
    assert client.calls == ["get_all_positions"], observed


def test_a_partial_owner_add_cannot_reuse_the_pre_add_high():
    client = FakeClient()
    client.positions = [api_position("150")]
    client.orders = [
        api_order(qty="150", stop_price="11.456", submitted_at="2026-11-09T16:30:01Z"),
        api_order(
            client_order_id="owner-add-partial",
            qty="100",
            filled_qty="50",
            side="buy",
            order_type="limit",
            status="partially_filled",
            submitted_at="2026-11-09T16:20:00Z",
        ).model_copy(update={"updated_at": datetime(2026, 11, 9, 11, 30, tzinfo=ET)}),
    ]
    client.closed_orders = [
        api_order(
            client_order_id="owner-entry",
            qty="100",
            filled_qty="100",
            side="buy",
            order_type="limit",
            status="filled",
            submitted_at="2026-11-09T15:55:00Z",
            filled_at="2026-11-09T16:00:00Z",
        ),
        api_order(
            qty="100",
            status="canceled",
            stop_price="11.00",
            submitted_at="2026-11-09T16:00:01Z",
        ),
    ]
    history = FakeHistory(
        [
            quote_at("12.00", datetime(2026, 11, 9, 11, 10, tzinfo=ET)),
            quote_at("11.70", datetime(2026, 11, 9, 11, 45, tzinfo=ET)),
        ]
    )
    manager = protector(client, history=history, now_ns=POST_NS)
    start(manager)
    line = line_for(manager, POST_NS, "11.50")
    assert manager.emulated_trigger("AAPL") == Decimal("11.456"), {
        "requests": history.requests,
        "state": line,
        "pages": [page.reason for page in manager.pages],
    }


def test_a_house_limit_is_not_an_adoption_trail():
    released_limit = api_order(
        qty="100",
        order_type="limit",
        limit_price="36.00",
        status="canceled",
        submitted_at="2026-11-09T21:46:00Z",  # repaired turn 3: 16:46 EST, after the fill
    )
    client = down_window_client(released_limit)
    history = down_window_history()
    at_ns = ns(datetime(2026, 11, 9, 19, 30, tzinfo=ET))  # repaired: post-market, after 18:00
    manager = protector(client, history=history, now_ns=at_ns)
    start(manager)
    assert manager.emulated_trigger("AAPL") == Decimal("36.08"), {
        "trigger": str(manager.emulated_trigger("AAPL")),
        "requests": history.requests,
    }


def test_a_stale_quantity_cannot_still_be_reported_as_tape_ok_during_rearm():
    client = tonight_client()
    manager = protector(client, history=FakeHistory(), now_ns=POST_NS)
    manager.rebuild_tapes()
    client.positions = [api_position("155")]
    manager.reconcile()
    line = line_for(manager, POST_NS, "36.60")
    assert "tape=ok" not in line, line


@pytest.mark.parametrize("reaches_bound", [False, True], ids=["null-row", "truncated-null-page"])
def test_sdk_normalization_cannot_hide_a_malformed_or_truncated_window(monkeypatch, reaches_bound):
    rows = [quote_payload()] * 9_999 + [None] if reaches_bound else [None]
    payload = {"quotes": {"AAPL": rows}}
    if reaches_bound:
        payload["next_page_token"] = "unread-high-page"  # noqa: S105 - a page token  # type: ignore  # an untyped review probe, kept as written
    calls = stub_http(monkeypatch, [(200, payload)])
    data = StockHistoricalDataClient("fixture", "fixture")
    bound_transport(data, Decimal("3.05"))
    history = BHistoricalTape(PacedHistoricalDataClient(data, paced(1)), 10_000)  # type: ignore  # an untyped review probe, kept as written
    manager = protector(tonight_client(), history=history, now_ns=POST_NS)
    start(manager)
    line = line_for(manager, POST_NS, "36.60")
    assert "tape=partial" in line, {
        "raw_rows": len(rows),
        "wire_requests": len(calls),
        "unread_page": payload.get("next_page_token"),
        "state": line,
        "pages": [page.reason for page in manager.pages],
    }


def test_a_partial_start_read_keeps_breach_counting_and_silence_paging():
    client = tonight_client()
    trail_id = str(client.orders[0].id)
    manager = protector(client, history=FakeHistory(error=RuntimeError("fixture")), now_ns=POST_NS)
    start(manager)
    line_for(manager, POST_NS, "36.40")
    assert not manager.on_trade("AAPL", Decimal("36.40"), POST_NS)
    assert not manager.on_trade("AAPL", Decimal("36.40"), POST_NS + 500_000_000)
    assert manager.on_trade("AAPL", Decimal("36.40"), POST_NS + 1_000_000_000)
    assert client.cancelled == [trail_id]
    manager.watch(POST_NS + 6_000_000_000)
    assert [page.reason for page in manager.pages] == [
        "outside_rth_tape_unreadable",
        "outside_rth_feed_silent",
    ]


@pytest.fixture(autouse=True)
def bounded_offline_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden during the local review")

    def expired(signum, frame):
        raise AssertionError("the named test exceeded its 15-second review bound")

    def run_with_heartbeat(coroutine, *, debug=None, loop_factory=None):
        with asyncio.Runner(debug=debug, loop_factory=loop_factory) as runner:
            loop = runner.get_loop()

            def heartbeat():
                loop.call_soon(heartbeat)

            loop.call_soon(heartbeat)
            return runner.run(coroutine)

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(asyncio, "run", run_with_heartbeat)
    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)

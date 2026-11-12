"""Codex's turn-3 delta-property probes of #262 (repaired copies), under B's own names."""

import inspect
import io
import json
import time
from dataclasses import fields
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
    StalledHistory,
    api_order,
    api_position,
    b_account,
    five_held,
    line_for,
    ns,
    quote_at,
    tonight_client,
    tonight_history,
)
from test_b_tape_probe_regressions import bounded_offline_execution, quote_payload, stub_http  # noqa: F401
from urllib3.connection import HTTPSConnection

from sextant.execution.pacer import ActionKind
from sextant.paper.b_feed import BHistoricalTape, bound_transport
from sextant.paper.b_protector import BParameters, Protector
from sextant.paper.b_protector_main import PacedHistoricalDataClient, PacedTradingClient
from sextant.paper.sessions import session_start_ns


def parameters(**updates):
    values = {
        "native_trail_fraction": Decimal("0.025"),
        "reconcile_interval_s": Decimal("10"),
        "feed_poll_interval_s": Decimal("1"),
        "feed_silence_s": Decimal("5"),
        "tape_read_timeout_s": Decimal("3.05"),
        "tape_max_quotes": Decimal("10000"),
        "tape_deadline_s": Decimal("10"),
        "tape_max_requests": Decimal("8"),
    }
    values.update(updates)
    supported = {field.name for field in fields(BParameters)}
    result = BParameters(**{name: value for name, value in values.items() if name in supported})
    for name, value in values.items():
        if name not in supported:
            object.__setattr__(result, name, value)
    return result


def manager_for(client, history, *, at_ns=POST_NS, clock=None, params=None):
    options = {"now_ns": lambda: at_ns, "feed": "sip", "history": history}
    if clock is not None and "monotonic" in inspect.signature(Protector).parameters:
        options["monotonic"] = lambda: clock[0]
    return Protector(client, b_account(), parameters() if params is None else params, **options)


def test_g1_five_stalled_positions_finish_and_are_protected():
    clock = [0.0]
    client = five_held(clock, trading_s=1.0)
    history = StalledHistory(clock, wait_s=3.05)
    at_ns = ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET))
    manager = manager_for(client, history, at_ns=at_ns, clock=clock)
    manager.rebuild_tapes()
    observed = {"elapsed": clock[0], "requests": len(history.requests)}
    assert clock[0] <= 13.05, observed
    assert len({request[0] for request in history.requests}) == len(history.requests), observed
    assert len(manager._tapes) == 5
    manager.reconcile()
    assert all(
        manager.emulated_trigger(symbol) == Decimal("36.465")
        for symbol in ("AAPL", "MSFT", "NVDA", "ASTS", "TSLA")
    )
    assert clock[0] <= 13.05, {"elapsed_at_first_protection": clock[0], "bound": 13.05}


class PacedHistory(PacedTradingClient):
    def quote_page(self, symbol, start_ns, end_ns, feed, page_token=None):
        # The paged interface (R1): one admission per page request, as before per window.
        page = self._client.quote_page  # type: ignore[attr-defined]
        return self._call(ActionKind.QUERY, page, symbol, start_ns, end_ns, feed, page_token)


def test_g6_eight_requests_leave_the_first_reconcile_its_tokens():
    clock = [0.0]
    client = five_held(clock, trading_s=0.0)
    pacer = paced(18)
    manager = manager_for(
        PacedTradingClient(client, pacer),
        PacedHistory(FakeHistory(), pacer),  # type: ignore  # an untyped review probe, kept as written
        at_ns=ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET)),
        clock=clock,
    )
    manager.rebuild_tapes()
    observed = {
        "wire_requests": pacer.wire_requests,
        "tokens": pacer.bucket.tokens,
        "queued": len(pacer.queue),
    }
    assert pacer.wire_requests == 8 and pacer.bucket.tokens == 10, observed
    assert not pacer.queue, observed
    manager.reconcile()
    assert pacer.bucket.tokens == 8
    assert all(
        manager.emulated_trigger(symbol) == Decimal("36.465")
        for symbol in ("AAPL", "MSFT", "NVDA", "ASTS", "TSLA")
    )


def test_n2_only_the_house_trail_is_the_claim_on_identical_valid_times():
    client = FakeClient()
    client.positions = [api_position("100")]
    client.orders = [api_order(qty="100", stop_price="36.075", submitted_at="2026-11-09T23:00:00Z")]
    client.closed_orders = [
        api_order(
            client_order_id="owner-entry",
            qty="100",
            filled_qty="100",
            side="buy",
            order_type="market",
            status="filled",
            filled_at="2026-11-09T21:45:00Z",
        ),
        api_order(
            qty="100",
            order_type="limit",
            limit_price="36.00",
            status="canceled",
            submitted_at="2026-11-09T21:46:00Z",
        ),
    ]
    history = FakeHistory(
        [
            quote_at("40.00", datetime(2026, 11, 9, 17, 30, tzinfo=ET)),
            quote_at("37.00", datetime(2026, 11, 9, 18, 5, tzinfo=ET)),
        ]
    )
    manager = manager_for(client, history, at_ns=ns(datetime(2026, 11, 9, 18, 10, tzinfo=ET)))
    manager.rebuild_tapes()
    manager.reconcile()
    assert manager.emulated_trigger("AAPL") == Decimal("36.08"), {
        "trigger": manager.emulated_trigger("AAPL"),
        "requests": history.requests,
    }


def test_n1_r3_a_refused_second_page_is_partial_raises_and_keeps_protecting(monkeypatch):
    calls = stub_http(
        monkeypatch,
        [
            (200, {"quotes": {"AAPL": [quote_payload()]}, "next_page_token": "unread"}),
            (200, {"quotes": {"AAPL": [quote_payload()]}}),
        ],
    )
    data = StockHistoricalDataClient("fixture", "fixture")
    bound_transport(data, Decimal("3.05"))
    pacer = paced(1)
    history = BHistoricalTape(PacedHistoricalDataClient(data, pacer), 10000)  # type: ignore  # an untyped review probe, kept as written
    client = tonight_client()
    manager = manager_for(client, history)
    manager.rebuild_tapes()
    manager.reconcile()
    observed = {
        "http_requests": len(calls),
        "admissions": pacer.wire_requests,
        "tape": manager._tapes["AAPL"],
        "pages": manager.pages,
    }
    assert len(calls) == pacer.wire_requests == 1, observed
    assert manager._tapes["AAPL"].status == "partial", observed
    assert [page.reason for page in manager.pages] == ["outside_rth_tape_unreadable"], observed
    # R3 changes this one expectation: the page read (bid 38.70) raises the trigger to its
    # lower-bound level 38.70 x 0.975 = 37.7325 -> 37.73 instead of leaving the 36.465 stop.
    assert manager.emulated_trigger("AAPL") == Decimal("37.73")
    line_for(manager, POST_NS, "36.40")
    assert not manager.on_trade("AAPL", Decimal("36.40"), POST_NS)
    assert not manager.on_trade("AAPL", Decimal("36.40"), POST_NS + 500_000_000)
    assert manager.on_trade("AAPL", Decimal("36.40"), POST_NS + 1_000_000_000)
    assert client.cancelled


@pytest.mark.parametrize("status", ["partially_filled", "canceled", "expired"])
def test_g2_positive_partial_quantity_without_a_fill_time_is_entry_unknown(status):
    client = tonight_client()
    client.positions = [api_position("105")]
    client.orders = [api_order(qty="105", stop_price="36.465", submitted_at="2026-11-09T22:45:01Z")]
    client.closed_orders.append(
        api_order(
            client_order_id="owner-partial",
            qty="100",
            filled_qty="50",
            side="buy",
            order_type="limit",
            status=status,
            submitted_at="2026-11-09T22:45:00Z",
        )
    )
    history = tonight_history()
    manager = manager_for(client, history)
    manager.rebuild_tapes()
    manager.reconcile()
    assert manager._tapes["AAPL"].status == "entry_unknown", {
        "tape": manager._tapes["AAPL"],
        "trigger": manager.emulated_trigger("AAPL"),
    }
    assert not history.requests
    assert [page.reason for page in manager.pages] == ["outside_rth_entry_unknown"]
    assert manager.emulated_trigger("AAPL") == Decimal("36.465")


def test_g3_missing_stop_cannot_ignore_the_brokers_known_high_water_mark():
    client = tonight_client()
    client.orders = [
        api_order(qty="55", submitted_at="2026-11-09T17:47:18Z").model_copy(
            update={"hwm": "40.00", "trail_percent": "2.5"}
        )
    ]
    manager = manager_for(client, tonight_history())
    manager.rebuild_tapes()
    manager.reconcile()
    observed = {
        "broker_hwm": client.orders[0].hwm,
        "trail_percent": "2.5",
        "stop_price": client.orders[0].stop_price,
        "tape": manager._tapes["AAPL"],
        "trigger": manager.emulated_trigger("AAPL"),
        "pages": manager.pages,
    }
    assert manager.emulated_trigger("AAPL") >= Decimal("39.00"), observed  # type: ignore  # an untyped review probe, kept as written


def test_g8_a_regular_hours_add_immediately_invalidates_the_tape():
    client = tonight_client()
    manager = manager_for(client, tonight_history())
    manager.rebuild_tapes()
    manager.reconcile()
    assert "tape=ok" in line_for(manager, POST_NS, "36.60")
    manager._now_ns = lambda: ns(datetime(2026, 11, 10, 10, 0, tzinfo=ET))
    client.positions = [api_position("110")]
    previous = len(manager.states)
    manager.reconcile()
    observed = {"states": manager.states[previous:], "tapes": manager._tapes}
    assert "AAPL" not in manager._tapes, observed
    assert manager.states[previous:]
    assert not any("tape=ok" in line for line in manager.states[previous:]), observed


class TricklingBody(io.RawIOBase):
    def __init__(self, payload, clock, timeout, waits):
        self.payload = payload
        self.cursor = 0
        self.header_end = payload.index(b"\r\n\r\n") + 4
        self.clock, self.timeout, self.waits = clock, timeout, waits

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.cursor == len(self.payload):
            return 0
        if self.cursor < self.header_end:
            count = min(len(buffer), self.header_end - self.cursor)
        else:
            count = 1
            wait = self.timeout() / 2
            self.clock[0] += wait
            self.waits.append(wait)
        buffer[:count] = self.payload[self.cursor : self.cursor + count]
        self.cursor += count
        return count


class OfflineSocket:
    def __init__(self, payload, clock, timeout, waits):
        self.timeout = timeout
        self.body = TricklingBody(payload, clock, lambda: self.timeout, waits)

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def makefile(self, mode, buffering=None):
        return io.BufferedReader(self.body)

    def sendall(self, data):
        pass

    def close(self):
        pass


class WallClock:
    def __init__(self):
        self.began = time.monotonic()

    def __getitem__(self, index):
        return time.monotonic() - self.began

    def __setitem__(self, index, value):
        time.sleep(max(0, value - (time.monotonic() - self.began)))


@pytest.mark.parametrize(
    "realtime",
    [
        pytest.param(
            False,
            id="production-timeout-clock",
            marks=pytest.mark.xfail(
                strict=True,
                reason="RED since #262: ITIMER_REAL bounds real time, not this simulated clock; "
                "the G1 witness is the scaled-real-clock form (turn-3 brief, item 1)",
            ),
        ),
        pytest.param(True, id="scaled-real-clock"),
    ],
)
def test_g1_a_progressing_response_can_outlive_the_entire_start_deadline(monkeypatch, realtime):
    if realtime:
        # A Python signal handler runs "at the next bytecode instruction", never inside the
        # low-level handler (docs.python.org/3/library/signal.html, read 2026-11-10), so the
        # XNYS calendar's ~0.19 s first-use construction could not be interrupted if it
        # landed inside the 0.05 s window. Warm what the constructor uses before this case's
        # clock starts; the deadline, window and assertions stay untouched.
        session_start_ns(POST_NS)
    clock, waits = (WallClock() if realtime else [0.0]), []
    timeout_s = Decimal("0.03") if realtime else Decimal("3.05")
    deadline_s = Decimal("0.05") if realtime else Decimal("10")
    body = json.dumps({"quotes": {"AAPL": [quote_payload()]}}).encode()
    payload = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
    payload += str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body

    def connect(connection):
        connection.sock = OfflineSocket(payload, clock, connection.timeout, waits)
        connection.is_verified = True
        connection.proxy_is_verified = True

    monkeypatch.setattr(HTTPSConnection, "connect", connect)
    data = StockHistoricalDataClient("fixture", "fixture")
    data._session.trust_env = False
    bound_transport(data, timeout_s)
    history = BHistoricalTape(PacedHistoricalDataClient(data, paced(18)), 10000)  # type: ignore  # an untyped review probe, kept as written
    client = five_held(clock, trading_s=0.0 if realtime else 1.0)  # type: ignore  # an untyped review probe, kept as written
    manager = manager_for(
        client,
        history,
        clock=clock,
        params=parameters(tape_deadline_s=deadline_s, tape_read_timeout_s=timeout_s),
    )
    manager.rebuild_tapes()
    bound = float(deadline_s + timeout_s)
    observed = {
        "elapsed_before_protection": clock[0],
        "deadline_plus_one_timeout": bound,
        "body_reads": len(waits),
        "max_read_wait": max(waits, default=0),
        "pages": manager.pages,
    }
    assert waits and all(wait < float(timeout_s) for wait in waits), observed
    assert clock[0] <= bound, observed

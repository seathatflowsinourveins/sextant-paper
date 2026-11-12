"""B's outside-RTH path: emulated trigger, one release, and the extended-hours LIMIT.

Alpaca's native trailing stop rests outside regular hours but does not trigger, so B emulates
the trigger from quotes and releases one marketable LIMIT after a persistent run of eligible
trades (``docs/design.md``:31 and ``docs/adr/0005-exit-engine.md`` X4;
Alpaca orders page read 2026-11-09).
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order, Position, TradeAccount, TradeUpdate
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest

from sextant.paper.b_feed import QuoteObservation
from sextant.paper.b_protector import BParameters, HistoricalTape, Protector
from sextant.paper.config import PaperAccount
from sextant.paper.sessions import Session

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 9, 20, 30, tzinfo=ET)  # after-hours, outside the regular session
NOW_NS = int(NOW.timestamp()) * 1_000_000_000
SEC = 1_000_000_000
POST = datetime(2026, 11, 9, 18, 10, tzinfo=ET)  # the incident's post-session start
POST_NS = int(POST.timestamp()) * SEC
ENTRY = datetime(2026, 11, 9, 12, 47, 17, 479764, tzinfo=ET)  # the broker's filled entry
ENTRY_NS = int(ENTRY.timestamp()) * SEC + ENTRY.microsecond * 1_000


def b_account() -> PaperAccount:
    return PaperAccount(
        lane="alpaca-paper-b",
        account_id="paper-fixture",
        client_id="ALPACA-B",
        namespace="B001",
        sessions=(Session.RTH,),
    )


def b_params() -> BParameters:
    return BParameters(
        native_trail_fraction=Decimal("0.025"),
        reconcile_interval_s=Decimal("10"),
        feed_poll_interval_s=Decimal("1"),
        feed_silence_s=Decimal("5"),
        tape_deadline_s=Decimal("10"),
        tape_max_requests=Decimal("100"),
    )


def api_account(number: str = "paper-fixture") -> TradeAccount:
    return TradeAccount.model_validate(
        {
            "id": str(uuid4()),
            "account_number": number,
            "status": "ACTIVE",
            "currency": "USD",
            "buying_power": "100000",
            "cash": "100000",
            "portfolio_value": "100000",
            "equity": "100000",
        }
    )


def api_position(qty: str = "10", symbol: str = "AAPL") -> Position:
    return Position.model_validate(
        {
            "asset_id": str(uuid4()),
            "symbol": symbol,
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "100",
            "qty": qty,
            "side": "long",
            "cost_basis": "1000",
            "qty_available": qty,
        }
    )


def api_order(
    *,
    client_order_id: str = "B001-trail-1",
    symbol: str = "AAPL",
    qty: str = "10",
    filled_qty: str = "0",
    side: str = "sell",
    order_type: str = "trailing_stop",
    status: str = "new",
    stop_price: str | None = None,
    limit_price: str | None = None,
    extended_hours: bool = False,
    filled_at: str | None = None,
    submitted_at: str = "2026-11-09T23:59:00Z",
) -> Order:
    payload = {
        "id": str(uuid4()),
        "client_order_id": client_order_id,
        "created_at": submitted_at,
        "updated_at": submitted_at,
        "submitted_at": submitted_at,
        "order_class": "simple",
        "time_in_force": "gtc",
        "status": status,
        "extended_hours": extended_hours,
        "symbol": symbol,
        "qty": qty,
        "filled_qty": filled_qty,
        "side": side,
        "type": order_type,
    }
    if stop_price is not None:
        payload["stop_price"] = stop_price
    if limit_price is not None:
        payload["limit_price"] = limit_price
    if filled_at is not None:
        payload["filled_at"] = filled_at
    return Order.model_validate(payload)


def api_update(
    order: Order, event: str = "fill", at: datetime = NOW, position_qty: float | None = 10.0
) -> TradeUpdate:
    return TradeUpdate.model_validate(
        {
            "event": event,
            "execution_id": str(uuid4()),
            "order": order.model_dump(),
            "timestamp": at.isoformat(),
            "position_qty": position_qty,
        }
    )


class FakeClient:
    def __init__(self) -> None:
        self.account = api_account()
        self.positions: list[Position] = []
        self.orders: list[Order] = []
        self.closed_orders: list[Order] = []
        self.submitted: list[TrailingStopOrderRequest | LimitOrderRequest] = []
        self.echoes: list[Order] = []
        self.cancelled: list[str] = []
        self.calls: list[str] = []
        self.fail_limits = False

    def get_account(self) -> TradeAccount:
        self.calls.append("get_account")
        return self.account

    def get_all_positions(self) -> list[Position]:
        self.calls.append("get_all_positions")
        return list(self.positions)

    def get_orders(self, filter=None) -> list[Order]:
        self.calls.append("get_orders")
        status = getattr(filter, "status", None)
        if status == QueryOrderStatus.CLOSED:
            return list(self.closed_orders)
        if status == QueryOrderStatus.OPEN:
            return list(self.orders)
        return list(self.closed_orders) + list(self.orders)

    def get_order_by_client_id(self, client_id: str) -> Order:
        raise AssertionError("the outside-RTH proof never resolves a submit identity")

    def submit_order(self, order_data: object) -> object:
        self.calls.append("submit_order")
        assert isinstance(order_data, (TrailingStopOrderRequest, LimitOrderRequest))
        if self.fail_limits and isinstance(order_data, LimitOrderRequest):
            raise RuntimeError("the broker refused the limit")
        self.submitted.append(order_data)
        client_order_id = order_data.client_order_id or f"B001-echo-{len(self.echoes)}"
        if isinstance(order_data, LimitOrderRequest):
            echo = api_order(
                client_order_id=client_order_id,
                qty=str(int(order_data.qty or 0)),
                order_type="limit",
                limit_price=str(order_data.limit_price),
                extended_hours=True,
            )
        else:
            echo = api_order(
                client_order_id=client_order_id,
                qty=str(int(order_data.qty or 0)),
                order_type="trailing_stop",
            )
        self.echoes.append(echo)
        return echo

    def cancel_order_by_id(self, order_id: object) -> None:
        self.calls.append("cancel_order_by_id")
        self.cancelled.append(str(order_id))
        self.orders = [order for order in self.orders if str(order.id) != str(order_id)]


class FakeHistory:
    def __init__(
        self,
        observations: Sequence[QuoteObservation] = (),
        error: Exception | None = None,
        *,
        by_feed: dict[str, Sequence[QuoteObservation]] | None = None,
        fail_feeds: set[str] | None = None,
        page_size: int | None = None,
    ) -> None:
        self.observations = tuple(observations)
        self.page_size = page_size
        self.error = error
        self.by_feed = {key: tuple(value) for key, value in (by_feed or {}).items()}
        self.fail_feeds = set(fail_feeds or ())
        self.requests: list[tuple[str, int, int, str]] = []

    def quote_page(
        self, symbol: str, start_ns: int, end_ns: int, feed: str, page_token: str | None = None
    ) -> tuple[tuple[QuoteObservation, ...], str | None]:
        """One request: the window newest first, ``page_size`` rows a page, offset tokens."""

        self.requests.append((symbol, start_ns, end_ns, feed))
        if self.error is not None:
            raise self.error
        if feed in self.fail_feeds:
            raise RuntimeError(f"the {feed} tape is down")
        items = self.by_feed.get(feed, self.observations)
        window = sorted(
            (item for item in items if start_ns <= item.ts_ns <= end_ns),
            key=lambda item: -item.ts_ns,
        )
        offset = int(page_token or 0)
        size = self.page_size or max(len(window), 1)
        token = str(offset + size) if len(window) > offset + size else None
        return tuple(window[offset : offset + size]), token


def protector(
    client: FakeClient,
    *,
    history: HistoricalTape | None = None,
    now_ns: int = NOW_NS,
) -> Protector:
    return Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: now_ns,
        feed="sip",
        history=history,
    )


def drive_breach(p: Protector, *, bid: str = "99.505", trade: str = "99.40") -> None:
    """Feed one quote and three eligible prints that complete the persistent breach."""

    p.on_quote(
        "AAPL",
        bid=Decimal(bid),
        ask=Decimal(bid) + Decimal("0.005"),
        last=Decimal(bid) + Decimal("0.005"),
        ts_ns=NOW_NS,
    )
    p.note_poll(NOW_NS)
    assert p.on_trade("AAPL", Decimal(trade), NOW_NS) is False
    assert p.on_trade("AAPL", Decimal(trade), NOW_NS + SEC // 2) is False
    assert p.on_trade("AAPL", Decimal(trade), NOW_NS + SEC) is True


def test_outside_rth_arms_the_native_trail_instead_of_paging() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    p = protector(client)
    p.reconcile()
    assert p.pages == []
    assert len(client.submitted) == 1
    assert isinstance(client.submitted[0], TrailingStopOrderRequest)


def test_outside_rth_adopts_the_resting_trails_stop_price() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client)
    p.reconcile()
    assert p.pages == []
    assert client.submitted == []
    assert p.emulated_trigger("AAPL") == Decimal("97.50")


def test_a_persistent_breach_cancels_the_trail_then_releases_one_extended_hours_limit() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    p.on_quote(
        "AAPL", bid=Decimal("99.505"), ask=Decimal("99.51"), last=Decimal("99.51"), ts_ns=NOW_NS
    )
    p.note_poll(NOW_NS)
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC // 2) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC) is True
    assert client.cancelled == [str(trail.id)]
    assert client.submitted == []  # the LIMIT waits for the terminal cancellation
    p.handle(api_update(trail, event="canceled"))
    assert len(client.submitted) == 1
    request = client.submitted[0]
    assert isinstance(request, LimitOrderRequest)
    assert request.side is not None and request.side.value == "sell"
    assert request.time_in_force is not None and request.time_in_force.value == "gtc"
    assert request.extended_hours is True
    assert Decimal(str(request.limit_price)) == Decimal("99.50")
    assert Decimal(str(request.qty)) == Decimal("10")
    assert p.pages == []


def test_a_breach_without_a_fresh_quote_pages_and_keeps_the_trail() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="99.45")]
    p = protector(client)
    p.reconcile()
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.52"),
        last=Decimal("99.51"),
        ts_ns=NOW_NS - 3 * SEC,
    )
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC // 2) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC) is True
    assert client.cancelled == []
    assert [page.reason for page in p.pages].count("outside_rth_release_unpriced") == 1
    assert client.submitted == []


def test_a_breach_after_the_protector_arms_its_own_trail_releases_a_limit() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    p = protector(client)
    p.reconcile()
    assert len(client.submitted) == 1
    assert isinstance(client.submitted[0], TrailingStopOrderRequest)
    trail = client.echoes[-1]
    drive_breach(p, trade="96.90")
    assert client.cancelled == [str(trail.id)]
    assert client.submitted == [client.submitted[0]]  # the LIMIT waits for the terminal event
    p.handle(api_update(trail, event="canceled"))
    limits = [order for order in client.submitted if isinstance(order, LimitOrderRequest)]
    assert len(limits) == 1
    assert Decimal(str(limits[0].qty)) == Decimal("10")
    assert Decimal(str(limits[0].limit_price)) == Decimal("99.50")
    assert p.pages == []


def test_an_owner_add_outside_rth_resizes_after_the_terminal_cancel() -> None:
    client = FakeClient()
    client.positions = [api_position("12")]
    trail = api_order(qty="10", stop_price="97.50")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    assert client.cancelled == [str(trail.id)]
    assert client.submitted == []  # the old trail can still fill until its terminal event
    p.handle(api_update(trail, event="canceled"))
    assert len(client.submitted) == 1
    assert isinstance(client.submitted[0], TrailingStopOrderRequest)
    assert Decimal(str(client.submitted[0].qty)) == Decimal("12")
    assert p.pages == []


def test_closed_session_never_writes() -> None:
    saturday = datetime(2026, 11, 7, 12, 0, tzinfo=ET)
    client = FakeClient()
    client.positions = [api_position("10")]
    p = Protector(client, b_account(), b_params(), now_ns=lambda: int(saturday.timestamp()) * SEC)
    p.reconcile()
    assert client.submitted == []
    assert [page.reason for page in p.pages] == ["market_closed"]


def test_a_reconcile_while_the_cancel_is_pending_does_not_rearm_the_trail() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    p.on_quote(
        "AAPL", bid=Decimal("99.505"), ask=Decimal("99.51"), last=Decimal("99.51"), ts_ns=NOW_NS
    )
    p.note_poll(NOW_NS)
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC // 2) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC) is True
    assert client.cancelled == [str(trail.id)]

    # The broker has not delivered the terminal cancellation yet. Reconciling now must
    # wait for it rather than arm a second trail beside the release.
    p.reconcile()

    assert client.submitted == []
    assert p.pages == []


def test_the_released_limit_price_crosses_to_the_sdk_without_a_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    real_request = LimitOrderRequest

    def spy(**kwargs: object) -> LimitOrderRequest:
        captured.update(kwargs)
        return real_request(**kwargs)

    monkeypatch.setattr("sextant.paper.b_protector.LimitOrderRequest", spy)
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    drive_breach(p)
    p.handle(api_update(trail, event="canceled"))

    assert isinstance(captured["limit_price"], str)
    assert Decimal(captured["limit_price"]) == Decimal("99.50")


def test_the_released_limit_uses_the_fresh_unreserved_residual() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    drive_breach(p)
    # The owner sells four shares while the cancellation is in flight; reconcile refreshes
    # the position, so the release may sell only the six shares still unreserved.
    client.positions = [api_position("6")]
    p.reconcile()
    p.handle(api_update(trail, event="canceled"))
    limits = [order for order in client.submitted if isinstance(order, LimitOrderRequest)]
    assert len(limits) == 1
    assert Decimal(str(limits[0].qty)) == Decimal("6")
    assert p.pages == []


def test_a_failed_release_submit_rearms_the_emulation_state() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    drive_breach(p)
    client.fail_limits = True
    p.handle(api_update(trail, event="canceled"))
    assert [page.reason for page in p.pages] == ["outside_limit_submit_failed"]

    # The failure re-armed the state, so the next quote can move the trigger again.
    moved = p.on_quote(
        "AAPL",
        bid=Decimal("110.00"),
        ask=Decimal("110.02"),
        last=Decimal("110.01"),
        ts_ns=NOW_NS + 2 * SEC,
    )
    assert moved == Decimal("107.25")


def test_a_delayed_cancel_for_another_trail_keeps_the_pending_release() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    earlier = api_order(client_order_id="B001-trail-earlier", stop_price="99.00")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    drive_breach(p)
    # A resize's trail cancels late; its terminal event must not drop the breach's pending
    # release for the trail that is actually resting.
    p.handle(api_update(earlier, event="canceled"))
    assert client.submitted == []
    p.handle(api_update(trail, event="canceled"))
    limits = [order for order in client.submitted if isinstance(order, LimitOrderRequest)]
    assert len(limits) == 1
    assert Decimal(str(limits[0].qty)) == Decimal("10")


def test_the_release_price_is_refreshed_when_the_cancellation_completes() -> None:
    clock = {"ns": NOW_NS}
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = Protector(client, b_account(), b_params(), now_ns=lambda: clock["ns"], feed="sip")
    p.reconcile()
    drive_breach(p)
    clock["ns"] = NOW_NS + SEC
    p.on_quote(
        "AAPL",
        bid=Decimal("98.00"),
        ask=Decimal("98.02"),
        last=Decimal("98.01"),
        ts_ns=clock["ns"],
    )
    p.note_poll(clock["ns"])
    p.handle(api_update(trail, event="canceled"))
    limits = [order for order in client.submitted if isinstance(order, LimitOrderRequest)]
    assert len(limits) == 1
    assert Decimal(str(limits[0].limit_price)) == Decimal("98.00")


def test_a_cancelled_released_limit_rearms_the_outside_emulation() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = protector(client)
    p.reconcile()
    drive_breach(p)
    p.handle(api_update(trail, event="canceled"))
    limit = client.submitted[-1]
    assert isinstance(limit, LimitOrderRequest)
    released = client.echoes[-1]
    client.orders = [released]

    # Two more shares arrive, so the released ten-share LIMIT no longer covers the
    # position; the outside resize cancels it and re-arms the native trail.
    client.positions = [api_position("12")]
    p.reconcile()
    assert client.cancelled[-1] == str(released.id)
    assert [type(order) for order in client.submitted] == [LimitOrderRequest]
    p.handle(api_update(released, event="canceled"))
    trails = [order for order in client.submitted if isinstance(order, TrailingStopOrderRequest)]
    assert len(trails) == 1
    assert Decimal(str(trails[0].qty)) == Decimal("12")

    # The replacement trail rests outside RTH, so the emulated trigger must still
    # accept quotes; otherwise the position has no active exit.
    moved = p.on_quote(
        "AAPL",
        bid=Decimal("110.00"),
        ask=Decimal("110.02"),
        last=Decimal("110.01"),
        ts_ns=NOW_NS + 2 * SEC,
    )
    assert moved == Decimal("107.25")


def test_the_0930_handover_snapshot_submits_only_the_released_limit() -> None:
    clock = {
        "ns": int(datetime(2026, 11, 9, 9, 29, 58, 500000, tzinfo=ET).timestamp()) * 1_000_000_000
    }
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = Protector(client, b_account(), b_params(), now_ns=lambda: clock["ns"], feed="sip")
    p.reconcile()

    p.on_quote(
        "AAPL",
        bid=Decimal("99.505"),
        ask=Decimal("99.510"),
        last=Decimal("99.510"),
        ts_ns=clock["ns"],
    )
    p.note_poll(clock["ns"])
    assert p.on_trade("AAPL", Decimal("99.40"), clock["ns"]) is False
    assert p.on_trade("AAPL", Decimal("99.40"), clock["ns"] + SEC // 2) is False
    assert p.on_trade("AAPL", Decimal("99.40"), clock["ns"] + SEC) is True

    # The trail's terminal cancellation arrives just after the regular session opens.
    # The handover's internal reconcile must not arm a second full-position exit
    # beside the LIMIT it is about to submit.
    clock["ns"] = int(datetime(2026, 11, 9, 9, 30, 0, 300000, tzinfo=ET).timestamp())
    clock["ns"] *= 1_000_000_000
    p.handle(api_update(trail, event="canceled"))

    exits = client.submitted
    assert len(exits) == 1
    assert isinstance(exits[0], LimitOrderRequest)
    assert Decimal(str(exits[0].qty)) == Decimal("10")
    assert Decimal(str(exits[0].limit_price)) == Decimal("99.50")


def ns(at: datetime) -> int:
    return int(at.timestamp()) * SEC + at.microsecond * 1_000


def quote_at(bid: str, at: datetime) -> QuoteObservation:
    value = Decimal(bid)
    return QuoteObservation("AAPL", value, value + Decimal("0.01"), None, ns(at))


CLAIM = datetime(2026, 11, 9, 12, 47, 18, tzinfo=ET)  # B's first house trail after the fill
CLAIM_NS = ns(CLAIM)
CLOSE_NS = ns(datetime(2026, 11, 9, 16, 0, tzinfo=ET))  # only outside-hours spans are read (G5)


def tonight_client() -> FakeClient:
    client = FakeClient()
    client.positions = [api_position("55")]
    client.orders = [api_order(qty="55", stop_price="36.465", submitted_at="2026-11-09T17:47:18Z")]
    client.closed_orders = [
        api_order(
            client_order_id="owner-entry-1",
            qty="55",
            filled_qty="55",
            side="buy",
            order_type="market",
            status="filled",
            filled_at="2026-11-09T17:47:17.479764Z",
            submitted_at="2026-11-09T17:47:16Z",
        )
    ]
    return client


def tonight_history() -> FakeHistory:
    return FakeHistory(
        [
            quote_at("38.70", datetime(2026, 11, 9, 16, 59, tzinfo=ET)),
            quote_at("36.99", CLAIM),
            quote_at("36.60", datetime(2026, 11, 9, 18, 9, tzinfo=ET)),
        ]
    )


def start(p: Protector) -> None:
    """The stream's process start: the one tape read, then the first reconcile."""

    p.rebuild_tapes()
    p.reconcile()


def line_for(p: Protector, at_ns: int, bid: str) -> str:
    p.note_poll(at_ns)
    p.on_quote("AAPL", bid=Decimal(bid), ask=Decimal(bid) + Decimal("0.01"), last=None, ts_ns=at_ns)
    p.watch()
    return p.states[-1]


def test_a_start_rebuilds_the_after_hours_high_water_mark_from_the_tape() -> None:
    client = tonight_client()
    history = tonight_history()
    p = protector(client, history=history, now_ns=POST_NS)
    start(p)

    assert p.pages == []
    assert client.submitted == []
    assert p.emulated_trigger("AAPL") == Decimal("37.73")
    assert history.requests == [("AAPL", CLOSE_NS, POST_NS, "sip")]
    line = line_for(p, POST_NS, "36.60")
    assert "tape=ok" in line and "trigger=37.73" in line


def test_a_restart_mid_session_keeps_the_rebuilt_level_and_never_lowers_it() -> None:
    client = tonight_client()
    first = protector(client, history=tonight_history(), now_ns=POST_NS)
    start(first)
    assert first.emulated_trigger("AAPL") == Decimal("37.73")

    second = protector(client, history=tonight_history(), now_ns=POST_NS)
    start(second)
    assert second.pages == []
    assert second.emulated_trigger("AAPL") == Decimal("37.73")

    # A later native trail at a lower stop must not lower the rebuilt level.
    client.orders = [api_order(qty="55", stop_price="36.00")]
    second.reconcile()
    assert second.emulated_trigger("AAPL") == Decimal("37.73")


def test_the_tape_is_read_once_at_start_and_never_in_the_running_loop() -> None:
    client = tonight_client()
    history = tonight_history()
    clock = [POST_NS]
    p = Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: clock[0],
        feed="sip",
        history=history,
    )
    start(p)
    assert len(history.requests) == 1

    clock[0] = ns(datetime(2026, 11, 9, 20, 5, tzinfo=ET))
    p.set_feed("boats")
    p.reconcile()
    p.watch()
    assert len(history.requests) == 1
    assert p.emulated_trigger("AAPL") == Decimal("37.73")


def test_an_unreadable_tape_pages_once_and_keeps_the_native_stop() -> None:
    client = tonight_client()
    history = FakeHistory(error=RuntimeError("the tape is down"))
    p = protector(client, history=history, now_ns=POST_NS)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("36.465")
    assert [page.reason for page in p.pages] == ["outside_rth_tape_unreadable"]
    p.reconcile()
    assert [page.reason for page in p.pages] == ["outside_rth_tape_unreadable"]
    line = line_for(p, POST_NS, "36.60")
    assert "tape=partial" in line and "trigger=36.465" in line
    assert client.cancelled == []


def test_an_unknown_entry_is_its_own_status_and_page() -> None:
    client = FakeClient()
    client.positions = [api_position("55")]
    client.orders = [api_order(qty="55", stop_price="36.465")]
    history = tonight_history()
    p = protector(client, history=history, now_ns=POST_NS)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("36.465")
    assert [page.reason for page in p.pages] == ["outside_rth_entry_unknown"]
    assert history.requests == []
    line = line_for(p, POST_NS, "36.60")
    assert "tape=entry_unknown" in line and "trigger=36.465" in line


def test_a_restart_at_nine_pm_reads_every_window_on_its_own_feed() -> None:
    client = tonight_client()
    sip_high = quote_at("38.65", datetime(2026, 11, 9, 16, 59, tzinfo=ET))
    boats_high = quote_at("37.00", datetime(2026, 11, 9, 20, 30, tzinfo=ET))
    history = FakeHistory(by_feed={"sip": [sip_high], "boats": [boats_high]})
    at_ns = ns(datetime(2026, 11, 9, 21, 0, tzinfo=ET))
    p = protector(client, history=history, now_ns=at_ns)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("37.68")
    assert p.pages == []
    sip_end_ns = ns(datetime(2026, 11, 9, 20, 0, tzinfo=ET))
    assert history.requests == [
        ("AAPL", CLOSE_NS, sip_end_ns, "sip"),
        ("AAPL", sip_end_ns, at_ns, "boats"),
    ]
    line = line_for(p, at_ns, "37.00")
    assert "tape=ok" in line and "trigger=37.68" in line


def down_window_client(*house: Order) -> FakeClient:
    client = FakeClient()
    client.positions = [api_position("100")]
    client.orders = [api_order(qty="100", stop_price="36.075", submitted_at="2026-11-09T23:00:00Z")]
    client.closed_orders = [
        api_order(
            client_order_id="owner-entry-1",
            qty="100",
            filled_qty="100",
            side="buy",
            order_type="market",
            status="filled",
            filled_at="2026-11-09T21:45:00Z",
            submitted_at="2026-11-09T21:40:00Z",
        ),
        *house,
    ]
    return client


def down_window_history() -> FakeHistory:
    return FakeHistory(
        by_feed={
            "sip": [
                quote_at("40.00", datetime(2026, 11, 9, 17, 30, tzinfo=ET)),
                quote_at("37.00", datetime(2026, 11, 9, 18, 10, tzinfo=ET)),
            ]
        }
    )


def test_an_owner_fill_while_b_is_down_is_adopted_at_bs_first_claim() -> None:
    client = down_window_client()
    history = down_window_history()
    at_ns = ns(datetime(2026, 11, 9, 19, 30, tzinfo=ET))
    p = protector(client, history=history, now_ns=at_ns)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("36.08")
    assert p.pages == []
    claim_ns = ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET))
    assert history.requests == [("AAPL", claim_ns, at_ns, "sip")]


def test_a_house_trail_placed_while_the_owner_buy_rested_is_not_the_adoption() -> None:
    # F3: a re-arm at 16:42 while the 16:40 buy rested; B was down at the 16:45 fill.
    rearm = api_order(
        qty="50", stop_price="35.00", status="canceled", submitted_at="2026-11-09T21:42:00Z"
    )
    client = down_window_client(rearm)
    history = down_window_history()
    at_ns = ns(datetime(2026, 11, 9, 19, 30, tzinfo=ET))
    p = protector(client, history=history, now_ns=at_ns)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("36.08")
    claim_ns = ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET))
    assert history.requests == [("AAPL", claim_ns, at_ns, "sip")]


def test_an_owner_add_is_adopted_at_the_add_and_excludes_the_earlier_high() -> None:
    client = FakeClient()
    client.positions = [api_position("200")]
    client.orders = [api_order(qty="200", stop_price="11.456", submitted_at="2026-11-09T23:30:01Z")]
    client.closed_orders = [
        api_order(
            client_order_id="owner-entry-1",
            qty="100",
            filled_qty="100",
            side="buy",
            order_type="market",
            status="filled",
            filled_at="2026-11-09T22:00:00Z",
            submitted_at="2026-11-09T21:55:00Z",
        ),
        api_order(
            client_order_id="owner-add-1",
            qty="100",
            filled_qty="100",
            side="buy",
            order_type="market",
            status="filled",
            filled_at="2026-11-09T23:30:00Z",
            submitted_at="2026-11-09T23:25:00Z",
        ),
    ]
    history = FakeHistory(
        by_feed={
            "sip": [
                quote_at("12.00", datetime(2026, 11, 9, 18, 0, tzinfo=ET)),
                quote_at("11.60", datetime(2026, 11, 9, 18, 45, tzinfo=ET)),
                quote_at("11.70", datetime(2026, 11, 9, 19, 30, tzinfo=ET)),
            ]
        }
    )
    at_ns = ns(datetime(2026, 11, 9, 19, 45, tzinfo=ET))
    p = protector(client, history=history, now_ns=at_ns)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("11.456")
    assert p.pages == []
    add_claim_ns = ns(datetime(2026, 11, 9, 18, 30, 1, tzinfo=ET))
    assert history.requests == [("AAPL", add_claim_ns, at_ns, "sip")]


def test_a_read_for_another_quantity_is_never_applied() -> None:
    # F4: the start read covers 100 shares; the owner adds 100 before the first reconcile.
    client = FakeClient()
    client.positions = [api_position("100")]
    client.orders = [api_order(qty="100", stop_price="11.00", submitted_at="2026-11-09T15:00:01Z")]
    entry = api_order(
        client_order_id="owner-entry-1",
        qty="100",
        filled_qty="100",
        side="buy",
        order_type="market",
        status="filled",
        filled_at="2026-11-09T15:00:00Z",
        submitted_at="2026-11-09T14:55:00Z",
    )
    client.closed_orders = [entry]
    history = FakeHistory(
        by_feed={"sip": [quote_at("12.00", datetime(2026, 11, 9, 11, 0, tzinfo=ET))]}
    )
    at_ns = ns(datetime(2026, 11, 9, 16, 0, 5, tzinfo=ET))
    p = protector(client, history=history, now_ns=at_ns)
    p.rebuild_tapes()

    client.positions = [api_position("200")]
    client.orders = [api_order(qty="200", stop_price="11.456", submitted_at="2026-11-09T21:00:04Z")]
    p.reconcile()

    assert p.emulated_trigger("AAPL") == Decimal("11.456")
    assert "tape=ok" not in line_for(p, at_ns, "11.50")


def test_a_failed_tape_window_reports_partial_keeps_the_stop_and_is_not_reread() -> None:
    client = tonight_client()
    sip_high = quote_at("38.65", datetime(2026, 11, 9, 16, 59, tzinfo=ET))
    boats_high = quote_at("37.00", datetime(2026, 11, 9, 20, 30, tzinfo=ET))
    history = FakeHistory(by_feed={"sip": [sip_high], "boats": [boats_high]}, fail_feeds={"sip"})
    at_ns = ns(datetime(2026, 11, 9, 21, 0, tzinfo=ET))
    p = protector(client, history=history, now_ns=at_ns)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("36.465")
    assert [page.reason for page in p.pages] == ["outside_rth_tape_unreadable"]
    line = line_for(p, at_ns, "36.60")
    assert "tape=partial" in line and "trigger=36.465" in line
    history.fail_feeds.clear()
    p.reconcile()
    assert p.emulated_trigger("AAPL") == Decimal("36.465")
    assert len(history.requests) == 1  # G1: the symbol stops at its first failure


def test_without_a_house_trail_after_the_fill_b_adopts_now_and_claims_no_tape() -> None:
    # F6: nothing was read, so the line must not say tape=ok.
    client = tonight_client()
    client.orders = []
    history = tonight_history()
    p = protector(client, history=history, now_ns=POST_NS)
    p.rebuild_tapes()

    assert history.requests == []
    assert p.pages == []
    client.orders = [api_order(qty="55", stop_price="36.465", submitted_at="2026-11-09T23:10:01Z")]
    p.reconcile()
    line = line_for(p, POST_NS, "36.60")
    assert "tape=" not in line and "trigger=36.465" in line, line


def test_a_partially_filled_add_without_a_fill_time_makes_the_entry_unknown() -> None:
    # G2: Alpaca documents no fill time for a partial fill, so a later partial add is unknown.
    client = FakeClient()
    client.positions = [api_position("160")]
    client.orders = [api_order(qty="160", stop_price="35.10", submitted_at="2026-11-09T15:30:01Z")]
    client.closed_orders = [
        api_order(
            client_order_id="owner-entry-1",
            qty="100",
            filled_qty="100",
            side="buy",
            order_type="market",
            status="filled",
            filled_at="2026-11-09T14:30:00Z",
            submitted_at="2026-11-09T14:29:59Z",
        ),
        api_order(
            client_order_id="B001-trail-old",
            qty="100",
            status="canceled",
            submitted_at="2026-11-09T14:30:01Z",
        ),
        api_order(
            client_order_id="owner-add-1",
            qty="100",
            filled_qty="60",
            side="buy",
            order_type="limit",
            limit_price="36.00",
            status="expired",
            submitted_at="2026-11-09T15:30:00Z",
        ),
    ]
    history = FakeHistory(
        by_feed={"sip": [quote_at("40.00", datetime(2026, 11, 9, 10, 0, tzinfo=ET))]}
    )
    at_ns = ns(datetime(2026, 11, 9, 17, 0, tzinfo=ET))
    p = protector(client, history=history, now_ns=at_ns)
    start(p)

    assert history.requests == []
    assert [page.reason for page in p.pages] == ["outside_rth_entry_unknown"]
    assert p.emulated_trigger("AAPL") == Decimal("35.10")


def test_a_partial_buy_last_updated_before_the_latest_fill_keeps_that_fill() -> None:
    old_partial = api_order(
        client_order_id="owner-old-partial",
        qty="100",
        filled_qty="50",
        side="buy",
        order_type="limit",
        limit_price="30.00",
        status="canceled",
        submitted_at="2026-11-06T15:00:00Z",
    )
    client = tonight_client()
    client.closed_orders.append(old_partial)
    history = tonight_history()
    p = protector(client, history=history, now_ns=POST_NS)
    start(p)

    assert p.pages == []
    assert p.emulated_trigger("AAPL") == Decimal("37.73")


def test_an_add_in_regular_hours_drops_the_start_reads_tape_status() -> None:
    # G8: the start read covered 55 shares; nothing was read for 110.
    client = tonight_client()
    clock = [POST_NS]
    p = Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: clock[0],
        feed="sip",
        history=tonight_history(),
    )
    start(p)
    assert "tape=ok" in line_for(p, POST_NS, "36.60")

    clock[0] = ns(datetime(2026, 11, 10, 10, 0, tzinfo=ET))
    client.positions = [api_position("110")]
    seen = len(p.states)
    p.reconcile()
    assert p.states[seen:]
    assert not any("tape=ok" in line for line in p.states[seen:])


def test_a_missing_native_stop_seeds_the_trigger_from_an_ok_tape() -> None:
    # G3: the tape is the only record of the earlier high when the broker shows no stop.
    client = tonight_client()
    client.orders = [api_order(qty="55", submitted_at="2026-11-09T17:47:18Z")]
    p = protector(client, history=tonight_history(), now_ns=POST_NS)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("37.73")
    assert "outside_rth_trigger_missing" in [page.reason for page in p.pages]


def test_a_missing_native_stop_keeps_the_brokers_own_high_water_mark() -> None:
    # G3 (turn 3): the maximum over every valid source; hwm 40.00 at 2.5% implies 39.00.
    client = tonight_client()
    client.orders = [
        api_order(qty="55", submitted_at="2026-11-09T17:47:18Z").model_copy(
            update={"hwm": "40.00", "trail_percent": "2.5"}
        )
    ]
    p = protector(client, history=tonight_history(), now_ns=POST_NS)
    start(p)

    assert p.emulated_trigger("AAPL") == Decimal("39.00")
    assert "outside_rth_trigger_missing" in [page.reason for page in p.pages]


class StalledHistory(FakeHistory):
    """A data API whose every request waits out its transport timeout, then fails."""

    def __init__(self, clock: list[float], wait_s: float) -> None:
        super().__init__()
        self.clock, self.wait_s = clock, wait_s

    def quote_page(
        self, symbol: str, start_ns: int, end_ns: int, feed: str, page_token: str | None = None
    ) -> tuple[tuple[QuoteObservation, ...], str | None]:
        self.requests.append((symbol, start_ns, end_ns, feed))
        self.clock[0] += self.wait_s
        raise TimeoutError("read timed out")


def five_held(clock: list[float], trading_s: float) -> FakeClient:
    symbols = ("AAPL", "MSFT", "NVDA", "ASTS", "TSLA")

    class Slow(FakeClient):
        def get_orders(self, filter=None) -> list[Order]:
            clock[0] += trading_s
            wanted = getattr(filter, "symbols", None)
            return [o for o in super().get_orders(filter) if wanted is None or o.symbol in wanted]

    client = Slow()
    client.positions = [api_position("55", symbol) for symbol in symbols]
    for symbol in symbols:
        client.orders.append(
            api_order(
                symbol=symbol, qty="55", stop_price="36.465", submitted_at="2026-11-06T21:10:01Z"
            )
        )
        client.closed_orders.append(
            api_order(
                client_order_id=f"owner-{symbol}",
                symbol=symbol,
                qty="55",
                filled_qty="55",
                side="buy",
                order_type="market",
                status="filled",
                filled_at="2026-11-06T21:10:00Z",
            )
        )
    return client


def test_five_positions_against_a_stalled_data_api_are_protected_inside_the_deadline() -> None:
    # G1: one monotonic deadline over the whole start read; each symbol stops at its first failure.
    clock = [0.0]
    client = five_held(clock, trading_s=1.0)
    history = StalledHistory(clock, wait_s=3.05)
    at_ns = ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET))  # Monday post, held since Friday post
    params = replace(b_params(), tape_read_timeout_s=Decimal("3.05"))
    p = Protector(
        client,
        b_account(),
        params,
        now_ns=lambda: at_ns,
        feed="sip",
        history=history,
        monotonic=lambda: clock[0],
    )
    p.rebuild_tapes()

    assert clock[0] <= 10  # a call starts only while one timeout fits before the deadline
    assert len({request[0] for request in history.requests}) == len(history.requests) <= 3
    assert all(status == "partial" for status in (p._tapes[s].status for s in p._tapes))
    assert len(p._tapes) == 5
    p.reconcile()
    for symbol in ("AAPL", "MSFT", "NVDA", "ASTS", "TSLA"):
        assert p.emulated_trigger(symbol) == Decimal("36.465")
    assert clock[0] <= 10 + 3.05  # turn 3: protecting through the first reconcile


class SleepingHistory(FakeHistory):
    """A data API that keeps the socket busy in real time, longer than any deadline."""

    def quote_page(
        self, symbol: str, start_ns: int, end_ns: int, feed: str, page_token: str | None = None
    ) -> tuple[tuple[QuoteObservation, ...], str | None]:
        self.requests.append((symbol, start_ns, end_ns, feed))
        for _ in range(500):
            time.sleep(0.01)  # each wait inside any read timeout; only an elapsed bound ends it
        return (), None


def test_the_interval_timer_ends_a_progressing_start_read_at_its_deadline() -> None:
    # G1 (turn 3): signal.setitimer(ITIMER_REAL) bounds the whole read in real time.
    client = five_held([0.0], trading_s=0.0)
    params = replace(b_params(), tape_deadline_s=Decimal("0.2"))
    at_ns = ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET))
    p = Protector(
        client, b_account(), params, now_ns=lambda: at_ns, feed="sip", history=SleepingHistory()
    )

    def outer(signum: int, frame: object) -> None:
        raise AssertionError("the outer alarm fired")

    previous = signal.signal(signal.SIGALRM, outer)
    prior = signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        began = time.monotonic()
        p.rebuild_tapes()
        elapsed = time.monotonic() - began
        restored = signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)[0]
    finally:
        signal.setitimer(signal.ITIMER_REAL, *prior)
        signal.signal(signal.SIGALRM, previous)

    assert elapsed < 1.0
    assert restored[0] is outer and 28 < restored[1] <= 30  # handler and timer restored
    assert [p._tapes[s].status for s in sorted(p._tapes)] == ["partial"] * 5
    assert [page.reason for page in p.pages] == ["outside_rth_tape_unreadable"] * 5
    p.reconcile()
    assert p.emulated_trigger("AAPL") == Decimal("36.465")


def test_off_the_main_thread_the_start_read_reads_nothing_and_pages_once() -> None:
    client = five_held([0.0], trading_s=0.0)
    history = FakeHistory()
    p = Protector(
        client, b_account(), b_params(), now_ns=lambda: POST_NS, feed="sip", history=history
    )
    worker = threading.Thread(target=p.rebuild_tapes)
    worker.start()
    worker.join(5)

    assert not history.requests and not client.calls and not p._tapes
    assert [page.reason for page in p.pages] == ["outside_rth_tape_unreadable"]


def test_the_start_read_stops_at_its_request_budget() -> None:
    # G6: the start read spends at most tape_max_requests, leaving the rest to the reconcile.
    clock = [0.0]
    client = five_held(clock, trading_s=0.0)
    history = FakeHistory(error=None)
    at_ns = ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET))
    params = BParameters(
        native_trail_fraction=Decimal("0.025"),
        reconcile_interval_s=Decimal("10"),
        feed_poll_interval_s=Decimal("1"),
        feed_silence_s=Decimal("5"),
        tape_deadline_s=Decimal("10"),
        tape_max_requests=Decimal("8"),
    )
    p = Protector(client, b_account(), params, now_ns=lambda: at_ns, feed="sip", history=history)
    p.rebuild_tapes()

    # Held since Friday post: four outside-hours windows each, so one order read and four
    # windows per symbol; the eighth request is the second symbol's first window.
    assert len(client.calls) + len(history.requests) == 8
    assert [tape.status for tape in p._tapes.values()] == ["ok"] + ["partial"] * 4
    # R1-R3: the detail now also says how many pages were read before the budget ended.
    assert "start read over its deadline or budget" in p._tapes["MSFT"].detail
    assert p._tapes["MSFT"].detail.startswith("1 page(s) read")
    assert history.requests[0][3] == "sip" and len(history.requests) == 5


def test_only_the_outside_hours_spans_since_adoption_are_read() -> None:
    # G5: regular-hours and closed spans are never requested; the native stop carries RTH.
    from sextant.paper.b_protector import tape_windows

    friday_post = ns(datetime(2026, 11, 6, 16, 10, tzinfo=ET))
    monday_post = ns(datetime(2026, 11, 9, 18, 0, tzinfo=ET))
    at = [ns(datetime(2026, 11, d, h, m, tzinfo=ET)) for d, h, m in ((6, 20, 0), (8, 20, 0))]
    monday = [ns(datetime(2026, 11, 9, h, m, tzinfo=ET)) for h, m in ((4, 0), (9, 30), (16, 0))]
    assert tape_windows(friday_post, monday_post) == (
        (friday_post, at[0], "sip"),
        (at[1], monday[0], "boats"),
        (monday[0], monday[1], "sip"),
        (monday[2], monday_post, "sip"),
    )


def test_a_start_in_regular_hours_reads_and_applies_at_the_first_outside_adoption() -> None:
    # G7: B restarted at 10:00 Tuesday holds a position whose post-market high it saw Monday.
    client = tonight_client()
    history = tonight_history()
    clock = [ns(datetime(2026, 11, 10, 10, 0, tzinfo=ET))]
    p = Protector(
        client, b_account(), b_params(), now_ns=lambda: clock[0], feed="sip", history=history
    )
    start(p)
    assert history.requests[0][1] == CLOSE_NS
    assert p.emulated_trigger("AAPL") is None

    clock[0] = ns(datetime(2026, 11, 10, 16, 0, 1, tzinfo=ET))
    p.reconcile()
    assert p.emulated_trigger("AAPL") == Decimal("37.73")


def test_a_start_in_a_closed_session_still_reads_the_tape() -> None:
    # G7: a Saturday restart reads Friday's outside-hours spans, applied at Sunday 20:00.
    client = tonight_client()
    client.orders = [api_order(qty="55", stop_price="36.465", submitted_at="2026-11-06T21:00:01Z")]
    client.closed_orders[0] = api_order(
        client_order_id="owner-entry-1",
        qty="55",
        filled_qty="55",
        side="buy",
        order_type="market",
        status="filled",
        filled_at="2026-11-06T21:00:00Z",
    )
    history = FakeHistory([quote_at("38.70", datetime(2026, 11, 6, 17, 0, tzinfo=ET))])
    clock = [ns(datetime(2026, 11, 7, 11, 0, tzinfo=ET))]
    p = Protector(
        client, b_account(), b_params(), now_ns=lambda: clock[0], feed="sip", history=history
    )
    start(p)
    assert history.requests
    assert [page.reason for page in p.pages] == ["market_closed"]

    clock[0] = ns(datetime(2026, 11, 8, 20, 0, 1, tzinfo=ET))
    p.reconcile()
    assert p.emulated_trigger("AAPL") == Decimal("37.73")

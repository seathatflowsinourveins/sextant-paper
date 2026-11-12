"""The state line B writes at startup, on a session change, and on a custody change.

The line is the only outside-RTH witness: it names the session mode, the holding, the
protected quantity, the trigger reference and its basis, the resting house order, and the
market-data feed's age. It is a log line, not a trading decision (clean B PR-6).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from alpaca.trading.models import Order, Position, TradeAccount, TradeUpdate
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest
from alpaca.trading.stream import TradingStream

from sextant.paper.b_protector import BParameters, Protector, ProtectorStream
from sextant.paper.config import PaperAccount
from sextant.paper.sessions import Session

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 9, 20, 30, tzinfo=ET)  # overnight, outside the regular session
NOW_NS = int(NOW.timestamp()) * 1_000_000_000
SEC = 1_000_000_000


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
) -> Order:
    payload = {
        "id": str(uuid4()),
        "client_order_id": client_order_id,
        "created_at": "2026-11-09T23:59:00Z",
        "updated_at": "2026-11-09T23:59:00Z",
        "submitted_at": "2026-11-09T23:59:00Z",
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
        self.submitted: list[TrailingStopOrderRequest | LimitOrderRequest] = []
        self.echoes: list[Order] = []
        self.cancelled: list[str] = []
        self.calls: list[str] = []

    def get_account(self) -> TradeAccount:
        self.calls.append("get_account")
        return self.account

    def get_all_positions(self) -> list[Position]:
        self.calls.append("get_all_positions")
        return list(self.positions)

    def get_orders(self, filter=None) -> list[Order]:
        self.calls.append("get_orders")
        return list(self.orders)

    def get_order_by_client_id(self, client_id: str) -> Order:
        raise AssertionError("the observability proof never resolves a submit identity")

    def submit_order(self, order_data: object) -> object:
        self.calls.append("submit_order")
        assert isinstance(order_data, (TrailingStopOrderRequest, LimitOrderRequest))
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
        self.orders.append(echo)
        return echo

    def cancel_order_by_id(self, order_id: object) -> None:
        self.calls.append("cancel_order_by_id")
        self.cancelled.append(str(order_id))
        self.orders = [order for order in self.orders if str(order.id) != str(order_id)]


def protector(client: FakeClient, *, feed: str = "none", now_ns: int = NOW_NS) -> Protector:
    return Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: now_ns,
        feed=feed,
    )


def test_the_line_names_every_field_of_an_overnight_hold() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client, feed="sip")
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=NOW_NS - 2 * SEC,
    )
    p.note_poll(NOW_NS)
    p.reconcile()

    line = p.states[-1]
    assert line.startswith("state ")
    assert "mode=overnight" in line
    assert "symbol=AAPL" in line
    assert "position=10" in line
    assert "protected=10" in line
    assert "trigger=97.50" in line
    assert "on=bid" in line
    assert f"order={client.orders[0].id}" in line
    assert "feed=sip" in line
    assert "poll_age=0.0s" in line
    assert "quote_age=2.0s" in line
    assert "order=none" not in line


def test_the_line_never_names_the_account_or_a_key() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client)
    p.reconcile()
    line = p.states[-1]

    assert "paper-fixture" not in line
    assert "ALPACA-B" not in line
    assert "test-key" not in line
    assert "/home/" not in line


def test_a_regular_session_measures_the_trigger_on_the_trade() -> None:
    at = datetime(2026, 11, 9, 10, 30, tzinfo=ET)
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client, now_ns=int(at.timestamp()) * SEC)
    p.reconcile()

    line = p.states[-1]
    assert "mode=rth" in line
    assert "trigger=97.50" in line
    assert "on=trade" in line


def test_a_session_change_writes_the_line_again() -> None:
    clock = [int(datetime(2026, 11, 9, 19, 59, tzinfo=ET).timestamp()) * SEC]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = Protector(client, b_account(), b_params(), now_ns=lambda: clock[0])
    p.reconcile()
    assert "mode=post" in p.states[-1]

    clock[0] = int(datetime(2026, 11, 9, 20, 0, 30, tzinfo=ET).timestamp()) * SEC
    p.reconcile()
    assert "mode=overnight" in p.states[-1]
    assert len([line for line in p.states if line.startswith("state ")]) == 2


def test_a_custody_change_writes_the_cancel_then_the_rearmed_line() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client, feed="sip")
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=NOW_NS,
    )
    p.note_poll(NOW_NS)
    p.reconcile()
    trail = client.orders[0]
    assert "protected=10" in p.states[-1]

    client.positions = [api_position("12")]
    p.reconcile()
    assert "position=12" in p.states[-1]
    assert "protected=0" in p.states[-1]
    assert "order=none" in p.states[-1]

    p.handle(api_update(trail, "canceled", NOW, 12))
    line = p.states[-1]
    assert "position=12" in line
    assert "protected=12" in line
    assert "order=none" not in line
    assert isinstance(client.submitted[-1], TrailingStopOrderRequest)


def test_the_production_default_names_no_feed_and_no_age() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client)
    p.reconcile()

    line = p.states[-1]
    assert "feed=none" in line
    assert "poll_age=none" in line
    assert "quote_age=none" in line


def test_a_release_writes_the_unprotected_cancel_then_the_limit() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="99.45")]
    p = protector(client, feed="sip")
    p.reconcile()
    trail = client.orders[0]

    p.on_quote(
        "AAPL",
        bid=Decimal("99.505"),
        ask=Decimal("99.51"),
        last=Decimal("99.51"),
        ts_ns=NOW_NS,
    )
    p.note_poll(NOW_NS)
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC // 2) is False
    assert p.on_trade("AAPL", Decimal("99.40"), NOW_NS + SEC) is True
    assert any("protected=0" in line and "order=none" in line for line in p.states)

    p.handle(api_update(trail, "canceled", NOW, 10))
    line = p.states[-1]
    assert "position=10" in line
    assert "protected=0" in line
    assert "order=none" not in line
    assert "on=none" in line
    assert isinstance(client.submitted[-1], LimitOrderRequest)


def test_the_stream_writes_the_startup_line(monkeypatch) -> None:
    async def no_connect(self) -> None:
        return None

    monkeypatch.setattr(TradingStream, "_start_ws", no_connect)
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    lines: list[str] = []
    stream = ProtectorStream(
        client,
        b_account(),
        b_params(),
        api_key="test-key",
        secret_key="test-secret",  # noqa: S106 - fixture, never a real key
        now_ns=lambda: NOW_NS,
        on_state=lines.append,
    )
    asyncio.run(stream._start_ws())

    assert len(lines) == 1
    assert lines[0].startswith("state mode=overnight")
    assert "feed=none" in lines[0]

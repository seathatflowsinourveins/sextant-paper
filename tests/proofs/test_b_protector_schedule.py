"""B's scheduled reconcile and regular-session-open re-arm (clean B PR-2).

The trade-update stream is event-driven: it reconciles when the owner or the broker acts,
and on every reconnect. Nothing reconciles when a resting native trailing stop is lost, and
nothing re-arms an unprotected position when the regular session opens. These witnesses pin
the cadence and the open hook to the protector, not to whichever event happens to arrive.
"""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.models import Order, Position, TradeAccount
from alpaca.trading.requests import TrailingStopOrderRequest
from alpaca.trading.stream import TradingStream

from sextant.paper.b_protector import BParameters, Protector, ProtectorStream, load_b_parameters
from sextant.paper.config import PaperAccount
from sextant.paper.sessions import Session

ROOT = Path(__file__).parents[2]
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=ET)
NOW_NS = int(NOW.timestamp()) * 1_000_000_000
SECOND = 1_000_000_000


def ns(local: datetime) -> int:
    return int(local.timestamp()) * 1_000_000_000


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
    client_order_id: str,
    symbol: str = "AAPL",
    qty: str = "10",
    side: str = "sell",
    order_type: str = "trailing_stop",
    status: str = "new",
    trail_percent: str | None = "2.5",
) -> Order:
    payload = {
        "id": str(uuid4()),
        "client_order_id": client_order_id,
        "created_at": "2026-11-09T14:59:00Z",
        "updated_at": "2026-11-09T14:59:00Z",
        "submitted_at": "2026-11-09T14:59:00Z",
        "order_class": "simple",
        "time_in_force": "gtc",
        "status": status,
        "extended_hours": False,
        "symbol": symbol,
        "qty": qty,
        "filled_qty": "0",
        "side": side,
        "type": order_type,
    }
    if trail_percent is not None:
        payload["trail_percent"] = trail_percent
    return Order.model_validate(payload)


class FakeTradingClient:
    """Serves alpaca-py models and records every order write."""

    def __init__(self, *, positions=(), orders=()):
        self.account = api_account()
        self.positions = list(positions)
        self.orders = list(orders)
        self.calls: list[str] = []
        self.submitted: list[TrailingStopOrderRequest] = []
        self.cancelled: list[str] = []

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
        raise AssertionError("the schedule proof never resolves a submit identity")

    def submit_order(self, order_data):
        self.calls.append("submit_order")
        assert isinstance(order_data, TrailingStopOrderRequest)
        assert order_data.client_order_id is not None
        assert order_data.symbol is not None
        self.submitted.append(order_data)
        order = api_order(
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            qty=str(order_data.qty),
            trail_percent=str(order_data.trail_percent),
        )
        self.orders.append(order)
        return order

    def cancel_order_by_id(self, order_id) -> None:
        self.calls.append("cancel_order_by_id")
        self.cancelled.append(str(order_id))
        self.orders = [o for o in self.orders if str(o.id) != str(order_id)]


def test_the_registry_carries_the_scheduled_reconcile_interval(tmp_path):
    path = ROOT / "lanes/paper/b-parameters.json"
    params = load_b_parameters(path)
    assert params.reconcile_interval_s == Decimal("10")

    rows = json.loads(path.read_text())
    assert {row["name"] for row in rows} == {
        "native_trail_fraction",
        "reconcile_interval_s",
        "feed_poll_interval_s",
        "feed_silence_s",
        "tape_read_timeout_s",
        "tape_max_quotes",
        "tape_deadline_s",
        "tape_max_requests",
        "alpaca_min_trail_fraction",
        "trail_atr_period",
        "trail_atr_bar_minutes",
        "trail_atr_multiple",
        "tape_max_bars",
    }
    assert all(row["calibration_state"] == "UNCALIBRATED" for row in rows)
    assert all(row["source"] for row in rows)
    interval = next(row for row in rows if row["name"] == "reconcile_interval_s")
    assert "second" in interval["unit"]
    assert interval["last_change"] == "2026-11-09"

    with pytest.raises(ValueError):
        BParameters(Decimal("0.025"), Decimal("0"), Decimal("1"), Decimal("5"))

    incomplete = tmp_path / "b-parameters.json"
    incomplete.write_text(
        json.dumps([row for row in rows if row["name"] != "reconcile_interval_s"])
    )
    with pytest.raises(ValueError):
        load_b_parameters(incomplete)


def test_a_scheduled_reconcile_rearms_a_house_trail_the_broker_lost():
    clock = [NOW_NS]
    client = FakeTradingClient(positions=[api_position("10")])
    protector = Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: clock[0],
    )
    protector.reconcile()
    assert len(client.submitted) == 1
    assert client.submitted[0].qty == 10

    client.orders.clear()  # the broker loses the trail and no trade update reaches us
    calls = len(client.calls)

    clock[0] += 9 * SECOND
    assert protector.reconcile_when_due() is False
    assert len(client.calls) == calls  # not due yet: no broker call, no busy poll
    assert len(client.submitted) == 1

    clock[0] += SECOND
    assert protector.reconcile_when_due() is True
    assert len(client.submitted) == 2
    assert client.submitted[1].symbol == "AAPL"
    assert client.submitted[1].qty == 10
    assert (client.submitted[1].client_order_id or "").startswith("B001-trail-")


def test_the_regular_session_open_rearms_before_the_interval_elapses():
    clock = [ns(datetime(2026, 11, 9, 9, 29, 55, tzinfo=ET))]
    client = FakeTradingClient(positions=[api_position("10")])
    protector = Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: clock[0],
    )

    assert protector.reconcile_when_due() is False  # five seconds before the open, not due
    assert client.submitted == []
    assert protector.pages == []

    clock[0] = ns(datetime(2026, 11, 9, 9, 30, 0, tzinfo=ET))
    assert protector.reconcile_when_due() is True  # the session flip, not the interval, fires
    assert len(client.submitted) == 1
    assert client.submitted[0].qty == 10


def test_the_open_rearms_the_full_net_unreserved_quantity():
    clock = [ns(datetime(2026, 11, 9, 9, 29, 55, tzinfo=ET))]
    owner_sell = api_order(
        client_order_id="owner-sell-1",
        qty="2",
        side="sell",
        order_type="limit",
        trail_percent=None,
    )
    client = FakeTradingClient(positions=[api_position("12")], orders=[owner_sell])
    protector = Protector(client, b_account(), b_params(), now_ns=lambda: clock[0])

    clock[0] = ns(datetime(2026, 11, 9, 9, 30, 0, tzinfo=ET))
    assert protector.reconcile_when_due() is True
    assert [request.qty for request in client.submitted] == [10]  # 12 held, 2 reserved
    assert str(owner_sell.id) not in client.cancelled


def _stream(client, pages) -> ProtectorStream:
    return ProtectorStream(
        client,
        b_account(),
        b_params(),
        api_key="fixture-key",
        secret_key="fixture-secret",  # noqa: S106 - fixture, never a real key
        now_ns=lambda: NOW_NS,
        on_page=pages.append,
    )


def test_the_stream_runs_the_schedule_alongside_the_trade_updates(monkeypatch):
    pages: list = []
    stream = _stream(FakeTradingClient(), pages)
    ticks: list[int] = []
    done = asyncio.Event()

    def scheduled(self):
        ticks.append(1)
        done.set()
        return False

    async def fake_run_forever(self):
        await asyncio.wait_for(done.wait(), timeout=2)

    monkeypatch.setattr(ProtectorStream, "SCHEDULE_TICK_S", 0.005)
    monkeypatch.setattr(Protector, "reconcile_when_due", scheduled)
    monkeypatch.setattr(TradingStream, "_run_forever", fake_run_forever)

    asyncio.run(stream._run_forever())

    assert ticks == [1]
    assert pages == []


def test_a_schedule_failure_pages_and_keeps_the_timer_running(monkeypatch):
    pages: list = []
    stream = _stream(FakeTradingClient(), pages)
    ticks: list[int] = []
    done = asyncio.Event()

    def scheduled(self):
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("snapshot refused")
        done.set()
        return False

    async def fake_run_forever(self):
        await asyncio.wait_for(done.wait(), timeout=2)

    monkeypatch.setattr(ProtectorStream, "SCHEDULE_TICK_S", 0.005)
    monkeypatch.setattr(Protector, "reconcile_when_due", scheduled)
    monkeypatch.setattr(TradingStream, "_run_forever", fake_run_forever)

    asyncio.run(stream._run_forever())

    assert pages[-1].reason == "reconcile_failed"
    assert pages[-1].detail == "RuntimeError"
    assert len(ticks) >= 2

"""B's outside-RTH market data: fed breach, truthful arming, and a page on silence.

The production entrypoint used to subscribe only the trade-update stream, so outside regular
hours the emulated BID_ASK trigger never saw a quote or a print and the state line could claim
protection it did not have. These proofs drive the REST latest-quote/latest-trade adapter and
the silence watchdog; they name the pinned alpaca-py client, requests and feeds (clean B).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from alpaca.common.enums import Sort
from alpaca.data.enums import DataFeed
from alpaca.data.models import Quote, Trade
from alpaca.data.requests import (
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Order, Position, TradeAccount, TradeUpdate
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest
from alpaca.trading.stream import TradingStream

from sextant.paper.b_feed import BHistoricalTape, BMarketDataFeed, feed_for, timestamp_ns
from sextant.paper.b_protector import (
    BParameters,
    Protector,
    ProtectorStream,
)
from sextant.paper.config import PaperAccount
from sextant.paper.sessions import Session

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 9, 20, 30, tzinfo=ET)  # overnight, outside the regular session


def ns(at: datetime) -> int:
    return timestamp_ns(at)


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
    submitted_at: str = "2026-11-09T23:59:00Z",
    filled_at: str | None = None,
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


def api_quote(
    bid: str = "99.505", ask: str = "99.51", at: datetime = NOW, symbol: str = "AAPL"
) -> Quote:
    return Quote(
        symbol,
        {
            "t": at.isoformat(),
            "bp": float(bid),
            "bs": 10,
            "ap": float(ask),
            "as": 10,
        },
    )


def api_trade(price: str = "99.40", at: datetime = NOW) -> Trade:
    return Trade(
        "AAPL",
        {
            "t": at.isoformat(),
            "p": float(price),
            "s": 100,
        },
    )


def _symbols(value: str | Sequence[str]) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


class FakeDataClient:
    def __init__(
        self,
        *,
        quote: Quote | None = None,
        trades: Sequence[Trade] = (),
        repeat_last_trade: bool = False,
    ) -> None:
        self.quote = quote
        self.trades = list(trades)
        self.repeat_last_trade = repeat_last_trade
        self.requests: list[tuple[str, DataFeed | None, tuple[str, ...]]] = []

    def get_stock_latest_quote(self, request: StockLatestQuoteRequest) -> dict[str, Quote]:
        symbols = _symbols(request.symbol_or_symbols)
        self.requests.append(("quote", request.feed, symbols))
        if self.quote is None or self.quote.symbol not in symbols:
            return {}
        return {self.quote.symbol: self.quote}

    def get_stock_latest_trade(self, request: StockLatestTradeRequest) -> dict[str, Trade]:
        symbols = _symbols(request.symbol_or_symbols)
        self.requests.append(("trade", request.feed, symbols))
        if not self.trades or self.trades[0].symbol not in symbols:
            return {}
        trade = self.trades[0] if self.repeat_last_trade else self.trades.pop(0)
        return {trade.symbol: trade}


class FakeClient:
    def __init__(self) -> None:
        self.account = api_account()
        self.positions: list[Position] = []
        self.orders: list[Order] = []
        self.closed_orders: list[Order] = []
        self.submitted: list[TrailingStopOrderRequest | LimitOrderRequest] = []
        self.cancelled: list[str] = []

    def get_account(self) -> TradeAccount:
        return self.account

    def get_all_positions(self) -> list[Position]:
        return list(self.positions)

    def get_orders(self, filter=None) -> list[Order]:
        status = getattr(filter, "status", None)
        if status == QueryOrderStatus.CLOSED:
            return list(self.closed_orders)
        if status == QueryOrderStatus.OPEN:
            return list(self.orders)
        return list(self.orders) + list(self.closed_orders)

    def get_order_by_client_id(self, client_id: str) -> Order:
        raise AssertionError("the feed proof never resolves a submit identity")

    def submit_order(self, order_data: object) -> object:
        assert isinstance(order_data, (TrailingStopOrderRequest, LimitOrderRequest))
        self.submitted.append(order_data)
        client_order_id = order_data.client_order_id or f"B001-echo-{len(self.submitted)}"
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
        self.orders.append(echo)
        return echo

    def cancel_order_by_id(self, order_id: object) -> None:
        self.cancelled.append(str(order_id))
        self.orders = [order for order in self.orders if str(order.id) != str(order_id)]


def protector(
    client: FakeClient, *, feed: str = "none", clock: list[int] | None = None
) -> Protector:
    now = clock if clock is not None else [NOW_NS]
    return Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: now[0],
        feed=feed,
    )


def test_a_fed_breach_cancels_the_trail_and_releases_one_limit() -> None:
    clock = [NOW_NS]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="99.45")]
    data = FakeDataClient(
        quote=api_quote(),
        trades=[
            api_trade(at=NOW),
            api_trade(at=datetime(2026, 11, 9, 20, 30, 0, 500_000, tzinfo=ET)),
            api_trade(at=datetime(2026, 11, 9, 20, 30, 1, tzinfo=ET)),
        ],
    )
    feed = BMarketDataFeed(data)
    p = protector(client, feed="sip", clock=clock)
    p.reconcile()
    trail = client.orders[0]

    for _ in range(3):
        batch = feed.poll(("AAPL",), "sip")
        p.note_poll(clock[0])
        for quote in batch.quotes:
            p.on_quote(
                quote.symbol,
                bid=quote.bid,
                ask=quote.ask,
                last=quote.last,
                ts_ns=quote.ts_ns,
            )
        for trade in batch.trades:
            p.on_trade(trade.symbol, trade.price, trade.ts_ns)
        clock[0] += SEC // 2

    assert any("protected=0" in line and "order=none" in line for line in p.states)
    p.handle(api_update(trail, "canceled", NOW, 10))
    assert isinstance(client.submitted[-1], LimitOrderRequest)
    assert "protected=0" in p.states[-1]
    assert "on=none" in p.states[-1]


def test_a_missing_feed_pages_instead_of_staying_silent() -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client)
    p.reconcile()
    p.watch()

    assert [page.reason for page in p.pages] == ["outside_rth_feed_silent"]
    assert p.pages[0].symbol == "AAPL"
    assert "protected=0" in p.states[-1]
    assert "feed=none" in p.states[-1]


def test_a_standing_quote_stays_armed_while_polls_succeed() -> None:
    clock = [NOW_NS]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client, feed="sip", clock=clock)
    p.reconcile()
    p.note_poll(NOW_NS)
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=NOW_NS,
    )
    p.watch()
    assert p.pages == []
    assert "protected=10" in p.states[-1]
    assert "poll_age=0.0s" in p.states[-1]
    assert "quote_age=0.0s" in p.states[-1]

    clock[0] += 56 * SEC // 10
    p.note_poll(clock[0])
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=NOW_NS,
    )
    p.watch()
    assert p.pages == []
    assert "protected=10" in p.states[-1]
    assert "poll_age=0.0s" in p.states[-1]
    assert "quote_age=5.6s" in p.states[-1]


def test_polls_that_stop_still_page_and_unarm() -> None:
    clock = [NOW_NS]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client, feed="sip", clock=clock)
    p.reconcile()
    p.note_poll(NOW_NS)
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=NOW_NS,
    )
    p.watch()
    assert p.pages == []

    clock[0] += 6 * SEC
    p.watch()
    assert [page.reason for page in p.pages] == ["outside_rth_feed_silent"]
    assert "protected=0" in p.states[-1]
    assert "trigger=none" in p.states[-1]
    assert "poll_age=6.0s" in p.states[-1]
    assert "quote_age=6.0s" in p.states[-1]


def test_a_quiet_interval_does_not_reset_the_breach_run() -> None:
    clock = [NOW_NS]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="99.45")]
    data = FakeDataClient(quote=api_quote(at=NOW), trades=[api_trade(at=NOW)])
    feed = BMarketDataFeed(data)
    p = protector(client, feed="sip", clock=clock)
    p.reconcile()
    trail = client.orders[0]

    for second in range(13):
        if second == 6:
            data.quote = api_quote(at=datetime(2026, 11, 9, 20, 30, 6, tzinfo=ET))
            data.trades = [api_trade(at=datetime(2026, 11, 9, 20, 30, 6, tzinfo=ET))]
        if second == 12:
            data.quote = api_quote(at=datetime(2026, 11, 9, 20, 30, 12, tzinfo=ET))
            data.trades = [api_trade(at=datetime(2026, 11, 9, 20, 30, 12, tzinfo=ET))]
        batch = feed.poll(("AAPL",), "sip")
        p.note_poll(clock[0])
        for quote in batch.quotes:
            p.on_quote(
                quote.symbol,
                bid=quote.bid,
                ask=quote.ask,
                last=quote.last,
                ts_ns=quote.ts_ns,
            )
        for trade in batch.trades:
            p.on_trade(trade.symbol, trade.price, trade.ts_ns)
        p.watch()
        clock[0] += SEC

    assert p.pages == []
    p.handle(api_update(trail, "canceled", datetime(2026, 11, 9, 20, 30, 12, tzinfo=ET), 10))
    assert isinstance(client.submitted[-1], LimitOrderRequest)


def test_each_symbol_reports_its_own_quote_age() -> None:
    client = FakeClient()
    client.positions = [api_position("10", "AAPL"), api_position("10", "MSFT")]
    client.orders = [
        api_order(client_order_id="B001-trail-aapl", symbol="AAPL", stop_price="97.50"),
        api_order(client_order_id="B001-trail-msft", symbol="MSFT", stop_price="197.50"),
    ]
    p = protector(client, feed="sip")
    p.reconcile()
    p.note_poll(NOW_NS)
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=NOW_NS,
    )
    p.on_quote(
        "MSFT",
        bid=Decimal("199.50"),
        ask=Decimal("199.51"),
        last=Decimal("199.50"),
        ts_ns=NOW_NS - 2 * SEC,
    )
    p.watch()

    aapl = next(line for line in reversed(p.states) if "symbol=AAPL" in line)
    msft = next(line for line in reversed(p.states) if "symbol=MSFT" in line)
    assert "protected=10" in aapl
    assert "quote_age=0.0s" in aapl
    assert "protected=10" in msft
    assert "quote_age=2.0s" in msft


def test_pre_and_post_poll_sip_and_the_overnight_polls_boats() -> None:
    assert feed_for(Session.PRE) == "sip"
    assert feed_for(Session.POST) == "sip"
    assert feed_for(Session.OVERNIGHT) == "boats"
    assert feed_for(Session.RTH) is None
    assert feed_for(Session.CLOSED) is None

    data = FakeDataClient(quote=api_quote(), trades=[api_trade()])
    feed = BMarketDataFeed(data)
    feed.poll(("AAPL",), "sip")
    feed.poll(("AAPL",), "boats")

    assert data.requests[0] == ("quote", DataFeed.SIP, ("AAPL",))
    assert data.requests[1] == ("trade", DataFeed.SIP, ("AAPL",))
    assert data.requests[2] == ("quote", DataFeed.BOATS, ("AAPL",))
    assert data.requests[3] == ("trade", DataFeed.BOATS, ("AAPL",))


def test_the_same_latest_trade_is_counted_once() -> None:
    data = FakeDataClient(quote=api_quote(), trades=[api_trade()], repeat_last_trade=True)
    feed = BMarketDataFeed(data)

    first = feed.poll(("AAPL",), "sip")
    second = feed.poll(("AAPL",), "sip")

    assert len(first.quotes) == 1
    assert len(first.trades) == 1
    assert len(second.quotes) == 1
    assert second.quotes[0].ts_ns == first.quotes[0].ts_ns
    assert second.trades == ()


def test_the_poll_interval_must_be_shorter_than_the_silence_bound() -> None:
    with pytest.raises(ValueError, match="feed_poll_interval_s"):
        BParameters(
            native_trail_fraction=Decimal("0.025"),
            reconcile_interval_s=Decimal("10"),
            feed_poll_interval_s=Decimal("5"),
            feed_silence_s=Decimal("5"),
        )


def test_the_stream_startup_line_names_the_live_feed(monkeypatch) -> None:
    async def no_connect(self) -> None:
        return None

    monkeypatch.setattr(TradingStream, "_start_ws", no_connect)
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    data = FakeDataClient(quote=api_quote())
    lines: list[str] = []
    stream = ProtectorStream(
        client,
        b_account(),
        b_params(),
        api_key="test-key",
        secret_key="test-secret",  # noqa: S106 - fixture, never a real key
        now_ns=lambda: NOW_NS,
        on_state=lines.append,
        data_client=data,
    )
    asyncio.run(stream._start_ws())

    assert len(lines) == 1
    assert "feed=boats" in lines[0]
    assert "protected=10" in lines[0]
    assert "poll_age=0.0s" in lines[0]
    assert "quote_age=0.0s" in lines[0]


def test_a_quote_stamped_after_the_print_still_prices_the_release() -> None:
    clock = [NOW_NS + SEC + SEC // 20]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="99.45")]
    p = protector(client, feed="sip", clock=clock)
    p.reconcile()
    trail = client.orders[0]
    p.note_poll(clock[0])
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.40"),
        ts_ns=clock[0],
    )

    for offset in (0, SEC // 2, SEC):
        p.on_trade("AAPL", Decimal("99.40"), NOW_NS + offset)

    assert p.pages == []
    assert client.cancelled
    p.handle(api_update(trail, "canceled", NOW, 10))
    assert isinstance(client.submitted[-1], LimitOrderRequest)


def test_a_quote_older_than_the_vendor_bound_still_prices_while_polls_succeed() -> None:
    clock = [NOW_NS]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="99.45")]
    p = protector(client, feed="sip", clock=clock)
    p.reconcile()
    trail = client.orders[0]
    p.note_poll(NOW_NS)
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.45"),
        ts_ns=NOW_NS,
    )

    clock[0] += 4 * SEC + SEC // 5
    p.note_poll(clock[0])
    p.watch()
    assert "protected=10" in p.states[-1]
    assert "quote_age=4.2s" in p.states[-1]

    for offset in (0, SEC // 2, SEC):
        clock[0] = NOW_NS + 4 * SEC + SEC // 5 + offset
        p.on_trade("AAPL", Decimal("99.40"), clock[0])

    assert p.pages == []
    p.handle(api_update(trail, "canceled", NOW, 10))
    assert isinstance(client.submitted[-1], LimitOrderRequest)


def test_a_prior_session_quote_never_arms_or_ratchets_the_new_session() -> None:
    at = datetime(2026, 11, 9, 20, 0, 1, tzinfo=ET)
    clock = [int(at.timestamp()) * SEC]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="97.50")]
    p = protector(client, feed="none", clock=clock)
    p.reconcile()
    p.set_feed("sip")

    p.on_quote(
        "AAPL",
        bid=Decimal("105.00"),
        ask=Decimal("105.01"),
        last=Decimal("105.00"),
        ts_ns=int(datetime(2026, 11, 9, 3, 59, 50, tzinfo=ET).timestamp()) * SEC,
    )

    assert p.emulated_trigger("AAPL") == Decimal("97.50")


def test_a_segment_change_resets_the_breach_run() -> None:
    at = datetime(2026, 11, 9, 19, 59, 30, tzinfo=ET)
    clock = [int(at.timestamp()) * SEC]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="99.45")]
    p = protector(client, feed="sip", clock=clock)
    p.reconcile()
    p.note_poll(clock[0])
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=clock[0],
    )
    p.on_trade("AAPL", Decimal("99.40"), clock[0])
    p.on_trade("AAPL", Decimal("99.40"), clock[0] + SEC // 2)

    clock[0] = int(datetime(2026, 11, 9, 20, 0, 1, tzinfo=ET).timestamp()) * SEC
    p.set_feed("boats")
    assert p.on_trade("AAPL", Decimal("99.40"), clock[0]) is False
    assert p.pages == []
    assert client.submitted == []


def test_the_first_post_quote_never_seeds_below_the_native_stop() -> None:
    before = datetime(2026, 11, 9, 15, 59, 59, tzinfo=ET)
    after = datetime(2026, 11, 9, 16, 0, 1, tzinfo=ET)
    clock = [int(before.timestamp()) * SEC]
    client = FakeClient()
    client.positions = [api_position("10")]
    client.orders = [api_order(stop_price="107.25")]
    p = protector(client, feed="none", clock=clock)
    p.reconcile()

    clock[0] = int(after.timestamp()) * SEC
    p.set_feed("sip")
    p.on_quote(
        "AAPL",
        bid=Decimal("108.00"),
        ask=Decimal("108.01"),
        last=Decimal("108.00"),
        ts_ns=clock[0],
    )

    assert p.emulated_trigger("AAPL") == Decimal("107.25")


def test_a_symbol_without_a_quote_pages_while_another_is_armed() -> None:
    client = FakeClient()
    client.positions = [api_position("10", "AAPL"), api_position("10", "MSFT")]
    client.orders = [
        api_order(client_order_id="B001-trail-aapl", symbol="AAPL", stop_price="97.50"),
        api_order(client_order_id="B001-trail-msft", symbol="MSFT", stop_price="197.50"),
    ]
    p = protector(client, feed="sip")
    p.reconcile()
    p.note_poll(NOW_NS)
    p.on_quote(
        "AAPL",
        bid=Decimal("99.50"),
        ask=Decimal("99.51"),
        last=Decimal("99.50"),
        ts_ns=NOW_NS,
    )
    p.watch()

    assert [(page.reason, page.symbol) for page in p.pages] == [("outside_rth_feed_silent", "MSFT")]
    aapl = next(line for line in reversed(p.states) if "symbol=AAPL" in line)
    msft = next(line for line in reversed(p.states) if "symbol=MSFT" in line)
    assert "protected=10" in aapl
    assert "protected=0" in msft


def test_the_silence_registry_row_says_what_is_measured() -> None:
    root = Path(__file__).resolve().parents[2]
    rows = json.loads((root / "lanes/paper/b-parameters.json").read_text())
    row = next(item for item in rows if item["name"] == "feed_silence_s")
    assert row["unit"] == "seconds without a successful outside-RTH poll before the watchdog pages"
    assert "UNCALIBRATED" in row["source"]
    assert "X4" not in row["source"]
    assert "docs/design.md" not in row["source"]


class FakeHistoricalClient:
    def __init__(self, rows: dict[str, list[dict[str, object]]]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, dict[str, object]]] = []

    def get(self, path: str, data: dict[str, object]) -> dict[str, object]:
        self.requests.append((path, data))
        return {"quotes": self.rows, "next_page_token": None}


def historical_rows() -> dict[str, list[dict[str, object]]]:
    return {
        "AAPL": [
            {"t": "2026-11-09T17:47:18Z", "bp": 36.99, "ap": 37.00, "bs": 1, "as": 1},
            {"t": "2026-11-09T21:59:00Z", "bp": 38.70, "ap": 38.71, "bs": 1, "as": 1},
        ]
    }


def test_the_historical_tape_requests_the_session_feed_newest_first() -> None:
    # R4: alpaca-py 0.44.0 BaseTimeseriesDataRequest.sort (alpaca/data/requests.py:40), so a
    # partial read covers the most recent part of the window.
    client = FakeHistoricalClient(historical_rows())
    tape = BHistoricalTape(client, 10_000)
    start = int(datetime(2026, 11, 9, 12, 47, tzinfo=ET).timestamp()) * SEC
    end = int(datetime(2026, 11, 9, 18, 10, tzinfo=ET).timestamp()) * SEC

    observations, token = tape.quote_page("AAPL", start, end, "sip")

    assert {item.bid for item in observations} == {Decimal("36.99"), Decimal("38.70")}
    assert token is None
    path, request = client.requests[0]
    assert path == "/stocks/quotes"
    assert request["symbols"] == "AAPL"
    assert request["feed"] is DataFeed.SIP
    assert request["sort"] is Sort.DESC
    assert request.get("page_token") is None
    assert request["limit"] == 10_000
    assert request["start"] == "2026-11-09T17:47:00+00:00"
    assert request["end"] == "2026-11-09T23:10:00+00:00"


def test_a_continuation_page_sends_the_token_and_returns_the_next_one() -> None:
    # R1: the token alpaca-py's own page loop sends (alpaca/common/rest.py:381,388,398).
    class Pages(FakeHistoricalClient):
        def get(self, path: str, data: dict[str, object]) -> dict[str, object]:
            self.requests.append((path, dict(data)))
            return {"quotes": self.rows, "next_page_token": "page-three"}

    client = Pages(historical_rows())
    _, token = BHistoricalTape(client, 10_000).quote_page("AAPL", 0, 1, "sip", "page-two")

    assert token == "page-three"  # noqa: S105 - a page token
    assert client.requests[0][1]["page_token"] == "page-two"  # noqa: S105 - a page token


@pytest.mark.parametrize(("feed", "expected"), [("sip", DataFeed.SIP), ("boats", DataFeed.BOATS)])
def test_the_historical_tape_uses_the_sessions_feed(feed: str, expected: DataFeed) -> None:
    client = FakeHistoricalClient(historical_rows())
    tape = BHistoricalTape(client, 10_000)

    tape.quote_page("AAPL", 0, 1, feed)

    assert client.requests[0][1]["feed"] is expected


def test_the_tape_windows_split_at_the_feed_boundaries() -> None:
    from sextant.paper.b_protector import tape_windows

    start = ns(datetime(2026, 11, 9, 18, 10, tzinfo=ET))
    end = ns(datetime(2026, 11, 9, 21, 0, tzinfo=ET))
    sip_end = ns(datetime(2026, 11, 9, 20, 0, tzinfo=ET))
    assert tape_windows(start, end) == ((start, sip_end, "sip"), (sip_end, end, "boats"))

    overnight = ns(datetime(2026, 11, 9, 3, 0, tzinfo=ET))
    noon = ns(datetime(2026, 11, 9, 12, 0, tzinfo=ET))
    feeds = [window[2] for window in tape_windows(overnight, noon)]
    assert feeds == ["boats", "sip"]


def tonight_client() -> FakeClient:
    client = FakeClient()
    client.positions = [api_position("55")]
    client.orders = [api_order(qty="55", stop_price="36.465", submitted_at="2026-11-09T17:47:18Z")]
    client.closed_orders = [
        api_order(
            client_order_id="owner-entry",
            side="buy",
            qty="55",
            filled_qty="55",
            status="filled",
            submitted_at="2026-11-09T17:47:16Z",
            filled_at="2026-11-09T17:47:17.479764Z",
        )
    ]
    return client


def started_line(client: FakeClient, history: object, at_ns: int) -> tuple[Protector, str]:
    p = Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: at_ns,
        feed="sip",
        history=history,  # type: ignore[arg-type]
    )
    p.rebuild_tapes()
    p.reconcile()
    p.note_poll(at_ns)
    p.on_quote("AAPL", bid=Decimal("36.60"), ask=Decimal("36.61"), last=None, ts_ns=at_ns)
    p.watch()
    return p, p.states[-1]


class ShapeClient:
    def __init__(self, response: object) -> None:
        self.response = response

    def get(self, path: str, data: dict[str, object]) -> object:
        return self.response


POST_AT_NS = ns(datetime(2026, 11, 9, 18, 10, tzinfo=ET))


@pytest.mark.parametrize(
    "response",
    [
        {"quotes": {"aapl": []}},
        {"quotes": []},
        {"AAPL": []},
        {"quotes": {"AAPL": [None]}},
        # R1: a token is followed now; one that never ends is partial when the budget ends.
        {"quotes": {"AAPL": []}, "next_page_token": "unread"},
    ],
    ids=["other-symbol-key", "non-mapping", "no-quotes-key", "null-row", "endless-pages"],
)
def test_a_malformed_tape_response_is_partial_never_ok(response: object) -> None:
    # F6: an unexpected shape is not an empty window.
    tape = BHistoricalTape(ShapeClient(response), 10_000)
    p, line = started_line(tonight_client(), tape, POST_AT_NS)

    assert "tape=partial" in line and "trigger=36.465" in line, line
    assert [page.reason for page in p.pages] == ["outside_rth_tape_unreadable"]


def test_a_page_over_the_quote_bound_is_partial() -> None:
    # R1 changes N3's bound: a page AT the bound with no token is the window's last page; a
    # page OVER the bound is not what was asked for, so it is malformed and partial.
    rows = {
        "AAPL": [
            {"t": "2026-11-09T21:59:00Z", "bp": 38.70, "ap": 38.71, "bs": 1, "as": 1},
            {"t": "2026-11-09T22:00:00Z", "bp": 38.60, "ap": 38.61, "bs": 1, "as": 1},
            {"t": "2026-11-09T22:01:00Z", "bp": 38.50, "ap": 38.51, "bs": 1, "as": 1},
        ]
    }
    tape = BHistoricalTape(FakeHistoricalClient(rows), 2)
    _, line = started_line(tonight_client(), tape, POST_AT_NS)

    assert "tape=partial" in line and "trigger=36.465" in line, line


def test_a_read_that_hangs_at_start_reaches_its_bound_and_b_starts_protecting() -> None:
    # Witness iv: a server that accepts and never answers; the transport timeout ends the read.
    import socket
    import threading
    import time

    from alpaca.data.historical.stock import StockHistoricalDataClient

    from sextant.paper.b_feed import bound_transport

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    hold = threading.Event()

    def accept_and_hang() -> None:
        connection, _ = server.accept()
        hold.wait(3)  # the test's own hard bound: the RED side cannot hang past it
        connection.close()

    threading.Thread(target=accept_and_hang, daemon=True).start()
    data = StockHistoricalDataClient(
        "fixture-key", "fixture-secret", url_override=f"http://127.0.0.1:{server.getsockname()[1]}"
    )
    try:
        bound_transport(data, Decimal("0.2"))
        tape = BHistoricalTape(data, 10_000)
        began = time.monotonic()
        p, line = started_line(tonight_client(), tape, POST_AT_NS)
        elapsed = time.monotonic() - began
    finally:
        hold.set()
        server.close()

    assert elapsed < 2, elapsed
    assert "tape=partial" in line and "trigger=36.465" in line and "protected=55" in line, line
    assert [page.reason for page in p.pages] == ["outside_rth_tape_unreadable"]


def paced(tokens: int):
    from sextant.execution.pacer import AccountPacer
    from sextant.paper.b_protector_main import BudgetControls
    from sextant.paper.config import PaperBudget

    budget = PaperBudget(requests=180, window_s=60, broker_cap=200, external_reserve=20, source="t")
    pacer = AccountPacer(BudgetControls(budget), now_ns=lambda: 0)  # type: ignore[arg-type]
    pacer.bucket.tokens = tokens
    return pacer


def test_historical_quote_reads_wait_on_the_account_pacer() -> None:
    from sextant.paper.b_protector_main import PacedHistoricalDataClient

    pacer = paced(1)
    client = FakeHistoricalClient(historical_rows())
    tape = BHistoricalTape(PacedHistoricalDataClient(client, pacer), 10_000)  # type: ignore[arg-type]
    tape.quote_page("AAPL", 0, 1, "sip")
    assert len(client.requests) == 1
    assert pacer.bucket.tokens == 0


def test_a_refused_historical_read_is_withdrawn_and_never_runs_later() -> None:
    # R4 / F2: a refused read leaves the pacer's queue; a later drain must not send it.
    from sextant.execution.pacer import PacerRefused
    from sextant.paper.b_protector_main import PacedHistoricalDataClient

    pacer = paced(0)
    client = FakeHistoricalClient(historical_rows())
    tape = BHistoricalTape(PacedHistoricalDataClient(client, pacer), 10_000)  # type: ignore[arg-type]
    with pytest.raises(PacerRefused):
        tape.quote_page("AAPL", 0, 1, "sip")
    assert pacer.queue == []
    pacer.bucket.tokens = 5
    pacer.drain()
    assert client.requests == []


def test_the_stream_reads_the_tape_once_before_its_loop_and_never_inside_it(monkeypatch) -> None:
    # R1/R2: production's own path; the read runs before the loop exists, then never again.
    reads: list[bool] = []

    class LoopProbe:
        def quote_page(
            self, symbol: str, start_ns: int, end_ns: int, feed: str, page_token: object = None
        ) -> tuple:
            try:
                asyncio.get_running_loop()
                reads.append(True)
            except RuntimeError:
                reads.append(False)
            return (), None

    async def no_connect(self) -> None:
        return None

    def loop_body(self) -> None:
        asyncio.run(self._start_ws())

    monkeypatch.setattr(TradingStream, "_start_ws", no_connect)
    monkeypatch.setattr(TradingStream, "run", loop_body)
    stream = ProtectorStream(
        tonight_client(),
        b_account(),
        b_params(),
        api_key="test-key",
        secret_key="test-secret",  # noqa: S106 - fixture, never a real key
        now_ns=lambda: POST_AT_NS,
        history=LoopProbe(),  # type: ignore[arg-type]
    )
    stream.run()
    stream.protector.reconcile()

    assert reads == [False]
    assert stream.protector.emulated_trigger("AAPL") == Decimal("36.465")


def test_a_one_sided_historical_quote_is_skipped_not_a_malformed_window() -> None:
    # G4: a zero bid or ask is an ordinary thin-tape row, as the live poller treats it.
    rows = historical_rows()
    rows["AAPL"].insert(1, {"t": "2026-11-09T20:00:00Z", "bp": 0, "ap": 38.80, "bs": 0, "as": 1})
    tape = BHistoricalTape(FakeHistoricalClient(rows), 10_000)
    observations, token = tape.quote_page("AAPL", 0, POST_AT_NS, "sip")

    assert [item.bid for item in observations] == [Decimal("36.99"), Decimal("38.70")]
    assert token is None


def test_the_bounded_tape_client_makes_no_internal_retries() -> None:
    # G1: alpaca-py retries a 429 or 504 three times with a 3 s sleep (common/rest.py:127-134).
    from alpaca.common.exceptions import APIError
    from alpaca.data.historical.stock import StockHistoricalDataClient

    from sextant.paper.b_feed import bound_transport

    data = StockHistoricalDataClient("fixture-key", "fixture-secret")
    bound_transport(data, Decimal("0.2"))
    sent: list[str] = []

    class Answer:
        status_code = 429
        text = "rate limited"

        def raise_for_status(self) -> None:
            import requests

            raise requests.HTTPError("429")

    def request(method: str, url: str, **opts: object) -> Answer:
        sent.append(url)
        return Answer()

    data._session.request = request  # type: ignore[method-assign]
    with pytest.raises(APIError):
        BHistoricalTape(data, 10_000).quote_page("AAPL", 0, 1, "sip")
    assert len(sent) == 1


def test_every_refused_start_read_is_withdrawn_from_the_pacer() -> None:
    # G6: a refused order or position read must not run inside a later drain.
    from sextant.execution.pacer import PacerRefused
    from sextant.paper.b_protector_main import PacedTradingClient

    sent: list[str] = []

    class Counted(FakeClient):
        def get_all_positions(self) -> list[Position]:
            sent.append("get_all_positions")
            return super().get_all_positions()

        def get_orders(self, filter=None) -> list[Order]:
            sent.append("get_orders")
            return super().get_orders(filter)

    pacer = paced(0)
    trading = PacedTradingClient(Counted(), pacer)  # type: ignore[arg-type]
    for read in (trading.get_all_positions, trading.get_orders):
        with pytest.raises(PacerRefused):
            read()
    pacer.bucket.tokens = 5
    pacer.drain()
    assert sent == []

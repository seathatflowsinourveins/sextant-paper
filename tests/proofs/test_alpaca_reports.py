"""Offline report proof from the pinned SDK schema and Nautilus adapter template.

Sources read 2026-11-10: alpaca-py@cc4cb3b7 alpaca/trading/{models,enums}.py
and NautilusTrader@d53849b python/tests/integration/test_python_adapter_template.py.
Synthetic SDK snapshots follow the Alpaca streaming and position examples
and the lead's venue fake on lead/b-event-book-on-main.
"""

from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import pytest
from alpaca.trading.models import Order, Position, TradeUpdate
from alpaca.trading.stream import TradingStream
from nautilus_trader.common import Logger
from nautilus_trader.model import (
    AccountId,
    AvgPxReconciliation,
    ClientOrderId,
    Currency,
    Equity,
    InstrumentId,
    LiquiditySide,
    Money,
    OrderSide,
    OrderStatus,
    OrderStatusReport,
    OrderType,
    PositionSide,
    Price,
    Symbol,
    TimeInForce,
    TradeId,
    TrailingOffsetType,
    TriggerType,
    VenueOrderId,
)

from sextant.alpaca import reports as parsing
from sextant.alpaca.reports import fill_report, order_status_report, position_status_report

ACCOUNT = AccountId("ALPACA-TEST")
INSTRUMENT = InstrumentId.from_str("TEST.ALPACA")
INITIALIZED_NS = 3_000_000_000
EQUITY = Equity(
    instrument_id=INSTRUMENT,
    raw_symbol=Symbol("TEST"),
    currency=Currency.from_str("USD"),
    price_precision=4,
    price_increment=Price.from_str("0.0001"),
    ts_event=0,
    ts_init=0,
)


def snapshot(**fields: object) -> Order:
    return Order.model_validate(
        {
            "id": str(UUID(int=1)),
            "client_order_id": "synthetic-order",
            "created_at": "1970-01-01T00:00:00Z",
            "submitted_at": "1970-01-01T00:00:01Z",
            "updated_at": "1970-01-01T00:00:02.000001Z",
            "asset_class": "us_equity",
            "symbol": "TEST",
            "side": "sell",
            "type": "limit",
            "time_in_force": "day",
            "qty": "9.000",
            "filled_qty": "0",
            "limit_price": "0.1234",
            "status": "new",
            "extended_hours": False,
        }
        | fields
    )


def test_partial_limit_snapshot_preserves_decimal_fields_and_timestamps():
    order = snapshot(status="partially_filled", filled_qty="1.000", filled_avg_price="0.12345")
    report = order_status_report(
        order, account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
    )

    assert report is not None
    assert (report.account_id, report.instrument_id) == (ACCOUNT, INSTRUMENT)
    assert report.client_order_id == ClientOrderId("synthetic-order")
    assert report.venue_order_id == VenueOrderId(str(UUID(int=1)))
    assert (report.order_side, report.order_type) == (OrderSide.SELL, OrderType.LIMIT)
    assert (report.time_in_force, report.order_status) == (
        TimeInForce.DAY,
        OrderStatus.PARTIALLY_FILLED,
    )
    assert report.quantity.as_decimal() == Decimal("9")
    assert report.filled_qty.as_decimal() == Decimal("1")
    assert report.price is not None and report.price.as_decimal() == Decimal("0.1234")
    assert report.avg_px == Decimal("0.12345")
    assert (report.ts_accepted, report.ts_last, report.ts_init) == (
        1_000_000_000,
        2_000_001_000,
        INITIALIZED_NS,
    )


@pytest.mark.parametrize(
    ("status", "filled_qty", "expected"),
    [
        pytest.param("new", "0", OrderStatus.ACCEPTED, id="new"),
        pytest.param("accepted", "0", OrderStatus.ACCEPTED, id="accepted"),
        pytest.param("filled", "9", OrderStatus.FILLED, id="filled"),
        pytest.param("canceled", "0", OrderStatus.CANCELED, id="canceled"),
        pytest.param("expired", "0", OrderStatus.EXPIRED, id="expired"),
        pytest.param("rejected", "0", OrderStatus.REJECTED, id="rejected"),
        pytest.param("pending_cancel", "0", OrderStatus.PENDING_CANCEL, id="pending_cancel"),
        pytest.param("pending_replace", "0", OrderStatus.PENDING_UPDATE, id="pending_replace"),
    ],
)
def test_report_preserves_supported_venue_status(status, filled_qty, expected):
    report = order_status_report(
        snapshot(status=status, filled_qty=filled_qty),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
    )
    assert report is not None
    assert report.order_status == expected


@pytest.mark.parametrize("status", ["replaced"])
def test_venue_status_requiring_more_evidence_fails_loudly(status):
    with pytest.raises(ValueError, match="status"):
        order_status_report(
            snapshot(status=status),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
        )


def test_confirmed_trailing_successor_preserves_venue_id_and_basis_point_offset():
    successor = snapshot(
        id=str(UUID(int=2)),
        client_order_id="synthetic-successor",
        replaces=str(UUID(int=1)),
        type="trailing_stop",
        qty="9",
        limit_price=None,
        stop_price="0.1087",
        trail_percent="2.125",
        time_in_force="gtc",
    )
    stable_id = ClientOrderId("synthetic-order")
    report = order_status_report(
        successor,
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
        client_order_id=stable_id,
    )

    assert report is not None
    assert report.venue_order_id == VenueOrderId(str(UUID(int=2)))
    assert report.client_order_id == stable_id
    assert report.order_type == OrderType.TRAILING_STOP_MARKET
    assert report.time_in_force == TimeInForce.GTC
    assert report.trigger_type == TriggerType.DEFAULT
    assert report.trigger_price is not None and report.trigger_price.as_decimal() == Decimal(
        "0.1087"
    )
    assert report.trailing_offset == Decimal("212.5")
    assert report.trailing_offset_type == TrailingOffsetType.BASIS_POINTS


def test_notional_snapshot_without_share_quantity_is_refused():
    with pytest.raises(ValueError, match="share quantity"):
        order_status_report(
            snapshot(qty=None, notional="9.00"),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
        )


@pytest.mark.parametrize(
    ("side", "qty", "expected"),
    [
        pytest.param("long", "5.000", PositionSide.LONG, id="long"),
        pytest.param("short", "-5.000", PositionSide.SHORT, id="short"),
    ],
)
def test_position_uses_total_shares_and_opening_only_cost_basis(side, qty, expected):
    position = Position.model_validate(
        {
            "asset_id": str(UUID(int=3)),
            "symbol": "TEST",
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "1.23456789",
            "qty": qty,
            "qty_available": "2",
            "cost_basis": "6.17283946",
            "side": side,
        }
    )

    report = position_status_report(
        position, account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
    )

    assert report is not None
    assert (report.account_id, report.instrument_id) == (ACCOUNT, INSTRUMENT)
    assert report.position_side == expected
    assert report.quantity.as_decimal() == Decimal("5")
    assert report.avg_px_open == Decimal("1.23456789")
    assert report.avg_px_open_reconciliation == AvgPxReconciliation.OPENING_ONLY
    assert report.venue_position_id is None
    assert (report.ts_last, report.ts_init) == (INITIALIZED_NS, INITIALIZED_NS)


def trade_update(**fields: object) -> dict:
    return {
        "stream": "trade_updates",
        "data": {
            "event": "partial_fill",
            "execution_id": str(UUID(int=4)),
            "order": snapshot(
                status="partially_filled", filled_qty="4", filled_avg_price="0.125"
            ).model_dump(mode="json"),
            "timestamp": "1970-01-01T00:00:02.000002Z",
            "position_qty": "3",
            "qty": "1.000",
            "price": "0.12456789",
        }
        | fields,
    }


@pytest.mark.parametrize("event", ["fill", "partial_fill"])
def test_fill_uses_execution_fields_even_when_order_status_cannot_map(event):
    order = snapshot(status="done_for_day", filled_qty="4.5", filled_avg_price="0.125")
    update = trade_update(event=event, order=order.model_dump(mode="json"))
    commission = Money.from_str("0.01 USD")
    stable_id = ClientOrderId("synthetic-stable")

    report = fill_report(
        update,
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
        commission=commission,
        client_order_id=stable_id,
    )

    assert report is not None
    assert (report.account_id, report.instrument_id) == (ACCOUNT, INSTRUMENT)
    assert report.client_order_id == stable_id
    assert report.venue_order_id == VenueOrderId(str(UUID(int=1)))
    assert report.trade_id == TradeId(str(UUID(int=4)))
    assert report.order_side == OrderSide.SELL
    assert report.last_qty.as_decimal() == Decimal("1")
    assert report.last_px.as_decimal() == Decimal("0.1246")
    assert report.commission == commission
    assert report.liquidity_side == LiquiditySide.NO_LIQUIDITY_SIDE
    assert (report.ts_event, report.ts_init) == (2_000_002_000, INITIALIZED_NS)


@pytest.mark.parametrize("field", ["price", "qty", "execution_id"])
def test_incomplete_fill_raises_instead_of_using_order_totals(field):
    with pytest.raises(ValueError, match="fill"):
        fill_report(
            trade_update(**{field: None}),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
            commission=Money.from_str("0.01 USD"),
        )


def test_non_fill_update_has_no_fill_report():
    assert (
        fill_report(
            trade_update(event="canceled"),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
            commission=Money.from_str("0.01 USD"),
        )
        is None
    )


def test_p2_5_whole_fill_uses_instrument_quantity_precision():
    report = fill_report(
        trade_update(qty="100.0"),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
        commission=Money.from_str("0.01 USD"),
    )
    assert report is not None
    assert report.last_qty.precision == 0


def test_p2_5_fractional_order_is_refused_at_equity_size_precision():
    with pytest.raises(ValueError, match="quantity"):
        order_status_report(
            snapshot(qty="1.25"),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
        )


@pytest.mark.parametrize(
    "status", ["pending_new", "accepted_for_bidding", "done_for_day", "held", "pending_review"]
)
def test_p2_6_open_venue_order_remains_in_report(status):
    report = order_status_report(
        snapshot(status=status, qty="9"),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
    )
    assert report is not None
    assert report.order_status == OrderStatus.ACCEPTED


def test_p2_6_unknown_status_fails_the_record_loudly():
    order = snapshot(qty="9").model_copy(update={"status": "future_status"})
    with pytest.raises(ValueError, match="status"):
        order_status_report(order, account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS)


def test_p2_7_non_equity_position_does_not_abort_sibling():
    base = {
        "asset_id": str(UUID(int=3)),
        "symbol": "TEST",
        "exchange": "NASDAQ",
        "asset_class": "us_equity",
        "avg_entry_price": "1",
        "qty": "5",
        "cost_basis": "5",
        "side": "long",
    }
    positions = [
        Position.model_validate(base | {"asset_class": "crypto"}),
        Position.model_validate(base),
    ]
    reports = [
        position_status_report(
            position, account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
        )
        for position in positions
    ]
    assert reports[0] is None
    assert reports[1] is not None


def test_p2_7_batch_preserves_failures_and_contingent_owner_sell_leg():
    parse_batch = getattr(parsing, "order_status_report_results", None)
    assert parse_batch is not None, "per-record report results are missing"
    leg = snapshot(id=str(UUID(int=6)), side="sell")
    parent = snapshot(order_class="bracket", legs=[leg], side="buy").model_dump(mode="json")
    unknown = snapshot().model_dump(mode="json") | {"status": "future_status"}
    results = parse_batch(
        [(parent, EQUITY), (unknown, EQUITY), (snapshot().model_dump(mode="json"), EQUITY)],
        account_id=ACCOUNT,
        ts_init=INITIALIZED_NS,
    )
    assert results[0][0] is parent
    assert isinstance(results[0][1], ValueError)
    assert results[0][0]["legs"][0]["side"] == "sell"
    assert results[1][0] is unknown
    assert isinstance(results[1][1], ValueError)
    assert isinstance(results[2][1], OrderStatusReport)
    assert results[2][1].order_status == OrderStatus.ACCEPTED


def test_raw_fill_preserves_digits_before_instrument_rounding():
    report = fill_report(
        trade_update(price="0.123450000000000000001"),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
        commission=Money.from_str("0.01 USD"),
    )
    assert report is not None
    assert report.last_px.as_decimal() == Decimal("0.1235")
    assert report.last_px.precision == EQUITY.price_precision
    assert report.last_qty.precision == EQUITY.size_precision


@pytest.mark.parametrize("field", ["qty", "price"])
def test_raw_fill_refuses_float_input(field):
    # Synthetic malformed wire data deliberately uses a float.
    with pytest.raises(ValueError, match="decimal text"):
        fill_report(
            trade_update(**{field: 100.0}),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
            commission=Money.from_str("0.01 USD"),
        )


def test_typed_trade_update_cannot_enter_the_raw_fill_boundary():
    typed = TradeUpdate.model_validate(trade_update()["data"])
    with pytest.raises(ValueError, match="raw"):
        fill_report(
            typed,  # pyright: ignore[reportArgumentType] - deliberately malformed parser input.
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
            commission=Money.from_str("0.01 USD"),
        )


def test_order_and_position_values_use_loaded_precision():
    order = order_status_report(
        snapshot(), account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
    )
    assert order.quantity.precision == 0
    assert order.filled_qty.precision == 0
    assert order.price is not None and order.price.precision == 4


@pytest.mark.parametrize("status", ["stopped", "suspended"])
def test_nonterminal_restriction_retains_order_and_warns(status, monkeypatch):
    logger = Mock(spec=Logger)
    monkeypatch.setattr(parsing, "_log", lambda: logger)
    report = order_status_report(
        snapshot(status=status), account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
    )
    assert report.order_status == OrderStatus.ACCEPTED
    logger.warning.assert_called_once()


@pytest.mark.parametrize(
    ("status", "qty", "filled", "expected"),
    [
        ("calculated", "9", "0", OrderStatus.ACCEPTED),
        ("calculated", "9", "9", OrderStatus.FILLED),
        pytest.param("done_for_day", "100", "40", OrderStatus.PARTIALLY_FILLED, id="day_partial"),
        pytest.param(
            "calculated", "100", "40", OrderStatus.PARTIALLY_FILLED, id="calculated_partial"
        ),
        ("canceled", "100", "40", OrderStatus.CANCELED),
        ("expired", "100", "40", OrderStatus.EXPIRED),
    ],
)
def test_open_status_uses_venue_execution_totals(status, qty, filled, expected):
    report = order_status_report(
        snapshot(status=status, qty=qty, filled_qty=filled),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
    )
    assert report.order_status == expected


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        ("market", {"limit_price": None}, OrderType.MARKET),
        ("stop", {"limit_price": None, "stop_price": "0.1"}, OrderType.STOP_MARKET),
        ("stop_limit", {"stop_price": "0.1"}, OrderType.STOP_LIMIT),
    ],
)
def test_simple_order_types_keep_their_execution_fields(kind, fields, expected):
    report = order_status_report(
        snapshot(type=kind, side="buy", **fields),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
    )
    assert report.order_type == expected
    assert report.order_side == OrderSide.BUY
    if kind != "market":
        assert report.trigger_price is not None
        assert report.trigger_price.as_decimal() == Decimal("0.1")
        assert report.trigger_price.precision == 4


@pytest.mark.parametrize(
    ("tif", "expected"),
    [
        ("opg", TimeInForce.AT_THE_OPEN),
        ("cls", TimeInForce.AT_THE_CLOSE),
        ("ioc", TimeInForce.IOC),
        ("fok", TimeInForce.FOK),
    ],
)
def test_order_time_in_force_and_expiration_are_reported(tif, expected):
    report = order_status_report(
        snapshot(time_in_force=tif, expires_at="1970-01-02T00:00:00Z"),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
    )
    assert report.time_in_force == expected
    assert report.expire_time == 86_400_000_000_000
    assert report.avg_px is None


def test_trail_price_uses_price_offset():
    report = order_status_report(
        snapshot(type="trailing_stop", limit_price=None, trail_price="0.02"),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
    )
    assert report.trailing_offset == Decimal("0.02")
    assert report.trailing_offset_type == TrailingOffsetType.PRICE


@pytest.mark.parametrize(("price", "percent"), [(None, None), ("0.02", "2")])
def test_trail_requires_exactly_one_offset(price, percent):
    with pytest.raises(ValueError, match="exactly one"):
        order_status_report(
            snapshot(type="trailing_stop", trail_price=price, trail_percent=percent),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"asset_class": "crypto"},
        {"order_class": "bracket"},
        {"qty": "-1"},
        {"filled_qty": "10"},
        {"side": None},
        {"type": None},
        {"limit_price": 0.1},
    ],
)
def test_unrepresentable_order_fields_fail_the_record(fields):
    with pytest.raises(ValueError):
        order_status_report(
            snapshot(**fields), account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
        )


def position(**fields: object) -> Position:
    return Position.model_validate(
        {
            "asset_id": str(UUID(int=3)),
            "symbol": "TEST",
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "1",
            "qty": "5",
            "cost_basis": "5",
            "side": "long",
        }
        | fields
    )


def test_non_equity_position_logs_once(monkeypatch):
    logger = Mock(spec=Logger)
    monkeypatch.setattr(parsing, "_log", lambda: logger)
    assert (
        position_status_report(
            position(asset_class="crypto"),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
        )
        is None
    )
    logger.warning.assert_called_once()


@pytest.mark.parametrize(
    "fields",
    [
        {"qty": "0"},
        {"qty": "-1"},
        {"avg_entry_price": "-1"},
        {"qty": "NaN"},
    ],
)
def test_inconsistent_equity_position_fails_loudly(fields):
    with pytest.raises(ValueError):
        position_status_report(
            position(**fields), account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
        )


def test_zero_position_cost_basis_is_unknown():
    report = position_status_report(
        position(avg_entry_price="0"),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
    )
    assert report is not None
    assert report.quantity.precision == 0
    assert report.avg_px_open is None


def test_fractional_position_and_fill_raise_named_quantity_error():
    with pytest.raises(ValueError, match="quantity exceeds"):
        position_status_report(
            position(qty="1.25"), account_id=ACCOUNT, instrument=EQUITY, ts_init=INITIALIZED_NS
        )
    with pytest.raises(ValueError, match="quantity exceeds"):
        fill_report(
            trade_update(qty="1.25"),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
            commission=Money.from_str("0.01 USD"),
        )


def test_fill_falls_back_to_venue_client_identity():
    report = fill_report(
        trade_update(),
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
        commission=Money.from_str("0.01 USD"),
    )
    assert report is not None
    assert report.client_order_id == ClientOrderId("synthetic-order")


def test_malformed_decimal_fails_with_a_record_error():
    with pytest.raises(ValueError, match="decimal"):
        fill_report(
            trade_update(qty="invalid"),
            account_id=ACCOUNT,
            instrument=EQUITY,
            ts_init=INITIALIZED_NS,
            commission=Money.from_str("0.01 USD"),
        )


def test_sdk_raw_stream_reaches_fill_parser_with_decimal_text():
    # Fixture credentials construct an offline SDK stream. No connection is opened.
    stream = TradingStream("fixture-key", "fixture-secret", paper=True, raw_data=True)
    raw = stream._cast(trade_update(qty="100.000", price="0.123450000000000000001"))
    assert isinstance(raw, dict)
    report = fill_report(
        raw,
        account_id=ACCOUNT,
        instrument=EQUITY,
        ts_init=INITIALIZED_NS,
        commission=Money.from_str("0.01 USD"),
    )
    assert report is not None
    assert report.last_px.as_decimal() == Decimal("0.1235")
    assert report.last_qty.as_decimal() == Decimal("100")
    assert report.last_qty.precision == 0

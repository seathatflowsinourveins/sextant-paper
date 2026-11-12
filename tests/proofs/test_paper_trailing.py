"""Vector proof for the pure-Decimal transcription of NautilusTrader's trailing calculator.

Every case below is ported from the Rust test module in
``crates/execution/src/trailing.rs`` at ``d53849b2`` (the v2.0.0rc8 release line). The
case groups keep that file's test names so a reader can diff them one for one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sextant.paper.trailing import (
    Side,
    TrailingOffsetType,
    TriggerType,
    trailing_stop_calculate,
    trailing_stop_calculate_with_bid_ask,
    trailing_stop_calculate_with_last,
)

D = Decimal
BUY = Side.BUY
SELL = Side.SELL
PRICE = TrailingOffsetType.PRICE
BASIS_POINTS = TrailingOffsetType.BASIS_POINTS
TICKS = TrailingOffsetType.TICKS


def _d(value: object) -> Decimal | None:
    return None if value is None else D(str(value))


def calc(
    increment: str = "0.01",
    *,
    side: Side,
    offset: object,
    offset_type: TrailingOffsetType = PRICE,
    trigger: object = None,
    override: object = None,
    limit: object = None,
    limit_offset: object = None,
    trigger_type: TriggerType = TriggerType.DEFAULT,
    bid: object = None,
    ask: object = None,
    last: object = None,
) -> tuple[Decimal | None, Decimal | None]:
    return trailing_stop_calculate(
        D(increment),
        side=side,
        offset=D(str(offset)),
        offset_type=offset_type,
        trigger_px=_d(override),
        stored_trigger_px=_d(trigger),
        limit_px=_d(limit),
        limit_offset=_d(limit_offset),
        trigger_type=trigger_type,
        bid=_d(bid),
        ask=_d(ask),
        last=_d(last),
    )


def test_calculate_with_no_last_price() -> None:
    with pytest.raises(ValueError, match="last"):
        calc(
            side=SELL,
            offset=1,
            trigger=100,
            trigger_type=TriggerType.LAST_PRICE,
        )


def test_calculate_with_no_bid_ask() -> None:
    with pytest.raises(ValueError, match="bid"):
        calc(side=SELL, offset=1, trigger=100, ask=99)
    with pytest.raises(ValueError, match="ask"):
        calc(side=SELL, offset=1, trigger=100, bid=98)


@pytest.mark.parametrize(
    ("side", "trigger", "offset", "last", "expected"),
    [
        (BUY, 100, 1, 99, None),
        (BUY, 100, 1, 98, "99.0"),
        (SELL, 100, 1, 101, None),
        (SELL, 100, 1, 102, "101.0"),
    ],
)
def test_trailing_stop_market_last_price(
    side: Side, trigger: object, offset: object, last: object, expected: str | None
) -> None:
    new_trigger, new_limit = calc(
        side=side,
        offset=offset,
        trigger=trigger,
        trigger_type=TriggerType.LAST_PRICE,
        last=last,
    )
    assert new_trigger == _d(expected)
    assert new_limit is None


@pytest.mark.parametrize(
    ("side", "trigger", "offset", "ask", "bid", "expected"),
    [
        (BUY, 1505, 1, 1480, 1479, "1481.0"),
        (SELL, 1495, 1, 1521, 1520, "1519.0"),
    ],
)
def test_trailing_stop_market_default_trigger_type(
    side: Side, trigger: object, offset: object, ask: object, bid: object, expected: str
) -> None:
    new_trigger, new_limit = calc(side=side, offset=offset, trigger=trigger, ask=ask, bid=bid)
    assert new_trigger == _d(expected)
    assert new_limit is None


@pytest.mark.parametrize(
    ("side", "trigger", "offset", "last", "expected"),
    [
        (BUY, 100, 50, 98, "98.49"),
        (BUY, 100, 100, 97, "97.97"),
        (SELL, 100, 50, 102, "101.49"),
        (SELL, 100, 100, 103, "101.97"),
    ],
)
def test_trailing_stop_market_basis_points(
    side: Side, trigger: object, offset: object, last: object, expected: str
) -> None:
    new_trigger, _ = calc(
        side=side,
        offset=offset,
        offset_type=BASIS_POINTS,
        trigger=trigger,
        trigger_type=TriggerType.LAST_PRICE,
        last=last,
    )
    assert new_trigger == _d(expected)


@pytest.mark.parametrize(
    ("side", "trigger", "offset", "bid", "ask", "expected"),
    [
        (BUY, 100, 1, 98, 99, None),
        (BUY, 100, 1, 97, 98, "99.0"),
        (SELL, 100, 1, 101, 102, None),
        (SELL, 100, 1, 102, 103, "101.0"),
    ],
)
def test_trailing_stop_market_bid_ask_trigger_type(
    side: Side,
    trigger: object,
    offset: object,
    bid: object,
    ask: object,
    expected: str | None,
) -> None:
    new_trigger, _ = calc(
        side=side,
        offset=offset,
        trigger=trigger,
        trigger_type=TriggerType.BID_ASK,
        bid=bid,
        ask=ask,
    )
    assert new_trigger == _d(expected)


@pytest.mark.parametrize(
    ("side", "trigger", "offset", "last", "expected"),
    [
        (BUY, 100, 5, 98, "98.05"),
        (BUY, 100, 10, 97, "97.10"),
        (SELL, 100, 5, 102, "101.95"),
        (SELL, 100, 10, 103, "102.90"),
    ],
)
def test_trailing_stop_market_ticks(
    side: Side, trigger: object, offset: object, last: object, expected: str
) -> None:
    new_trigger, _ = calc(
        side=side,
        offset=offset,
        offset_type=TICKS,
        trigger=trigger,
        trigger_type=TriggerType.LAST_PRICE,
        last=last,
    )
    assert new_trigger == _d(expected)


@pytest.mark.parametrize(
    ("side", "trigger", "offset", "last", "bid", "ask", "expected"),
    [
        (BUY, 100, 1, 98, 97, 98, "99.0"),
        (BUY, 100, 1, 97, 96, 99, "98.0"),
        (SELL, 100, 1, 102, 102, 103, "101.0"),
        (SELL, 100, 1, 103, 101, 102, "102.0"),
    ],
)
def test_trailing_stop_market_last_or_bid_ask_trigger_type(
    side: Side,
    trigger: object,
    offset: object,
    last: object,
    bid: object,
    ask: object,
    expected: str,
) -> None:
    new_trigger, _ = calc(
        side=side,
        offset=offset,
        trigger=trigger,
        trigger_type=TriggerType.LAST_OR_BID_ASK,
        bid=bid,
        ask=ask,
        last=last,
    )
    assert new_trigger == _d(expected)


@pytest.mark.parametrize(
    ("side", "trigger", "offset", "last", "expected"),
    [
        (BUY, 100, 1, 98, "99.0"),
        (SELL, 100, 1, 102, "101.0"),
    ],
)
def test_trailing_stop_market_trigger_only_moves_in_favor(
    side: Side, trigger: object, offset: object, last: object, expected: str
) -> None:
    new_trigger, _ = calc(
        side=side,
        offset=offset,
        trigger=trigger,
        trigger_type=TriggerType.LAST_PRICE,
        last=last,
    )
    assert new_trigger == _d(expected)


@pytest.mark.parametrize(
    (
        "side",
        "trigger",
        "price",
        "offset",
        "limit_offset",
        "last",
        "expected_trigger",
        "expected_limit",
    ),
    [
        (BUY, 105, "104.5", 1, "0.5", 100, "101.0", "100.5"),
        (SELL, 95, "95.5", 1, "0.5", 100, "99.0", "99.5"),
    ],
)
def test_trailing_stop_limit_last_price(
    side: Side,
    trigger: object,
    price: object,
    offset: object,
    limit_offset: object,
    last: object,
    expected_trigger: str,
    expected_limit: str,
) -> None:
    new_trigger, new_limit = calc(
        side=side,
        offset=offset,
        trigger=trigger,
        limit=price,
        limit_offset=limit_offset,
        trigger_type=TriggerType.LAST_PRICE,
        last=last,
    )
    assert new_trigger == _d(expected_trigger)
    assert new_limit == _d(expected_limit)


@pytest.mark.parametrize(
    ("offset_type", "offset", "side", "last", "expected"),
    [
        (PRICE, "1.25", BUY, 98, "99.25"),
        (BASIS_POINTS, 50, BUY, 98, "98.49"),
        (TICKS, 5, SELL, 102, "101.95"),
    ],
)
def test_calculate_with_last_uses_decimal_math(
    offset_type: TrailingOffsetType, offset: object, side: Side, last: object, expected: str
) -> None:
    result = trailing_stop_calculate_with_last(
        D("0.01"), offset_type, side, D(str(offset)), D(str(last))
    )
    assert result == _d(expected)


@pytest.mark.parametrize(
    ("increment", "offset_type", "side", "offset", "last", "expected"),
    [
        ("0.25", BASIS_POINTS, SELL, 33, "1500.00", "1495.00"),
        ("0.25", BASIS_POINTS, BUY, 33, "1500.25", "1505.25"),
        ("0.25", PRICE, SELL, "0.30", "1500.00", "1499.75"),
        ("0.50", PRICE, SELL, "0.25", "100.50", "100.00"),
        ("0.25", PRICE, SELL, "0.10", "1500.00", "1500.00"),
        ("0.25", PRICE, SELL, "0.30", "-10.00", "-10.25"),
        ("0.01", BASIS_POINTS, SELL, "1.25", "100.00", "99.99"),
        ("0.01", PRICE, SELL, "0.005", "100.00", "100.00"),
        ("0.01", PRICE, SELL, "0.004", "100.00", "100.00"),
        ("0.00", PRICE, SELL, "0.30", "1500.00", "1499.70"),
    ],
)
def test_calculate_with_last_rounds_to_price_increment(
    increment: str,
    offset_type: TrailingOffsetType,
    side: Side,
    offset: object,
    last: object,
    expected: str,
) -> None:
    result = trailing_stop_calculate_with_last(
        D(increment), offset_type, side, D(str(offset)), D(str(last))
    )
    assert result == _d(expected)


@pytest.mark.parametrize(
    ("offset_type", "side", "offset", "bid", "ask", "expected"),
    [
        (PRICE, SELL, "1.25", 102, 103, "100.75"),
        (BASIS_POINTS, SELL, 50, 102, 103, "101.49"),
        (TICKS, BUY, 5, 102, 103, "103.05"),
    ],
)
def test_calculate_with_bid_ask_uses_decimal_math(
    offset_type: TrailingOffsetType,
    side: Side,
    offset: object,
    bid: object,
    ask: object,
    expected: str,
) -> None:
    result = trailing_stop_calculate_with_bid_ask(
        D("0.01"), offset_type, side, D(str(offset)), D(str(bid)), D(str(ask))
    )
    assert result == _d(expected)


def test_trailing_stop_limit_basis_points() -> None:
    new_trigger, new_limit = calc(
        side=BUY,
        offset=50,
        offset_type=BASIS_POINTS,
        trigger=110,
        limit="109.5",
        limit_offset=25,
        trigger_type=TriggerType.LAST_PRICE,
        last=98,
    )
    assert new_trigger == _d("98.49")
    assert new_limit == _d("98.24")


@pytest.mark.parametrize(
    ("side", "stored_trigger", "override", "last"),
    [
        (BUY, 105, 100, 101),
        (SELL, 95, 100, 99),
    ],
)
def test_trigger_price_override_takes_precedence(
    side: Side, stored_trigger: object, override: object, last: object
) -> None:
    new_trigger, new_limit = calc(
        side=side,
        offset=1,
        trigger=stored_trigger,
        override=override,
        trigger_type=TriggerType.LAST_PRICE,
        last=last,
    )
    assert new_trigger is None
    assert new_limit is None


@pytest.mark.parametrize(
    ("side", "trigger", "price", "expected_trigger", "expected_limit"),
    [
        (BUY, 100, "105", None, "100.50"),
        (BUY, 105, "100", "101.00", None),
        (SELL, 100, "95", None, "99.50"),
        (SELL, 95, "100", "99.00", None),
    ],
)
def test_trailing_stop_limit_prices_move_independently(
    side: Side,
    trigger: object,
    price: object,
    expected_trigger: str | None,
    expected_limit: str | None,
) -> None:
    new_trigger, new_limit = calc(
        side=side,
        offset=1,
        trigger=trigger,
        limit=price,
        limit_offset="0.5",
        trigger_type=TriggerType.LAST_PRICE,
        last=100,
    )
    assert new_trigger == _d(expected_trigger)
    assert new_limit == _d(expected_limit)


@pytest.mark.parametrize(
    ("side", "trigger", "last", "expected_trigger", "expected_limit"),
    [
        (BUY, 110, 98, "99.00", "98.50"),
        (BUY, 110, 103, "102.00", "101.50"),
        (SELL, 90, 103, "102.00", "102.50"),
        (SELL, 90, 98, "99.00", "99.50"),
    ],
)
def test_trailing_stop_limit_last_or_bid_ask_keeps_best_prices(
    side: Side,
    trigger: object,
    last: object,
    expected_trigger: str,
    expected_limit: str,
) -> None:
    new_trigger, new_limit = calc(
        side=side,
        offset=1,
        trigger=trigger,
        limit=trigger,
        limit_offset="0.5",
        trigger_type=TriggerType.LAST_OR_BID_ASK,
        bid=100,
        ask=101,
        last=last,
    )
    assert new_trigger == _d(expected_trigger)
    assert new_limit == _d(expected_limit)

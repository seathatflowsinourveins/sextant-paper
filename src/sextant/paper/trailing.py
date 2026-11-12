"""NautilusTrader's trailing-stop calculator, transcribed to pure Decimal.

Source: ``nautilus_trader`` at ``d53849b2`` (the v2.0.0rc8 release line),
``crates/execution/src/trailing.rs``.  ``trailing_stop_calculate_with_last`` is the body at
lines 144-198; ``trailing_stop_calculate_with_bid_ask`` is lines 230-245; and
``trailing_stop_calculate`` is lines 31-186.  The port is mechanical: the same offsets,
the same "only improve" rule, the same half-to-even rounding and the same fallback to the
nearest price increment.  The Rust ``OrderAny`` is flattened into its fields, because this
repository has no order object to hand the calculator.

``limit_offset`` being present is what makes this a trailing *stop-limit* calculation: the
Rust function reads that fact from ``OrderType::TrailingStopLimit`` and treats a missing
``limit_offset`` there as an error.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum

TEN_THOUSAND = Decimal(10_000)


class TrailingCalculationError(ValueError):
    """The calculator was asked for a price it cannot compute from the inputs given."""


class Side(StrEnum):
    """The order side, in the values the alpaca-py boundary uses."""

    BUY = "buy"
    SELL = "sell"


class TrailingOffsetType(StrEnum):
    """How ``offset`` is expressed, matching the upstream enum's variants."""

    PRICE = "price"
    BASIS_POINTS = "basis_points"
    TICKS = "ticks"


class TriggerType(StrEnum):
    """Which market signal drives the trigger, matching the upstream enum's variants."""

    DEFAULT = "default"
    LAST_PRICE = "last_price"
    MARK_PRICE = "mark_price"
    BID_ASK = "bid_ask"
    LAST_OR_BID_ASK = "last_or_bid_ask"


def _precision(increment: Decimal) -> int:
    exponent = increment.as_tuple().exponent
    if not isinstance(exponent, int):
        raise TrailingCalculationError("price_increment must be a finite decimal")
    return max(0, -exponent)


def _from_decimal_dp(value: Decimal, precision: int) -> Decimal:
    """``Price::from_decimal_dp``: round half to even at the increment's precision."""

    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN)


def _round_to_increment(price: Decimal, increment: Decimal, precision: int) -> Decimal:
    rounded = _from_decimal_dp(price, precision)
    magnitude = abs(increment)
    if magnitude == 0 or rounded % magnitude == 0:
        return rounded
    # The increment is coarser than its precision, such as 0.25: use its nearest multiple.
    try:
        increments = (price / magnitude).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        nearest = increments * magnitude
    except (InvalidOperation, ArithmeticError):  # pragma: no cover - Decimal has no overflow
        nearest = price
    return _from_decimal_dp(nearest, precision)


def _better(side: Side, candidate: Decimal, current: Decimal) -> bool:
    return candidate < current if side is Side.BUY else candidate > current


def _maybe_move(side: Side, current: Decimal | None, candidate: Decimal) -> Decimal | None:
    """Return the candidate only when it improves on ``current``; never lower the bar."""

    if current is None or _better(side, candidate, current):
        return candidate
    return None


def trailing_stop_calculate_with_last(
    price_increment: Decimal,
    offset_type: TrailingOffsetType,
    side: Side,
    offset: Decimal,
    last: Decimal,
) -> Decimal:
    """The trigger price for one basis price (upstream ``trailing.rs:144-198``)."""

    if offset_type is TrailingOffsetType.PRICE:
        distance = offset
    elif offset_type is TrailingOffsetType.BASIS_POINTS:
        distance = last * offset / TEN_THOUSAND
    elif offset_type is TrailingOffsetType.TICKS:
        distance = offset * price_increment
    else:  # pragma: no cover - the enum has no other members
        raise TrailingCalculationError(f"unsupported trailing offset type: {offset_type}")
    price = last + distance if side is Side.BUY else last - distance
    return _round_to_increment(price, price_increment, _precision(price_increment))


def trailing_stop_calculate_with_bid_ask(
    price_increment: Decimal,
    offset_type: TrailingOffsetType,
    side: Side,
    offset: Decimal,
    bid: Decimal,
    ask: Decimal,
) -> Decimal:
    """The trigger price from a quote (upstream ``trailing.rs:230-245``)."""

    basis = ask if side is Side.BUY else bid
    return trailing_stop_calculate_with_last(price_increment, offset_type, side, offset, basis)


def trailing_stop_calculate(
    price_increment: Decimal,
    *,
    side: Side,
    offset: Decimal,
    offset_type: TrailingOffsetType,
    trigger_type: TriggerType = TriggerType.DEFAULT,
    stored_trigger_px: Decimal | None = None,
    trigger_px: Decimal | None = None,
    limit_px: Decimal | None = None,
    limit_offset: Decimal | None = None,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    last: Decimal | None = None,
) -> tuple[Decimal | None, Decimal | None]:
    """The improved trigger and limit prices, or ``None`` where a price did not improve.

    ``trigger_px`` is the caller's override; otherwise the order's stored
    ``stored_trigger_px`` seeds the trigger.  A ``limit_offset`` makes the calculation a
    trailing stop-limit (upstream ``OrderType::TrailingStopLimit``).
    """

    trigger = trigger_px if trigger_px is not None else stored_trigger_px
    limit = limit_px
    new_trigger: Decimal | None = None
    new_limit: Decimal | None = None
    is_limit = limit_offset is not None

    def move(current: Decimal | None, candidate: Decimal) -> Decimal | None:
        return _maybe_move(side, current, candidate)

    if trigger_type in (TriggerType.LAST_PRICE, TriggerType.MARK_PRICE):
        if last is None:
            raise TrailingCalculationError(f"last price required for {trigger_type}")
        moved_trigger = move(
            trigger,
            trailing_stop_calculate_with_last(price_increment, offset_type, side, offset, last),
        )
        if moved_trigger is not None:
            trigger = moved_trigger
        new_trigger = moved_trigger
        if is_limit:
            assert limit_offset is not None
            moved_limit = move(
                limit,
                trailing_stop_calculate_with_last(
                    price_increment, offset_type, side, limit_offset, last
                ),
            )
            if moved_limit is not None:
                limit = moved_limit
            new_limit = moved_limit
    elif trigger_type in (
        TriggerType.DEFAULT,
        TriggerType.BID_ASK,
        TriggerType.LAST_OR_BID_ASK,
    ):
        if bid is None:
            raise TrailingCalculationError("bid required")
        if ask is None:
            raise TrailingCalculationError("ask required")
        basis = ask if side is Side.BUY else bid
        moved_trigger = move(
            trigger,
            trailing_stop_calculate_with_last(price_increment, offset_type, side, offset, basis),
        )
        if moved_trigger is not None:
            trigger = moved_trigger
        new_trigger = moved_trigger
        if is_limit:
            assert limit_offset is not None
            moved_limit = move(
                limit,
                trailing_stop_calculate_with_last(
                    price_increment, offset_type, side, limit_offset, basis
                ),
            )
            if moved_limit is not None:
                limit = moved_limit
            new_limit = moved_limit
        if trigger_type is TriggerType.LAST_OR_BID_ASK:
            if last is None:
                raise TrailingCalculationError("last required")
            updated_trigger = move(
                trigger,
                trailing_stop_calculate_with_last(price_increment, offset_type, side, offset, last),
            )
            if updated_trigger is not None:
                trigger = updated_trigger
                new_trigger = updated_trigger
            if is_limit:
                assert limit_offset is not None
                updated_limit = move(
                    limit,
                    trailing_stop_calculate_with_last(
                        price_increment, offset_type, side, limit_offset, last
                    ),
                )
                if updated_limit is not None:
                    limit = updated_limit
                    new_limit = updated_limit
    else:  # pragma: no cover - the enum has no other members
        raise TrailingCalculationError(f"unsupported trigger type: {trigger_type}")
    return new_trigger, new_limit

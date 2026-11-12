"""N4 price precision and entry chase bounds; Alpaca Orders as cited in ADR-0004."""

from decimal import ROUND_CEILING, Decimal

from sextant.entry.parameters import EntryParameters, number


def limit_price(ask: Decimal, price_increment: Decimal) -> Decimal:
    """Round a buy up to the ask on both the instrument tick and the $1 ladder."""
    ask, tick = number(ask, "ask"), number(price_increment, "price_increment")
    ladder = Decimal("0.0001") if ask < 1 else Decimal("0.01")
    increment = max(tick, ladder)
    price = (ask / increment).to_integral_value(rounding=ROUND_CEILING) * increment
    if price >= 1:
        increment = max(tick, Decimal("0.01"))
        price = (price / increment).to_integral_value(rounding=ROUND_CEILING) * increment
    return price


def within_chase(price: Decimal, first_limit: Decimal, params: EntryParameters) -> bool:
    return price <= first_limit * (1 + params["entry_chase_bps"] / 10_000)


def funding_unit_cost(price: Decimal, commission_rate: Decimal | None, precision: int) -> Decimal:
    """N5: reserve the declared fee bound, rounded up per whole-share fill."""
    rate = number(commission_rate, "commission_rate", zero=True)
    quantum = Decimal(1).scaleb(-precision)
    return price + (price * rate).quantize(quantum, rounding=ROUND_CEILING)

"""Parse an Alpaca asset row into a Nautilus ``Equity``.

Shape follows nautilus_trader@d53849b ``docs/concepts/instruments/equity.md``.
Prices use the engine's tiered tick scheme (precedent
``crates/model/src/instruments/tick_scheme.rs:150-217``): Alpaca allows two
decimals at or above $1.00 and four below
(https://docs.alpaca.markets/us/docs/orders-at-alpaca, updated 2026-10-13, read
2026-11-10). Every equity gets precision 4, the 0.0001 increment and the
``ALPACA_US_EQUITY`` scheme, whose open tier ends at $10,000.99.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alpaca.trading.models import Asset
from nautilus_trader.model import (
    Currency,
    Equity,
    InstrumentId,
    Price,
    Symbol,
    TieredTickScheme,
    list_tick_schemes,
    register_tick_scheme,
)

from .constants import VENUE

_USD = Currency.from_str("USD")
_SUB_PENNY = "0.0001"
_PRICE_PRECISION = 4

ALPACA_TICK_SCHEME = "ALPACA_US_EQUITY"
_PRICE_TIERS = ((0.0, 1.0, 0.0001), (1.0, float("inf"), 0.01))
_MAX_TICKS_PER_TIER = 1_000_000

_VENDOR_FLAGS = ("tradable", "shortable", "easy_to_borrow", "marginable", "fractionable")


def register_alpaca_equity_tick_scheme() -> str:
    """Register the Alpaca equity tick ladder once, and return its name."""
    if ALPACA_TICK_SCHEME not in list_tick_schemes():
        register_tick_scheme(
            TieredTickScheme(
                ALPACA_TICK_SCHEME,
                _PRICE_TIERS,
                _PRICE_PRECISION,
                _MAX_TICKS_PER_TIER,
            ),
        )
    return ALPACA_TICK_SCHEME


def _read(asset: Asset | Mapping[str, Any], name: str) -> Any:
    if isinstance(asset, Mapping):
        return asset.get(name)
    return getattr(asset, name, None)


def parse_equity(asset: Asset | Mapping[str, Any], *, ts_init: int) -> Equity:
    """Build an ALPACA Equity from one row; reads the symbol and vendor flags."""
    symbol_value = _read(asset, "symbol")
    if not isinstance(symbol_value, str) or not symbol_value:
        raise ValueError("Alpaca asset row has no symbol")
    symbol = Symbol(symbol_value)
    return Equity(
        instrument_id=InstrumentId(symbol, VENUE),
        raw_symbol=symbol,
        currency=_USD,
        price_precision=_PRICE_PRECISION,
        price_increment=Price.from_str(_SUB_PENNY),
        ts_event=ts_init,
        ts_init=ts_init,
        tick_scheme=register_alpaca_equity_tick_scheme(),
        info={name: bool(_read(asset, name)) for name in _VENDOR_FLAGS},
    )

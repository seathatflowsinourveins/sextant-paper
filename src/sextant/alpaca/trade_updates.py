"""Lossless trade amendments; Nautilus rc8 has no native correction/cancel type.

alpaca-py cc4cb3b: data/mappings.py:62-84, models/trades.py:120-209.
NautilusTrader d53849b: crates/model/src/data/mod.rs:118-138 (native types),
python/nautilus_trader/model/__init__.pyi:1673-1707 (CustomData/DataType).
These are stream/bus records only; catalog serialization is outside this part.
"""

from dataclasses import dataclass
from decimal import Decimal

from msgpack import Timestamp
from nautilus_trader.model import CustomData, DataType, InstrumentId, Symbol, Venue


@dataclass(frozen=True, slots=True)
class _OriginalTrade:
    instrument_id: InstrumentId
    symbol: str
    exchange: str
    original_id: int
    original_price: Decimal
    original_size: Decimal
    tape: str
    ts_event: int
    ts_init: int


@dataclass(frozen=True, slots=True)
class AlpacaTradeCorrection(_OriginalTrade):
    corrected_id: int
    corrected_price: Decimal
    corrected_size: Decimal
    original_conditions: tuple[str, ...]
    corrected_conditions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlpacaTradeCancel(_OriginalTrade):
    action: str


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "*" in value:
        raise ValueError("Trade update requires nonempty named identity fields")
    return value


def _trade_id(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Trade update requires its vendor trade ID")
    return value


def _number(value: object, *, size: bool = False) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or (not size and number == 0):
        raise ValueError("Trade update requires finite price and size")
    if size and number != number.to_integral_value():
        raise ValueError("Trade update size must be a whole count")
    return number


def _conditions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Trade update conditions must be a string list")
    return tuple(value)


def _trade_update(raw: dict, venue: Venue, feed: str, receipt: int) -> CustomData:
    """Parse raw c/x inside the row boundary, before the SDK's float casts.

    SDK websocket.py:294-317 casts the wire timestamp through datetime.
    Instead retain wire t using msgpack 1.2.4 Timestamp.to_unix_nano(),
    documented at https://msgpack-python.readthedocs.io/en/latest/api.html
    (read 2026-11-11); local receipt stays separate. Keep the original
    vendor ID without TradeTick's 36-character truncation.
    """
    correction = raw["T"] == "c"
    if not correction and raw["T"] != "x":
        raise ValueError("Expected trade correction or cancel")
    symbol, exchange, tape = (_text(raw[key]) for key in ("S", "x", "z"))
    timestamp = raw["t"]
    if not isinstance(timestamp, Timestamp):
        raise TypeError("Trade update requires the SDK's wire timestamp")
    original = {
        "instrument_id": InstrumentId(Symbol(symbol), venue),
        "symbol": symbol,
        "exchange": exchange,
        "original_id": _trade_id(raw["oi" if correction else "i"]),
        "original_price": _number(raw["op" if correction else "p"]),
        "original_size": _number(raw["os" if correction else "s"], size=True),
        "tape": tape,
        "ts_event": timestamp.to_unix_nano(),
        "ts_init": receipt,
    }
    if correction:
        event = AlpacaTradeCorrection(
            **original,
            corrected_id=_trade_id(raw["ci"]),
            corrected_price=_number(raw["cp"]),
            corrected_size=_number(raw["cs"], size=True),
            original_conditions=_conditions(raw["oc"]),
            corrected_conditions=_conditions(raw["cc"]),
        )
    else:
        action = raw["a"]
        if action not in ("C", "E"):
            raise ValueError("Unknown trade cancel/error action")
        event = AlpacaTradeCancel(**original, action=action)
    return CustomData(DataType(type(event).__name__, metadata={"feed": feed}), event)

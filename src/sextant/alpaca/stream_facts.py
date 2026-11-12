"""Per-trade vendor facts and stream gaps, as bus data beside the engine's own ticks (N1e).

rc8's ``TradeTick`` has no slot for trade conditions (``model/__init__.pyi:7034-7035``, final)
and the engine's adapters drop them (Databento ``decode/market_data.rs:313-333``). The Alpaca
data client therefore publishes ``AlpacaTradeFacts`` immediately before each trade tick from
the same node-loop callback: one client output channel delivers in order and the runner hands
each event to the data engine synchronously (``live/src/python/client/mod.rs:478-482``,
``common/src/live/sender.rs``, ``live/src/runner.rs:545-549``), so a subscriber has the facts
when the tick arrives. A reconnect is published as ``AlpacaStreamGap``: the pin has no socket
state for Python clients. DESIGN-N1E.md holds the cited reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from alpaca.data.models import Trade
from nautilus_trader.model import DataType, InstrumentId, TradeTick


@dataclass(frozen=True, slots=True)
class AlpacaTradeFacts:
    instrument_id: InstrumentId
    trade_id: str  # the tick's own TradeId value
    conditions: tuple[str, ...] | None  # None: the vendor's conditions are unknown
    tape: str | None
    ts_event: int
    ts_init: int
    # The vendor's own price and size, Decimal(str(value)) as the stream delivered them: the
    # tick holds the price at instrument precision only (N1f; amendment identity, raw to raw).
    price: Decimal | None = None
    size: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AlpacaStreamGap:
    feed: str
    connects: int  # the connection number that ended the gap (2 for the first reconnect)
    ts_event: int
    ts_init: int


TRADE_ID_MAX = 36  # NautilusTrader rc8 crates/model/src/identifiers/trade_id.rs:50
AMENDMENTS = ("AlpacaTradeCorrection", "AlpacaTradeCancel")  # trade_updates.py data types


def vendor_trade_id(exchange: object, trade_id: object) -> str:
    """The engine ``TradeId`` value of a vendor print: the data client's one rule (N1f)."""
    venue = getattr(exchange, "value", exchange)
    return (f"{venue}-{trade_id}" if venue else str(trade_id))[:TRADE_ID_MAX]


def facts_type(feed: str) -> DataType:
    return DataType("AlpacaTradeFacts", metadata={"feed": feed})


def gap_type(feed: str) -> DataType:
    return DataType("AlpacaStreamGap", metadata={"feed": feed})


def trade_facts(row: Trade, tick: TradeTick) -> AlpacaTradeFacts:
    """Keep the conditions only as the vendor's list of strings; anything else is unknown."""
    raw = row.conditions
    known = isinstance(raw, list) and all(isinstance(code, str) and code for code in raw)
    tape = row.tape if isinstance(row.tape, str) and row.tape else None
    return AlpacaTradeFacts(
        tick.instrument_id,
        tick.trade_id.value,
        tuple(raw) if known and isinstance(raw, list) else None,
        tape,
        tick.ts_event,
        tick.ts_init,
        Decimal(str(row.price)),
        Decimal(str(row.size)),
    )


class StreamFacts:
    """The RankingActor's ``trade_facts`` source for one feed: the latest facts per instrument.

    One slot per name, so the store is bounded by the universe. A tick gets
    ``(True, conditions)`` only from facts carrying its own trade id; the mask itself is the
    ranking engine's (``ranking/engine.py`` ``price_counts``).
    """

    def __init__(self, feed: str) -> None:
        self.feed = feed
        self.facts_type, self.gap_type = facts_type(feed), gap_type(feed)
        self._latest: dict[InstrumentId, AlpacaTradeFacts] = {}

    def clear(self) -> None:
        """A (re)start holds no earlier run's facts (review P2)."""
        self._latest.clear()

    def record(self, facts: AlpacaTradeFacts) -> None:
        self._latest[facts.instrument_id] = facts

    def __call__(self, tick: TradeTick) -> tuple[bool, tuple[str, ...]] | None:
        facts = self._latest.get(tick.instrument_id)
        if facts is None or facts.trade_id != tick.trade_id.value or facts.conditions is None:
            return None
        return True, facts.conditions

    def vendor(self, tick: TradeTick) -> tuple[Decimal, Decimal] | None:
        """The raw price and size of the facts record paired with this tick, else None."""
        facts = self._latest.get(tick.instrument_id)
        if facts is None or facts.trade_id != tick.trade_id.value:
            return None
        return None if facts.price is None or facts.size is None else (facts.price, facts.size)

"""Poll SIP minute bars into the ranking seam without a stream.

The host is display-only and REST-only.  It converts each vendor bar into the
bar-fidelity representation the OMH e2e validated: the close prints once, the
vendor trade count ``n`` enters the trade-count history as ``n - 1`` counted
twins, and the segment prefix is anchored once at the segment start so that the
first ranking minute is computable exactly as in the replay.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model import (
    AggressorSide,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)

from sextant.execution.custody import WriterLease
from sextant.ranking.generation import Coverage, CoverageState
from sextant.ranking.publisher import RankingPublisher
from sextant.tradingview.bars import Bar, BarPoll
from sextant.tradingview.inputs import QUALIFIED
from sextant.tradingview.producer import Clocks, LiveProducer

VENUE = "SIM"


def load_symbol_map(path: Path) -> dict[str, str | None]:
    """Read the operator's exact symbol to exchange-prefixed routing map."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("the symbol map must be readable JSON") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("the symbol map must be a non-empty JSON object")
    result: dict[str, str | None] = {}
    targets: set[str] = set()
    for symbol, mapping in data.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol map keys must be tickers")
        if mapping is None:
            result[symbol] = None
            continue
        if not isinstance(mapping, str) or QUALIFIED.fullmatch(mapping) is None:
            raise ValueError("symbol map values must be exchange-prefixed mappings")
        venue = mapping.split(":", 1)[0]
        if venue in {"SMART", "IB", "IBKR", "ALPACA"}:
            raise ValueError("symbol map values need a venue exchange, not routing")
        if mapping in targets:
            raise ValueError("symbol map targets must be unique")
        targets.add(mapping)
        result[symbol] = mapping
    return result


class BarFidelityHost:
    """One segment's bar-fidelity publisher, fed by polled, feed-labelled minute bars."""

    def __init__(
        self,
        publisher: RankingPublisher,
        *,
        lease: WriterLease,
        sip_authority: str = "",
        feed_authority: str = "",
        symbol_map: Mapping[str, str | None],
    ) -> None:
        if not isinstance(symbol_map, Mapping) or not symbol_map:
            raise ValueError("a verified symbol map is required")
        self.publisher = publisher
        self.lease = lease
        self.symbol_map = dict(symbol_map)
        self.producer = LiveProducer(
            publisher, lease, sip_authority=sip_authority, feed_authority=feed_authority
        )
        self.mask_source = f"{publisher.feed} minute bars carry no condition or aggressor mask"
        self.receipt_source = (
            f"bar fidelity {publisher.feed.upper()} minute bars through the shared REST pacer"
        )
        self._universe = frozenset(publisher.universe)
        self._anchored: set[str] = set()
        self.last_close: dict[str, int] = {}
        self._last_bar: dict[str, tuple[int, Decimal, int, int]] = {}
        self._gaps: dict[str, int] = {}
        registry = publisher.registry
        self.keep_s = (
            max(
                int(registry.move_1m_window_s),
                int(registry.move_5m_window_s),
                int(registry.trades_accel_window_s) + int(registry.trades_accel_baseline_min) * 60,
            )
            + 1
        )

    def ingest(self, poll: BarPoll, *, market_ns: int, received_ns: int) -> int:
        """Feed one polled batch and set the coverage it honestly observed."""
        if type(market_ns) is not int or type(received_ns) is not int:
            raise ValueError("market and receive clocks must be integers")
        if received_ns < market_ns:
            raise ValueError("the receive clock never precedes the market clock")
        requested = set(poll.requested)
        unknown = (requested | set(poll.failed)) - self._universe
        if unknown:
            raise ValueError(f"bar poll names symbols outside the universe: {sorted(unknown)}")
        missing = (self._universe - requested) | set(poll.failed)
        for symbol in missing:
            # A failed or unrequested symbol leaves its bar unverified.  The
            # prefix may not advance past that minute until the bar (or a
            # complete poll of that same minute) recovers it.
            self._gaps[symbol] = min(self._gaps.get(symbol, market_ns), market_ns)
        ingested = 0
        for item in sorted(poll.bars, key=lambda bar: (bar.close_ns, bar.symbol)):
            if self._feed(item, market_ns=market_ns, received_ns=received_ns):
                ingested += 1
        if not missing:
            # A complete poll of this minute also verifies that a symbol with
            # no bar simply had no trades in it.
            for symbol in [name for name, gap in self._gaps.items() if gap == market_ns]:
                del self._gaps[symbol]
        unverified = missing | set(self._gaps)
        coverage = (
            Coverage(CoverageState.READY, True, len(self._universe), len(self._universe), 0, 0, 0)
            if not unverified
            else Coverage(
                CoverageState.DEGRADED,
                False,
                len(self._universe),
                len(self._universe) - len(unverified),
                len(unverified),
                0,
                0,
            )
        )
        self.publisher.set_coverage(coverage)
        return ingested

    def heartbeat(self, *, at: int, received_ns: int) -> bool:
        return self.producer.heartbeat(at=at, received_ns=received_ns)

    def publish(self, path: Path, *, now: int, mapping_clocks: Clocks | None = None) -> dict:
        clocks = mapping_clocks or Clocks(now, now, self.receipt_source)
        return self.producer.publish_unit(
            path, now=now, symbol_map=self.symbol_map, mapping_clocks=clocks
        )

    def poll(
        self,
        poll: BarPoll,
        *,
        market_ns: int,
        received_ns: int,
        path: Path,
        mapping_clocks: Clocks | None = None,
    ) -> tuple[int, dict]:
        ingested = self.ingest(poll, market_ns=market_ns, received_ns=received_ns)
        if not self.heartbeat(at=market_ns, received_ns=received_ns):
            raise ValueError("the heartbeat was refused")
        # The heartbeat receipt can trail the market clock by a few nanoseconds;
        # the publication decision clock must not precede the receipt it uses.
        decision_ns = max(market_ns, received_ns)
        return ingested, self.publish(path, now=decision_ns, mapping_clocks=mapping_clocks)

    def _feed(self, item: Bar, *, market_ns: int, received_ns: int) -> bool:
        symbol = item.symbol
        if symbol not in self._universe:
            raise ValueError(f"bar outside the universe: {symbol}")
        if item.start_ns < self.publisher.segment.start_ns:
            raise ValueError(f"bar outside the segment: {symbol}")
        if item.close_ns >= self.publisher.segment.end_ns:
            # A bar closing at the boundary belongs to the next segment; never
            # let it invalidate this segment's epoch at its final clock.
            return False
        if item.close_ns > market_ns:
            raise ValueError(f"bar close is after the market clock: {symbol}")
        gap = self._gaps.get(symbol)
        if gap is not None:
            if item.close_ns != gap:
                # Older than the hole or trying to advance past it: the prefix
                # stays understated, so refuse until the missing bar arrives.
                return False
            del self._gaps[symbol]
        previous = self._last_bar.get(symbol)
        if previous is not None and item.close_ns <= previous[0]:
            if item.close_ns < previous[0]:
                return False
            if (item.close, item.volume, item.trades) == previous[1:]:
                return False
            self.producer.invalidate("a revised bar requires a new publisher epoch")
            raise ValueError(f"revised bar requires a new epoch: {symbol}")
        self.last_close[symbol] = item.close_ns
        self._last_bar[symbol] = (item.close_ns, item.close, item.volume, item.trades)
        if item.volume == 0:
            return False
        reference = self.publisher.references.get(symbol)
        if reference is None or reference.prior_close is None:
            raise ValueError("a reference close is required before the first bar")
        if symbol not in self._anchored:
            price = (
                item.open
                if item.start_ns == self.publisher.segment.start_ns
                else reference.prior_close
            )
            self._anchor(symbol, price, market_ns=market_ns, received_ns=received_ns)
            self._anchored.add(symbol)
        tick = TradeTick(
            self._instrument(symbol),
            Price.from_str(_text(item.close)),
            Quantity.from_int(item.volume),
            AggressorSide.NO_AGGRESSOR,
            TradeId(f"bar:{symbol}:{item.start_ns}"),
            item.close_ns,
            item.close_ns,
        )
        state = self.publisher.states[symbol]
        seeded = state.volume_history.append_count(tick, max(item.trades - 1, 0), self.keep_s)
        self.publisher.states[symbol] = replace(state, volume_history=seeded)
        self.producer.on_trade(
            tick,
            clocks=self._clocks(market_ns, received_ns),
            now=market_ns,
            price_eligible=True,
            volume_eligible=True,
            conditions=(),
            mask_source=self.mask_source,
        )
        return True

    def _anchor(self, symbol: str, price: Decimal, *, market_ns: int, received_ns: int) -> None:
        segment = self.publisher.segment
        if segment.name == "POST":
            # ADR-0003 K5: the regular close belongs to no extended segment, so a POST
            # anchor must land inside the segment to establish the prefix.
            self.publisher.states[symbol] = replace(
                self.publisher.states[symbol], segment_covered=True
            )
            at = segment.start_ns + 1
        else:
            at = segment.start_ns
        anchor = TradeTick(
            self._instrument(symbol),
            Price.from_str(_text(price)),
            Quantity.from_int(1),
            AggressorSide.NO_AGGRESSOR,
            TradeId(f"anchor:{symbol}"),
            at,
            at,
        )
        self.producer.on_trade(
            anchor,
            clocks=self._clocks(market_ns, received_ns),
            now=market_ns,
            price_eligible=True,
            volume_eligible=True,
            conditions=(),
            mask_source=self.mask_source,
        )
        state = self.publisher.states[symbol]
        self.publisher.states[symbol] = replace(
            state, segment_dollar_volume=state.segment_dollar_volume - price
        )

    def _clocks(self, market_ns: int, received_ns: int) -> Clocks:
        return Clocks(market_ns, received_ns, self.receipt_source)

    @staticmethod
    def _instrument(symbol: str) -> InstrumentId:
        return InstrumentId.from_str(f"{symbol}.{VENUE}")


def _text(value: Decimal) -> str:
    return format(value, "f")

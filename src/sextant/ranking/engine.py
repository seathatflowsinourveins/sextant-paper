"""Per-trade carry of northstar-lanes@00203275944a:mover.py:20-306.

Session boundaries/official closes and complete baseline curves are input receipts,
never inferred here. Publication throttling belongs to the one publisher.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from nautilus_trader.model import QuoteTick, TradeTick

from sextant.ranking.baseline import BaselineTable
from sextant.ranking.registry import Registry
from sextant.ranking.window import NS, TradeWindow

ZERO = Decimal(0)
GATES = ("mover_min_move", "move_1m_positive", "mover_min_trades_accel", "mover_min_dvol_ratio")


@dataclass(frozen=True, slots=True)
class Segment:
    name: str
    session_date: date
    start_ns: int
    end_ns: int
    source: str

    def __post_init__(self) -> None:
        if self.name not in {"PRE", "RTH", "POST", "OVERNIGHT"}:
            raise ValueError("unsupported segment")
        if not self.source or not 0 <= self.start_ns < self.end_ns:
            raise ValueError("explicit sourced session boundaries required")


@dataclass(frozen=True, slots=True)
class ReferencePrices:
    prior_close: Decimal | None
    known_at: int
    source: str
    regular_open: Decimal | None = None
    open_ts: int | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("official split-adjusted completed-close source required")
        for price in (self.prior_close, self.regular_open):
            if price is not None and (
                type(price) is not Decimal or not price.is_finite() or price <= 0
            ):
                raise ValueError("reference price must be positive finite Decimal")
        if (self.regular_open is None) != (self.open_ts is None):
            raise ValueError("regular open requires its event timestamp")


@dataclass(frozen=True, slots=True)
class MoverRow:
    symbol: str
    known_at: int
    score: Decimal | None
    rank: int | None
    move: Decimal | None
    move_1m: Decimal | None
    move_5m: Decimal | None
    dvol_ratio: Decimal | None
    trades_accel: Decimal | None
    gates_passed: tuple[str, ...]
    observations: Mapping[str, Any]
    params_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", MappingProxyType(dict(self.observations)))


@dataclass(frozen=True, slots=True)
class MoverState:
    symbol: str | None = None
    segment_key: tuple[str, Segment] | None = None
    history: TradeWindow = field(default_factory=TradeWindow)
    volume_history: TradeWindow = field(default_factory=TradeWindow)
    segment_dollar_volume: Decimal = ZERO
    latest: MoverRow | None = None
    quote: QuoteTick | None = None
    known_at: int = 0
    segment_covered: bool = False
    params_hash: str | None = None


def qualifies(row: MoverRow) -> bool:
    return all(gate in row.gates_passed for gate in GATES)


def rank_movers(rows: Iterable[MoverRow]) -> tuple[MoverRow, ...]:
    """Rank all computable rows; entry flags never become universe floors."""
    ordered = sorted(
        (row for row in rows if row.score is not None and row.trades_accel is not None),
        key=lambda row: (-row.score, -row.trades_accel, row.symbol),  # type: ignore[operator]
    )
    return tuple(replace(row, rank=i) for i, row in enumerate(ordered, 1))


# The live producer's price and volume mask (docs/handbook/rank-live.md:43, G/U/Z/6/M/9, the
# literal in tradingview/producer.py, which a proof keeps equal); the engine's own closing-print
# mask below ({"6", "M", "9"}) is a subset.
EXCLUDED_CONDITIONS = frozenset({"G", "U", "Z", "6", "M", "9"})


def price_counts(price_eligible: bool, conditions: tuple[str, ...]) -> bool:
    """A print updates price only if its source says so and no mask condition marks it."""
    return price_eligible is True and not EXCLUDED_CONDITIONS.intersection(conditions)


def advance_mover(
    state: MoverState,
    tick: TradeTick,
    references: ReferencePrices,
    baseline: BaselineTable,
    *,
    segment: Segment,
    feed: str,
    registry: Registry,
    quote: QuoteTick | None = None,
    price_eligible: bool = True,
    volume_eligible: bool = True,
    conditions: tuple[str, ...] = (),
    known_at: int | None = None,
) -> tuple[MoverState, MoverRow | None]:
    """Separate price/volume masks; closing crosses update neither history.

    Each instance owns exactly one feed/segment. A late attachment without a
    verified prefix leaves the segment ratio unknown for the whole segment.
    """
    symbol = str(tick.instrument_id.symbol)
    now = max(tick.ts_event, tick.ts_init, known_at or 0)
    key = (feed, segment)
    if baseline.feed != feed or feed not in {"sip", "boats"}:
        raise ValueError("baseline feed mismatch")
    if state.segment_key is not None and state.segment_key != key:
        raise ValueError("source/session changed; create a fresh publisher")
    if state.symbol is not None and state.symbol != symbol:
        raise ValueError("one MoverState per symbol")
    if state.params_hash is not None and state.params_hash != registry.params_hash:
        raise ValueError("parameters changed; create fresh history")
    if not segment.start_ns <= tick.ts_event < segment.end_ns:
        raise ValueError("trade outside supplied session")
    if segment.name == "POST" and tick.ts_event == segment.start_ns:
        return state, None  # ADR-0003 K5: the actual close belongs to no extended segment.
    if now < state.known_at or max(references.known_at, baseline.known_at) > now:
        raise ValueError("regressed receipt or future reference/baseline")
    if baseline.latest_session is not None and baseline.latest_session >= segment.session_date:
        raise ValueError("current/future baseline session")
    if {"6", "M", "9"}.intersection(conditions) or not (price_eligible or volume_eligible):
        return state, None
    latest_quote = quote if quote is not None else state.quote
    if latest_quote is not None:
        if latest_quote.instrument_id != tick.instrument_id:
            raise ValueError("quote symbol mismatch")
        if (
            max(latest_quote.ts_event, latest_quote.ts_init) > now
            or latest_quote.ts_event > tick.ts_event
        ):
            raise ValueError("future quote")
        if latest_quote.ts_event < segment.start_ns:
            latest_quote = None
    recent_s = int(registry.trades_accel_window_s)
    baseline_s = int(registry.trades_accel_baseline_min) * 60
    keep_s = (
        max(int(registry.move_1m_window_s), int(registry.move_5m_window_s), recent_s + baseline_s)
        + 1
    )
    history = state.history.append(tick, keep_s) if price_eligible else state.history
    volumes = state.volume_history.append(tick, keep_s) if volume_eligible else state.volume_history
    covered = state.segment_covered or tick.ts_event == segment.start_ns
    dv = state.segment_dollar_volume
    if volume_eligible:
        dv += tick.price.as_decimal() * tick.size.as_decimal()
    last = history.price_at(tick.ts_event)
    price = last.price.as_decimal() if last else None
    move = (
        price / references.prior_close - 1 if price is not None and references.prior_close else None
    )
    p1 = history.price_at(tick.ts_event - registry.seconds("move_1m_window_s"))
    p5 = history.price_at(tick.ts_event - registry.seconds("move_5m_window_s"))
    move_1m = price / p1.price.as_decimal() - 1 if p1 and price is not None else None
    move_5m = price / p5.price.as_decimal() - 1 if p5 and price is not None else None
    recent_start = tick.ts_event - recent_s * NS
    prior_start = recent_start - baseline_s * NS
    _, trades = volumes.totals(recent_start, tick.ts_event)
    _, prior = volumes.totals(prior_start, recent_start)
    prior_covered = volumes.price_at(prior_start) is not None
    accel = Decimal(trades) * baseline_s / (prior * recent_s) if prior and prior_covered else None
    normal = baseline.cumulative(
        symbol, segment.name, Decimal(tick.ts_event - segment.start_ns) / NS
    )
    ratio = dv / normal.dollar_volume if covered and normal and normal.dollar_volume > 0 else None
    reasons = tuple(
        reason
        for reason, missing in (
            ("reference_unknown", references.prior_close is None),
            ("price_unknown", price is None),
            ("price_1m_unknown", p1 is None),
            ("price_5m_unknown", p5 is None),
            ("prior_window_uncovered", not prior_covered),
            ("prior_window_zero_trades", prior_covered and not prior),
            ("segment_prefix_unknown", not covered),
            ("baseline_unknown", normal is None),
            ("baseline_zero_dollar_volume", normal is not None and normal.dollar_volume == 0),
        )
        if missing
    )
    gates = tuple(
        name
        for name, passed in (
            (GATES[0], move is not None and move >= registry.mover_min_move),
            (GATES[1], move_1m is not None and move_1m > 0),
            (GATES[2], accel is not None and accel >= registry.mover_min_trades_accel),
            (GATES[3], ratio is not None and ratio >= registry.mover_min_dvol_ratio),
        )
        if passed
    )
    from_open = (
        price / references.regular_open - 1
        if price is not None
        and references.regular_open
        and references.open_ts is not None
        and tick.ts_event >= references.open_ts
        else None
    )
    row = MoverRow(
        symbol,
        now,
        move * ratio if move is not None and ratio is not None else None,
        None,
        move,
        move_1m,
        move_5m,
        ratio,
        accel,
        gates,
        {
            "price": price,
            "dollar_volume": dv if covered else None,
            "observed_dollar_volume_lower_bound": dv,
            "spread": latest_quote.ask_price.as_decimal() - latest_quote.bid_price.as_decimal()
            if latest_quote
            else None,
            "move_from_open": from_open,
            "ts_event": tick.ts_event,
            "ts_init": tick.ts_init,
            "last_price_ts": last.ts_event if last else None,
            "reference": references.prior_close,
            "reference_source": references.source,
            "reference_known_at": references.known_at,
            "baseline_source": baseline.provenance,
            "baseline_known_at": baseline.known_at,
            "baseline_sessions": normal.sessions if normal else None,
            "baseline_dollar_volume": normal.dollar_volume if normal else None,
            "segment_total_covered": covered,
            "uncalibrated": True,
            "unknown_reasons": reasons,
        },
        registry.params_hash,
    )
    return MoverState(
        symbol, key, history, volumes, dv, row, latest_quote, now, covered, registry.params_hash
    ), row

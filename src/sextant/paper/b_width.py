"""B's trail width from each symbol's own measured noise (D-30; Entries 77 and 80; LEAD-1021).

    trail_fraction(s) = min(native, max(minimum, noise_floor(s) / basis(s),
                                        multiple x ATR(s) / basis(s)))

``noise_floor`` is ADR-0005 X2's ``max(one_tick, spread)`` (``exits.policy.noise_floor``).
ATR is NautilusTrader rc8's Wilder ``AverageTrueRange`` on completed bars, carried in Decimal
by the repo's ``DecimalATR`` (ADR-0004: the same true-range, SMA seed and Wilder formulas,
proved against the native indicator). The bounds are registry rows: ``native`` is the 2.5%
cap and the no-width fallback; ``minimum`` (0.001) is our bound, observed on paper and not
documented by the broker (``docs/handbook/lessons.jsonl:52``). Every value is UNCALIBRATED.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from sextant.entry.indicators import DecimalATR
from sextant.exits.policy import noise_floor
from sextant.paper.b_feed import BarRow
from sextant.paper.outside_rth import SUB_DOLLAR_INCREMENT
from sextant.paper.sessions import Session, session_at, session_start_ns

PERCENT_STEP = Decimal("0.01")
MAX_TRAIL_FRACTION = Decimal("0.025")  # ADR-0025 B scope: the 2.5% cap
CENT = Decimal("0.01")
MINUTE_NS = 60 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class MarketView:
    """The basis price and the noise floor's inputs from one valid quote."""

    bid: Decimal
    spread: Decimal
    one_tick: Decimal


def view_from_quote(bid: Decimal, ask: Decimal) -> MarketView:
    """The quote's bid and spread, with the tick the outside-hours calculator uses."""

    _decimals(bid, ask)
    if bid <= 0 or ask < bid:
        raise ValueError("the width takes a finite, positive and uncrossed quote")
    tick = SUB_DOLLAR_INCREMENT if bid < 1 else CENT
    return MarketView(bid=bid, spread=ask - bid, one_tick=tick)


def _decimals(*values: object) -> None:
    if not all(isinstance(value, Decimal) for value in values):
        raise TypeError("the width takes Decimal inputs only")
    if not all(value.is_finite() for value in values):  # type: ignore[attr-defined]
        raise ValueError("the width takes finite inputs only")


class _Price:
    def __init__(self, value: Decimal) -> None:
        self._value = value

    def as_decimal(self) -> Decimal:
        return self._value


class _Bar:
    def __init__(self, row: BarRow) -> None:
        self.high, self.low, self.close = _Price(row.high), _Price(row.low), _Price(row.close)


def completed_atr(
    rows: Iterable[BarRow], period: int, bar_minutes: int, now_ns: int, fetched_ns: int
) -> Decimal | None:
    """Wilder ATR over this RTH session's bars that had closed when the page was fetched
    (LEAD-1021 item 6); None outside RTH, before ``period`` bars, or unless the newest closed
    bar is the bar of the interval that has just closed at ``now_ns``. A symbol with no trade
    in that interval has no bar for it, so it gets no new width and keeps its last one."""

    if type(period) is not int or period < 1:
        raise ValueError("the ATR period must be a positive integer")
    ordered = sorted(rows, key=lambda item: item.ts_ns)
    if len({row.ts_ns for row in ordered}) != len(ordered):
        raise ValueError("the ATR takes each bar interval once, however its pages were merged")
    if session_at(now_ns) is not Session.RTH:
        return None
    open_ns, span_ns = session_start_ns(now_ns), bar_minutes * MINUTE_NS
    done = [row for row in ordered if open_ns <= row.ts_ns <= min(now_ns, fetched_ns) - span_ns]
    if not done or done[-1].ts_ns != now_ns - now_ns % span_ns - span_ns:
        return None
    atr = DecimalATR(period)
    for row in done:
        atr.handle_bar(_Bar(row))  # type: ignore[arg-type]
    return atr.value


def trail_fraction(
    *,
    native: Decimal,
    minimum: Decimal,
    multiple: Decimal,
    atr: Decimal | None,
    view: MarketView | None,
) -> Decimal | None:
    """The bounded per-symbol width, or None when the ATR or a valid quote is missing."""

    _decimals(native, minimum, multiple)
    if not 0 < minimum <= native <= MAX_TRAIL_FRACTION or multiple <= 0:
        raise ValueError(
            "the width needs 0 < minimum <= native <= MAX_TRAIL_FRACTION, multiple > 0"
        )
    if atr is None or view is None:
        return None
    _decimals(atr, view.bid, view.spread, view.one_tick)
    if atr < 0 or view.bid <= 0 or view.spread < 0 or view.one_tick <= 0:
        raise ValueError("the width takes a non-negative ATR and spread, a positive bid and tick")
    noise = noise_floor(view.one_tick, view.spread) / view.bid
    return min(native, max(minimum, noise, multiple * atr / view.bid))


def trail_percent(fraction: Decimal) -> Decimal:
    """The broker's trail_percent: rounded UP (wider) to two decimals, our formatting choice."""

    _decimals(fraction)
    if fraction <= 0:
        raise ValueError("trail_percent takes a finite, positive fraction")
    return (fraction * 100).quantize(PERCENT_STEP, rounding=ROUND_CEILING)

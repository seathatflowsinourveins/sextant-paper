"""ADR-0024 surge30 transition: pure state, no timers, I/O, orders or runtime calls.

Adapted from the pinned upstream
(northstar-lanes@00203275944a:src/northstar_lanes/surge.py, trade_window.py
``before``/``last``/``first_moved``, contracts.py::is_closing_print,
registry.py::SurgeConfig and parameters.py::QUOTE_STARTS). The caller supplies
the session ``Segment``; this module never infers a boundary. Values are the
pinned upstream registry starts, still UNCALIBRATED: no local defaults.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.model import QuoteTick, TradeTick

from sextant.entry.parameters import EntryParameters
from sextant.entry.signals import Surge
from sextant.ranking.baseline import BaselineTable
from sextant.ranking.engine import Segment
from sextant.ranking.window import NS, TradeWindow

ZERO = Decimal(0)

CLOSING_PRINTS = frozenset({"6", "M", "9"})

# Upstream keeps a bounded history floor so the first-moved print and the
# quiet-price reference survive the whole 120 s first-leg window.
KEEP_S = 361


def _validate(config: Any) -> None:
    for item in fields(config):
        value = getattr(config, item.name)
        if type(value) is not type(item.default):
            raise TypeError(f"{item.name} must be {type(item.default).__name__}, never float")
        if item.name == "status" and value != "UNCALIBRATED":
            raise ValueError("registry starts must be UNCALIBRATED")
        if isinstance(value, Decimal) and (not value.is_finite() or value < 0):
            raise ValueError(f"{item.name} must be finite and nonnegative")
        if isinstance(value, int) and (isinstance(value, bool) or value <= 0):
            raise ValueError(f"{item.name} must be a positive integer")


def _params_hash(config: Any) -> str:
    payload = {
        item.name: getattr(config, item.name) for item in fields(config) if item.name != "status"
    }
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SurgeConfig:
    """The pinned upstream surge30 registry starts; every value is UNCALIBRATED."""

    surge_window_s: int = 30
    surge_dvol_ratio_min: Decimal = Decimal("10")
    surge_min_trades: int = 5
    surge_move_min: Decimal = Decimal("0.02")
    rising_lookback_s: int = 10
    rising_trades: int = 3
    first_leg_window_s: int = 120
    # QUOTE_STARTS: {"RTH": Decimal(1), "EXTENDED": Decimal(2)} seconds.
    quote_max_age_rth_s: int = 1
    quote_max_age_extended_s: int = 2
    status: Literal["UNCALIBRATED"] = "UNCALIBRATED"

    def __post_init__(self) -> None:
        _validate(self)

    @property
    def params_hash(self) -> str:
        return _params_hash(self)


DEFAULT_CONFIG = SurgeConfig()


@dataclass(frozen=True, slots=True)
class SurgeState:
    symbol: str | None = None
    segment_key: tuple[str, Segment] | None = None
    history: TradeWindow = field(default_factory=TradeWindow)
    volume_history: TradeWindow = field(default_factory=TradeWindow)
    flag: Surge | None = None
    latched: bool = False
    quote: QuoteTick | None = None
    known_at: int = 0
    params_hash: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)


def active_flag(state: SurgeState, ts: int) -> Surge | None:
    flag = state.flag
    return flag if flag is not None and flag.known_at <= ts < flag.expires_ts else None


def is_closing_print(trade_conditions: Iterable[str]) -> bool:
    """SIP 6 (closing trade), M (official close) and 9 (corrected close).

    Source, read 2026-10-30: https://docs.alpaca.markets/us/docs/market-data-faq
    """
    return not CLOSING_PRINTS.isdisjoint(trade_conditions)


def two_sided(
    quote: QuoteTick | None,
    ts: int,
    segment: Segment,
    config: SurgeConfig = DEFAULT_CONFIG,
) -> bool:
    """A fresh, positive two-sided book; locked or one-sided refuses."""
    age_s = config.quote_max_age_rth_s if segment.name == "RTH" else config.quote_max_age_extended_s
    return bool(
        quote is not None
        and segment.start_ns <= quote.ts_event <= ts
        and ts - quote.ts_event <= age_s * NS
        and quote.bid_price.as_decimal() > ZERO
        and quote.ask_price.as_decimal() > quote.bid_price.as_decimal()
        and quote.bid_size.as_decimal() > ZERO
        and quote.ask_size.as_decimal() > ZERO
    )


def advance_surge(
    state: SurgeState,
    tick: TradeTick,
    baseline: BaselineTable,
    *,
    segment: Segment,
    feed: str,
    params: EntryParameters,
    quote: QuoteTick | None = None,
    price_eligible: bool = True,
    volume_eligible: bool = True,
    common_stock: bool = True,
    conditions: tuple[str, ...] = (),
    config: SurgeConfig = DEFAULT_CONFIG,
    known_at: int | None = None,
) -> tuple[SurgeState, Surge | None]:
    """Advance one symbol's surge state inside the caller-supplied segment.

    ``feed`` must match the baseline and be a real SIP or BOATS feed; a
    baseline whose latest session is the current or a later session, or whose
    ``known_at`` is in the future, is lookahead and refuses. ``params`` is the
    entry registry authority for the first-leg window and must agree with the
    config instead of letting a reviewed registry value be silently truncated.
    """
    if state.params_hash is not None and state.params_hash != config.params_hash:
        raise ValueError("parameters changed; reinitialize surge history")
    symbol = str(tick.instrument_id.symbol)
    if state.symbol is not None and state.symbol != symbol:
        raise ValueError("one SurgeState per symbol")
    if baseline.feed != feed or feed not in {"sip", "boats"}:
        raise ValueError("baseline feed mismatch")
    key = (segment.name, segment)
    # The segment transition happens before any ineligible/closing skip, so a
    # new segment can never inherit the previous run's flag or latch.
    if state.segment_key is not None and state.segment_key != key:
        state = SurgeState(symbol=symbol, segment_key=key)
    if not segment.start_ns <= tick.ts_event < segment.end_ns:
        raise ValueError("trade outside supplied segment")
    now = max(state.known_at, tick.ts_event, tick.ts_init, known_at or 0)
    if baseline.known_at > now:
        raise ValueError("future baseline")
    if baseline.latest_session is not None and baseline.latest_session >= segment.session_date:
        raise ValueError("current/future baseline session")
    window_ns = params.ns("first_leg_window_s")
    if window_ns != config.first_leg_window_s * NS:
        raise ValueError("first_leg_window_s disagrees with entry registry")
    if is_closing_print(conditions) or not (price_eligible or volume_eligible):
        return state, None
    # Keep the newest quote watermark: an older update never replaces a newer
    # one, even when the newer book is crossed or one-sided. Freshness is
    # measured at availability (``now``), not at the triggering trade's event.
    latest_quote = state.quote
    if quote is not None:
        if quote.instrument_id != tick.instrument_id:
            raise ValueError("quote symbol mismatch")
        if max(quote.ts_event, quote.ts_init) > now:
            raise ValueError("future quote")
        if latest_quote is None or quote.ts_event > latest_quote.ts_event:
            latest_quote = quote
    if latest_quote is not None and latest_quote.ts_event < segment.start_ns:
        latest_quote = None
    keep_s = max(KEEP_S, config.surge_window_s + 1, config.rising_lookback_s + 1)
    history = state.history.append(tick, keep_s=keep_s) if price_eligible else state.history
    volume_history = (
        state.volume_history.append(tick, keep_s=keep_s)
        if volume_eligible
        else state.volume_history
    )
    start = tick.ts_event - config.surge_window_s * NS
    quiet = history.before(start)
    earlier = history.price_at(tick.ts_event - config.rising_lookback_s * NS)
    dv, trades = volume_history.totals(start - 1, tick.ts_event)
    normal = baseline.window(
        symbol,
        segment.name,
        Decimal(tick.ts_event - segment.start_ns) / NS,
        config.surge_window_s,
    )
    normal_dv = normal.dollar_volume if normal is not None else ZERO
    ratio = dv / normal_dv if normal_dv > 0 else ZERO
    last = history.last(config.rising_trades)
    rising = bool(
        earlier is not None
        and len(last) == config.rising_trades
        and all(
            t.ts_event >= start and t.price.as_decimal() >= earlier.price.as_decimal() for t in last
        )
    )
    latest_price = history.price_at(tick.ts_event)
    price = latest_price.price.as_decimal() if latest_price is not None else None
    move = price / quiet.price.as_decimal() - 1 if quiet is not None and price is not None else ZERO
    anomaly = bool(
        quiet is not None
        and trades >= config.surge_min_trades
        and ratio >= config.surge_dvol_ratio_min
        and move >= config.surge_move_min
    )
    qualifies = bool(
        anomaly and rising and common_stock and two_sided(latest_quote, now, segment, config)
    )
    expires_ns = tick.ts_event + window_ns  # ADR-0004: 120 s from the flag print
    flag: Surge | None = None
    stored_flag = state.flag
    stored_metrics = state.metrics
    if qualifies and not state.latched and active_flag(state, now) is None and now < expires_ns:
        assert quiet is not None and earlier is not None and price is not None
        first = history.first_moved(start - 1, quiet.price.as_decimal(), config.surge_move_min)
        assert first is not None
        flag = Surge(
            tick.instrument_id,
            segment.name,
            first.ts_event,
            tick.ts_event,
            now,
            expires_ns,
            quiet.price.as_decimal(),
            "surge30",
        )
        stored_flag = flag
        stored_metrics = {
            "dollar_volume": dv,
            "trade_count": trades,
            "dvol_ratio": ratio,
            "baseline_dollar_volume": normal_dv,
            "baseline_trade_count": normal.trade_count if normal is not None else ZERO,
            "baseline_sessions": normal.sessions if normal is not None else 0,
            "baseline_known_at": baseline.known_at,
            "baseline_provenance": baseline.provenance,
            "move": move,
            "rising": rising,
            "rising_reference": earlier.price.as_decimal(),
            "quiet_ts": quiet.ts_event,
            "window_start_ts": start,
            **{f"rising_price_{i}": t.price.as_decimal() for i, t in enumerate(last)},
            "latency_ns": now - first.ts_event,
            "uncalibrated": True,
            "quote_ts": latest_quote.ts_event if latest_quote is not None else None,
            "common_stock": common_stock,
            "segment": segment.name,
            "price_eligible": price_eligible,
            "volume_eligible": volume_eligible,
        }
    # Retain the episode latch until the unusual-volume/move episode is over;
    # briefly failing the rising check must not extend the first-leg exemption.
    latched = (state.latched or flag is not None) and anomaly
    return (
        SurgeState(
            symbol=symbol,
            segment_key=key,
            history=history,
            volume_history=volume_history,
            flag=stored_flag,
            latched=latched,
            quote=latest_quote,
            known_at=now,
            params_hash=config.params_hash,
            metrics=stored_metrics,
        ),
        flag,
    )

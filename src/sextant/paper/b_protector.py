"""B's owner-fill protector on alpaca-py's TradingClient and TradingStream (clean B PR-1).

The protector covers every unreserved whole long share with exactly one native trailing stop
of the full net quantity, re-arms it on every change, and never cancels, replaces or resizes
an owner order. Outside regular market hours the native trail rests but does not trigger, so
the protector adopts or emulates its trigger from quotes through NautilusTrader's own
calculator and, after a persistent run of eligible trades, cancels the trail and releases one
marketable extended-hours LIMIT (``docs/design.md``:31 and
``docs/adr/0005-exit-engine.md`` X4; Alpaca orders page read 2026-11-09).

The trade-update stream is event-driven, so the protector also reconciles on a fixed cadence
and once when the regular session opens: a resting trail the broker lost, or a position that
rested unprotected through the close, is re-armed without waiting for an unrelated event.

Every exit decision also lands in one append-only JSONL record written off the hot path: the
trigger level and the quote behind it, the high it came from, the prints that completed a
breach, the order and the book (B-EXIT-1959; LEAD-2354 item 2).
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order, Position, TradeAccount, TradeUpdate
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    OrderRequest,
    TrailingStopOrderRequest,
)
from alpaca.trading.stream import TradingStream

from sextant.execution.pacer import PacerRefused
from sextant.exits.policy import ExitParams, noise_floor
from sextant.paper.b_feed import (
    BMarketDataFeed,
    FeedBatch,
    LatestDataClient,
    QuoteObservation,
    feed_for,
    timestamp_ns,
)
from sextant.paper.b_width import (
    MAX_TRAIL_FRACTION,
    BarRow,
    MarketView,
    completed_atr,
    trail_fraction,
    trail_percent,
    view_from_quote,
)
from sextant.paper.config import PaperAccount
from sextant.paper.decision_record import (
    Book,
    DecisionLog,
    ExitRecord,
    HighWater,
    Observation,
    TapeSpan,
    TriggerSource,
)
from sextant.paper.outside_rth import (
    OutsideRthError,
    OutsideRthTrail,
    QuoteLike,
    TriggerQuote,
    rebuild_trigger_with_source,
)
from sextant.paper.sessions import ET, Session, session_at, session_start_ns

B_LANE = "alpaca-paper-b"
TRAIL_PERCENT_NAME = "native_trail_fraction"
RECONCILE_INTERVAL_NAME = "reconcile_interval_s"
FEED_POLL_NAME = "feed_poll_interval_s"
FEED_SILENCE_NAME = "feed_silence_s"
TAPE_TIMEOUT_NAME = "tape_read_timeout_s"
TAPE_QUOTES_NAME = "tape_max_quotes"
TAPE_DEADLINE_NAME = "tape_deadline_s"
TAPE_REQUESTS_NAME = "tape_max_requests"
TAPE_BARS_NAME = "tape_max_bars"
TAPE_NAMES = (
    TAPE_TIMEOUT_NAME,
    TAPE_QUOTES_NAME,
    TAPE_DEADLINE_NAME,
    TAPE_REQUESTS_NAME,
    TAPE_BARS_NAME,
)
WIDTH_NAMES = (
    "alpaca_min_trail_fraction",
    "trail_atr_period",
    "trail_atr_bar_minutes",
    "trail_atr_multiple",
)
PARAMETER_NAMES = frozenset(
    {
        TRAIL_PERCENT_NAME,
        RECONCILE_INTERVAL_NAME,
        FEED_POLL_NAME,
        FEED_SILENCE_NAME,
        *TAPE_NAMES,
        *WIDTH_NAMES,
    }
)
PARAMETER_KEYS = frozenset({"name", "unit", "value", "source", "calibration_state", "last_change"})
FILL_EVENTS = frozenset({"fill", "partial_fill"})
TERMINAL_EVENTS = frozenset({"canceled", "expired", "rejected", "stopped", "done_for_day"})
OPEN_ORDER_LIMIT = 500
MAX_CLIENT_ORDER_ID = 128
NS_PER_SECOND = Decimal(1_000_000_000)


class ProtectorRefusal(RuntimeError):
    """The protector will not act on this account or order."""


class _StartReadExpired(BaseException):
    """The start read's interval timer fired; no library handler may swallow it."""


def _bounded(seconds: float, body: Callable[[], None]) -> str | None:
    """Run ``body`` under a one-shot ITIMER_REAL alarm; return why it stopped early, or None.

    The pattern of the signal module's documented example (docs.python.org/3/library/
    signal.html): the handler raises, so a blocking call ends there (PEP 475). Always
    disarmed, and the previous handler and timer restored, before it returns.
    """

    def expire(signum: int, frame: object) -> None:
        raise _StartReadExpired

    began = time.monotonic()
    previous = signal.signal(signal.SIGALRM, expire)
    prior = (0.0, 0.0)
    try:
        prior = signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            body()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _StartReadExpired:
        return "over its deadline"
    except Exception as exc:
        return type(exc).__name__
    finally:
        signal.signal(signal.SIGALRM, previous)
        if prior[0] > 0:
            left = max(prior[0] - (time.monotonic() - began), 1e-6)
            signal.setitimer(signal.ITIMER_REAL, left, prior[1])
    return None


@dataclass(frozen=True)
class Page:
    """One operator alert; the runner decides where it goes."""

    reason: str
    symbol: str | None = None
    detail: str = ""


def _out_of_order(previous: int | None, at: int) -> bool:
    """A quote older than the newest one already accepted for its symbol is not observed."""

    return previous is not None and at < previous


@dataclass(frozen=True)
class _PendingRelease:
    """One broken trail waiting for its terminal cancellation before the LIMIT goes out."""

    trail_order_id: str


@dataclass(frozen=True)
class _WidthReads:
    """One refresh's two reads, taken off the loop's thread and applied on it (bw2 P1)."""

    symbols: tuple[str, ...]
    now_ns: int
    rows: Mapping[str, Sequence[BarRow]] | None
    fetched_ns: int
    bars_detail: str
    quotes: Sequence[QuoteObservation] | None
    quote_detail: str


@dataclass
class _PendingDecision:
    """One release decision captured at its breach, written once when it resolves."""

    at_ns: int
    trigger: Decimal | None
    source: TriggerSource | None
    high: HighWater | None
    observations: tuple[Observation, ...]
    book: Book
    quantity: Decimal
    noise: Decimal | None
    noise_detail: dict[str, str]
    price: Decimal


@dataclass(frozen=True)
class BParameters:
    """B's reviewed starting parameters, all UNCALIBRATED; without both tape bounds no tape."""

    native_trail_fraction: Decimal
    reconcile_interval_s: Decimal
    feed_poll_interval_s: Decimal
    feed_silence_s: Decimal
    tape_read_timeout_s: Decimal | None = None
    tape_max_quotes: Decimal | None = None
    tape_deadline_s: Decimal | None = None
    tape_max_requests: Decimal | None = None
    tape_max_bars: Decimal | None = None
    alpaca_min_trail_fraction: Decimal | None = None
    trail_atr_period: Decimal | None = None
    trail_atr_bar_minutes: Decimal | None = None
    trail_atr_multiple: Decimal | None = None

    @property
    def width_on(self) -> bool:
        """The per-symbol width runs only with all four of its rows (D-30)."""

        return all(getattr(self, name) is not None for name in WIDTH_NAMES)

    def __post_init__(self) -> None:
        fraction = _decimal(self.native_trail_fraction, TRAIL_PERCENT_NAME)
        interval = _decimal(self.reconcile_interval_s, RECONCILE_INTERVAL_NAME)
        poll = _decimal(self.feed_poll_interval_s, FEED_POLL_NAME)
        silence = _decimal(self.feed_silence_s, FEED_SILENCE_NAME)
        object.__setattr__(self, "native_trail_fraction", fraction)
        object.__setattr__(self, "reconcile_interval_s", interval)
        object.__setattr__(self, "feed_poll_interval_s", poll)
        object.__setattr__(self, "feed_silence_s", silence)
        if fraction <= 0 or fraction > MAX_TRAIL_FRACTION:
            raise ValueError("native_trail_fraction must be in (0, 0.025]")
        if interval <= 0:
            raise ValueError("reconcile_interval_s must be positive")
        if poll <= 0:
            raise ValueError("feed_poll_interval_s must be positive")
        if silence <= 0:
            raise ValueError("feed_silence_s must be positive")
        if poll >= silence:
            raise ValueError("feed_poll_interval_s must be shorter than feed_silence_s")
        for name in (*TAPE_NAMES, *WIDTH_NAMES):
            value = getattr(self, name)
            if value is not None and _decimal(value, name) <= 0:
                raise ValueError(f"{name} must be positive")
            whole = name in (TAPE_BARS_NAME, "trail_atr_period", "trail_atr_bar_minutes")
            if whole and value is not None and _decimal(value, name) % 1:
                raise ValueError(f"{name} must be a whole number")
        minimum = self.alpaca_min_trail_fraction
        if minimum is not None and _decimal(minimum, "alpaca_min_trail_fraction") > fraction:
            raise ValueError("alpaca_min_trail_fraction must not exceed native_trail_fraction")
        for name in WIDTH_NAMES:
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name))
        minutes = self.trail_atr_bar_minutes
        if minutes is not None and minutes > 59:
            raise ValueError(
                "trail_atr_bar_minutes must be at most 59 (the bars API's minute frames)"
            )


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int) and not isinstance(value, bool):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{field} must be a decimal string") from exc
    else:
        raise ValueError(f"{field} must be a Decimal, int or string")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def load_b_parameters(path: Path) -> BParameters:
    """Read B's registry rows, refusing anything not reviewed and UNCALIBRATED."""

    try:
        rows = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path.name}") from exc
    if not isinstance(rows, list):
        raise ValueError("B parameter registry must be a list")
    values: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != PARAMETER_KEYS:
            raise ValueError("B parameter row has the wrong keys")
        name = row["name"]
        if not isinstance(name, str) or name not in PARAMETER_NAMES or name in values:
            raise ValueError("B parameter row has an unknown or duplicate name")
        if row["calibration_state"] != "UNCALIBRATED":
            raise ValueError("B parameters start UNCALIBRATED")
        for key in ("unit", "value", "source", "last_change"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"B parameter row needs a nonempty {key}")
        try:
            date.fromisoformat(row["last_change"])
        except ValueError as exc:
            raise ValueError("B parameter last_change must be an ISO date") from exc
        values[name] = _decimal(row["value"], name)
    if set(values) != PARAMETER_NAMES:
        raise ValueError("B parameter registry is incomplete")
    return BParameters(
        values[TRAIL_PERCENT_NAME],
        values[RECONCILE_INTERVAL_NAME],
        values[FEED_POLL_NAME],
        values[FEED_SILENCE_NAME],
        *(values[name] for name in (*TAPE_NAMES, *WIDTH_NAMES)),
    )


class TradingClientLike(Protocol):
    def get_account(self) -> TradeAccount: ...

    def get_all_positions(self) -> list[Position]: ...

    def get_orders(self, filter: GetOrdersRequest | None = None) -> list[Order]: ...

    def get_order_by_client_id(self, client_id: str) -> Order: ...

    def submit_order(self, order_data: OrderRequest) -> Any: ...

    def cancel_order_by_id(self, order_id: object) -> None: ...


class HistoricalTape(Protocol):
    """One page of the historical-quote read and its continuation token; alpaca-py at run."""

    def quote_page(
        self,
        symbol: str,
        start_ns: int,
        end_ns: int,
        feed: str,
        page_token: str | None = None,
    ) -> tuple[Sequence[QuoteLike], str | None]: ...


class HistoricalBars(Protocol):
    """One batched page of the held symbols' bars; alpaca-py at run (b_feed.bar_page)."""

    def bar_page(
        self, symbols: Sequence[str], bar_minutes: int, now_ns: int
    ) -> tuple[Mapping[str, Sequence[BarRow]], str | None, int]: ...


LatestQuotes = Callable[[Sequence[str], str], Sequence[QuoteObservation]]


@dataclass(frozen=True)
class TapeRead:
    """One start read: its held quantity, its status and the level of the rows it read.

    When ``ok`` the level is the whole span's; when ``partial`` it is a lower bound from the
    rows actually read since adoption (R3), or None when no row was read. ``source_*`` and
    ``window_*`` name the quote row that raised the level, so a decision record can say which
    bid behind the level (B-EXIT-1959).
    """

    symbol: str
    qty: Decimal
    level: Decimal | None
    status: str
    detail: str = ""
    source_bid: Decimal | None = None
    source_ask: Decimal | None = None
    source_ns: int | None = None
    window_start_ns: int | None = None
    window_end_ns: int | None = None
    feed: str = ""
    pages: int = 0


TAPE_BOUNDARIES = ((4, 0), (9, 30), (13, 0), (16, 0), (17, 0), (20, 0))
TAPE_FEEDS = {Session.PRE: "sip", Session.POST: "sip", Session.OVERNIGHT: "boats"}


def tape_windows(start_ns: int, end_ns: int) -> tuple[tuple[int, int, str], ...]:
    """The pre-market, post-market and overnight spans of a span, each on its own feed.

    Regular-hours and closed spans are never read: the native trail resting since the
    adoption instant carries the regular-hours high in its stop (G5). SIP serves pre and
    post, BOATS the overnight; the boundaries include the 13:00 and 17:00 early-close ones.
    """
    windows: list[tuple[int, int, str]] = []
    cursor = start_ns
    while cursor < end_ns:
        local = datetime.fromtimestamp(cursor // 1_000_000_000, tz=ET)
        bounds = [local.replace(hour=h, minute=m, second=0) for h, m in TAPE_BOUNDARIES]
        bounds.append((local + timedelta(days=1)).replace(hour=0, minute=0, second=0))
        stop = min(end_ns, min(timestamp_ns(at) for at in bounds if at > local))
        feed = TAPE_FEEDS.get(session_at(cursor))
        if feed is not None and windows and windows[-1][1:] == (cursor, feed):
            windows[-1] = (windows[-1][0], stop, feed)
        elif feed is not None:
            windows.append((cursor, stop, feed))
        cursor = stop
    return tuple(windows)


def _event_name(event: object) -> str:
    return str(getattr(event, "value", event))


def _leaves(order: Order) -> Decimal:
    quantity = _decimal(order.qty or "0", "order.qty")
    filled = _decimal(order.filled_qty or "0", "order.filled_qty")
    return quantity - filled


def _kind(order: Order) -> str:
    kind = getattr(order, "type", None)
    return _event_name(getattr(order, "order_type", None) if kind is None else kind)


def _is_limit(order: Order) -> bool:
    return _kind(order) == "limit"


def _safe_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)[:40]


def _state_number(value: object | None) -> str:
    """A broker value as one bare line field; logging never raises on a bad value."""

    if value is None:
        return "none"
    try:
        return format(value if isinstance(value, Decimal) else Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return "none"


class Protector:
    """B's one-account protector; the client is called, never constructed here."""

    def __init__(
        self,
        client: TradingClientLike,
        account: PaperAccount,
        parameters: BParameters,
        *,
        now_ns: Callable[[], int] = time.time_ns,
        on_page: Callable[[Page], None] | None = None,
        exit_params: ExitParams | None = None,
        feed: str = "none",
        on_state: Callable[[str], None] | None = None,
        history: HistoricalTape | None = None,
        start_client: TradingClientLike | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        decisions: DecisionLog | None = None,
        bars: HistoricalBars | None = None,
        quotes: LatestQuotes | None = None,
    ) -> None:
        if account.lane != B_LANE:
            raise ProtectorRefusal("B's protector runs only on the slot-2 B paper lane")
        self._client = client
        self._account = account
        self._parameters = parameters
        self._now_ns = now_ns
        self._on_page = on_page
        self._on_state = on_state
        self._history = history
        self._start_client = client if start_client is None else start_client
        self._monotonic = monotonic
        self.pages: list[Page] = []
        self.page_sink_failures = 0  # fallback pages whose sink raised (Codex max P1 rule)
        self.states: list[str] = []
        self._feed = feed
        self._last_quote_ns: dict[str, int] = {}
        self._width_quote_ns: dict[str, int] = {}
        self._last_poll_ns: int | None = None
        self._feed_silent: set[str] = set()
        self._age_flagged: dict[str, int] = {}
        self._announced: dict[str, tuple[object, ...]] = {}
        self._cancelling: set[str] = set()
        self._unresolved: dict[str, str] = {}
        self._last_position: dict[str, Decimal] = {}
        self._last_reserved: dict[str, Decimal] = {}
        self._exit_params = exit_params if exit_params is not None else ExitParams.starts()
        self._outside: dict[str, OutsideRthTrail] = {}
        self._pending_release: dict[str, _PendingRelease] = {}
        self._pending_rearm: dict[str, str] = {}
        self._released_symbols: set[str] = set()
        self._house: dict[str, list[Order]] = {}
        self._sequence = 0
        self._tapes: dict[str, TapeRead] = {}
        self._start_unread = False
        opened_ns = self._now_ns()
        self._feed_started_ns: int | None = opened_ns if feed != "none" else None
        self._segment_start_ns = session_start_ns(opened_ns)
        self._last_reconcile_ns = opened_ns
        self._last_session = session_at(opened_ns)
        self._decisions = decisions
        self._pending_decision: dict[str, _PendingDecision] = {}
        self._trigger_source: dict[str, TriggerSource] = {}
        self._trigger_high: dict[str, HighWater] = {}
        self._bars = bars
        self._quotes = quotes
        self.data_requests = 0  # the width's market-data requests, never wire order actions
        self._width: dict[str, Decimal] = {}
        self._width_paged: set[str] = set()
        self._native_paged: set[str] = set()  # armed at native with no width stored
        self._width_seen: set[str] = set()
        self._width_bucket: int | None = None

    def page(self, reason: str, symbol: str | None = None, detail: str = "") -> None:
        page = Page(reason=reason, symbol=symbol, detail=detail)
        self.pages.append(page)
        if self._on_page is not None:
            self._on_page(page)

    def close(self) -> None:
        """Finish the decision-record writer, fsyncing every line already queued."""

        if self._decisions is not None:
            self._decisions.close()

    def _record(self, record: ExitRecord) -> None:
        if self._decisions is not None:
            self._decisions.append(record)

    def _trigger_book(self, symbol: str, quantity: Decimal | None = None) -> Book:
        position = self._last_position.get(symbol, Decimal(0))
        reserved = self._last_reserved.get(symbol, Decimal(0))
        available = position - reserved
        return Book(
            position=position,
            reserved=reserved,
            available=available,
            quantity=available if quantity is None else quantity,
        )

    def _noise(self, state: OutsideRthTrail) -> tuple[Decimal | None, dict[str, str]]:
        spread = state.spread
        floor = None if spread is None else noise_floor(state.increment, spread)
        detail = {
            "one_tick": str(state.increment),
            "spread": "none" if spread is None else str(spread),
            "floor": "none" if floor is None else str(floor),
            "persist_count": str(self._exit_params.breach_persist_count),
            "persist_span_s": str(self._exit_params.breach_persist_span_s),
        }
        return floor, detail

    def _observations(self, state: OutsideRthTrail) -> tuple[Observation, ...]:
        seen = state.breach_observations
        noise, _ = self._noise(state)
        offset = state.breach_count - len(seen)
        return tuple(
            Observation(
                at_ns=at_ns,
                price=price,
                kind="trade",
                count=offset + index + 1,
                noise=noise,
                spread=state.spread,
            )
            for index, (at_ns, price) in enumerate(seen)
        )

    def _provenance(
        self, symbol: str, state: OutsideRthTrail | None
    ) -> tuple[TriggerSource | None, HighWater | None]:
        source = self._trigger_source.get(symbol)
        high = self._trigger_high.get(symbol)
        if source is None and state is not None and state.source is not None:
            moved = state.source
            source = TriggerSource(
                kind="live_ratchet",
                level=moved.level,
                at_ns=moved.ts_ns,
                bid=moved.bid,
                ask=moved.ask,
            )
            high = HighWater(value=moved.bid, kind="bid", at_ns=moved.ts_ns)
        return source, high

    @staticmethod
    def _order_line(
        client_order_id: str,
        quantity: int,
        price: Decimal,
        *,
        submitted: bool,
        order_id: str | None = None,
    ) -> dict[str, object]:
        line: dict[str, object] = {
            "client_order_id": client_order_id,
            "side": "sell",
            "quantity": Decimal(quantity),
            "limit_price": price,
            "extended_hours": True,
            "submitted": submitted,
        }
        if order_id is not None:
            line["order_id"] = order_id
        return line

    @staticmethod
    def _order_from_order(order: Order) -> dict[str, object]:
        line: dict[str, object] = {
            "client_order_id": order.client_order_id,
            "side": _event_name(order.side),
            "quantity": _leaves(order),
            "submitted": True,
        }
        limit_price = getattr(order, "limit_price", None)
        if limit_price is not None:
            line["limit_price"] = limit_price
        extended = getattr(order, "extended_hours", None)
        if extended is not None:
            line["extended_hours"] = bool(extended)
        return line

    def _record_release(
        self,
        symbol: str,
        context: _PendingDecision | None,
        *,
        order: dict[str, object] | None,
        outcome: str,
        note: str = "",
    ) -> None:
        if context is None:
            return
        self._record(
            ExitRecord(
                action="release",
                at_ns=context.at_ns,
                symbol=symbol,
                trigger=context.trigger,
                source=context.source,
                high=context.high,
                observations=context.observations,
                order=order,
                book=self._trigger_book(symbol, context.quantity),
                noise=context.noise,
                noise_detail=context.noise_detail,
                outcome=outcome,
                note=note,
            )
        )

    def _record_cancel(self, order: Order, reason: str) -> None:
        symbol = order.symbol
        if symbol is None:
            return
        state = self._outside.get(symbol)
        source, high = self._provenance(symbol, state)
        trigger = state.trigger if state is not None else None
        if trigger is None:
            stop = getattr(order, "stop_price", None)
            if stop is not None:
                try:
                    trigger = _decimal(str(stop), "order.stop_price")
                except ValueError:
                    trigger = None
        if source is None and trigger is not None:
            submitted = getattr(order, "submitted_at", None)
            source = TriggerSource(kind="native_stop", level=trigger, stop_price=trigger)
            high = HighWater(
                value=trigger,
                kind="native_stop",
                at_ns=timestamp_ns(submitted) if isinstance(submitted, datetime) else None,
                detail="the cancelled order's resting stop price",
            )
        if state is not None:
            observations = self._observations(state)
            noise, noise_detail = self._noise(state)
        else:
            observations, noise, noise_detail = (), None, None
        self._record(
            ExitRecord(
                action="cancel",
                at_ns=self._now_ns(),
                symbol=symbol,
                trigger=trigger,
                source=source,
                high=high,
                observations=observations,
                order=self._order_from_order(order),
                book=self._trigger_book(symbol, _leaves(order)),
                noise=noise,
                noise_detail=noise_detail,
                outcome="cancelled",
                note=reason,
            )
        )

    def bind(self) -> None:
        """Confirm the broker account number before the stream starts."""

        fetched = self._client.get_account()
        if fetched.account_number != self._account.account_id:
            self.page("account_number_mismatch")
            raise ProtectorRefusal(
                "the broker account number does not match the configured B paper account"
            )

    def emulated_trigger(self, symbol: str) -> Decimal | None:
        """The trigger the outside-RTH node is currently watching, if any."""

        state = self._outside.get(symbol)
        return None if state is None else state.trigger

    def _poll_age(self, now_ns: int) -> str:
        """How long ago the poller last succeeded, or none before its first success."""

        if self._last_poll_ns is None:
            return "none"
        elapsed_ns = max(0, now_ns - self._last_poll_ns)
        return f"{Decimal(elapsed_ns) / NS_PER_SECOND:.1f}s"

    def _quote_age(self, symbol: str, now_ns: int) -> str:
        """How old the newest quote for this symbol is, or none when it has not seen one."""

        quote_ns = self._last_quote_ns.get(symbol)
        if quote_ns is None:
            return "none"
        elapsed_ns = max(0, now_ns - quote_ns)
        return f"{Decimal(elapsed_ns) / NS_PER_SECOND:.1f}s"

    def _poll_fresh(self, now_ns: int) -> bool:
        if self._feed == "none" or self._last_poll_ns is None:
            return False
        bound_ns = int(self._parameters.feed_silence_s * NS_PER_SECOND)
        return now_ns - self._last_poll_ns <= bound_ns

    def _priceable(self, session: Session, symbol: str, now_ns: int) -> bool:
        """True while a reference that can actually protect this symbol's shares is live.

        Outside regular hours the poller is the liveness signal; its standing quote for this
        symbol is the reference. This one predicate governs the state line, the watchdog and
        the release price, so the line never claims protection the release would refuse.
        """

        if session is Session.RTH:
            return True
        if session not in (Session.PRE, Session.POST, Session.OVERNIGHT):
            return False
        return self._poll_fresh(now_ns) and symbol in self._last_quote_ns

    def _state_snapshot(self, symbol: str, now_ns: int) -> tuple[str, tuple[object, ...]]:
        """Render the one-line custody and protection state, with its change key."""

        position = self._last_position.get(symbol, Decimal(0))
        orders = [
            order
            for order in self._house.get(symbol, [])
            if str(getattr(order, "id", "")) not in self._cancelling
        ]
        trails = [order for order in orders if not _is_limit(order)]
        limit = next((order for order in orders if _is_limit(order)), None)
        trail = trails[0] if trails else None
        resting = trail or limit
        session = session_at(now_ns)
        armed = self._priceable(session, symbol, now_ns)
        protected = sum((_leaves(order) for order in trails), Decimal(0)) if armed else Decimal(0)
        trigger: object | None = None
        basis: str | None = None
        if armed and symbol not in self._released_symbols:
            if trail is not None and session is Session.RTH:
                trigger = trail.stop_price
                basis = "trade"
            elif session in (Session.PRE, Session.POST, Session.OVERNIGHT):
                state = self._outside.get(symbol)
                if state is not None and state.has_trigger and not state.released:
                    trigger = state.trigger
                    basis = "bid"
        order_id = "none" if resting is None else str(resting.id)
        tape = self._tapes.get(symbol)
        note = None if tape is None else tape.status
        line = (
            f"state mode={session.value} symbol={_safe_symbol(symbol)} "
            f"position={_state_number(position)} protected={_state_number(protected)} "
            f"trigger={_state_number(trigger)} on={basis or 'none'} order={order_id} "
            f"feed={_safe_symbol(self._feed)} poll_age={self._poll_age(now_ns)} "
            f"quote_age={self._quote_age(symbol, now_ns)}"
        )
        width, width_src = self._width_in_force(symbol)
        line += f" width={width} width_src={width_src}"
        if note is not None:
            line += f" tape={note}"
        key = (
            width,
            width_src,
            session.value,
            _state_number(position),
            _state_number(protected),
            tuple(sorted(str(order.id) for order in orders)),
            basis or "none",
            _safe_symbol(self._feed),
            note or "none",
        )
        return line, key

    def _emit_state(self, line: str) -> None:
        self.states.append(line)
        if self._on_state is not None:
            try:
                self._on_state(line)
            except Exception:
                # A broken log sink must never change the protection path.
                self._on_state = None

    def _announce_symbol(self, symbol: str, now_ns: int) -> None:
        held = symbol in self._last_position
        if not held and symbol not in self._announced:
            return
        line, key = self._state_snapshot(symbol, now_ns)
        if not held:
            self._emit_state(line)
            self._announced.pop(symbol, None)
            return
        if self._announced.get(symbol) != key:
            self._emit_state(line)
            self._announced[symbol] = key

    def _announce_states(self, now_ns: int) -> None:
        for symbol in sorted(set(self._last_position) | set(self._announced)):
            self._announce_symbol(symbol, now_ns)

    def set_feed(self, feed: str) -> None:
        """Switch the session's feed; a new observation window starts at the session boundary."""

        at = self._now_ns()
        segment_start_ns = session_start_ns(at)
        if feed == self._feed:
            if segment_start_ns == self._segment_start_ns:
                return
            self._segment_start_ns = segment_start_ns
            self._last_quote_ns.clear()
            self._feed_silent.clear()
            self._age_flagged.clear()
            for state in self._outside.values():
                state.reset_run()
            return
        self._feed = feed
        self._segment_start_ns = segment_start_ns
        self._last_quote_ns.clear()
        self._last_poll_ns = None
        self._feed_silent.clear()
        self._age_flagged.clear()
        self._feed_started_ns = at if feed != "none" else None
        for state in self._outside.values():
            state.reset_run()

    def held_symbols(self) -> tuple[str, ...]:
        """The symbols the broker currently reports as held."""

        return tuple(sorted(self._last_position))

    def trail_width(self, symbol: str) -> Decimal:
        """The symbol's last computed width; the native cap where none was ever computed."""

        return self._width.get(symbol, self._parameters.native_trail_fraction)

    def refresh_widths_when_due(self) -> bool:
        """Read and apply the widths when due, on the caller's thread (the start read, tests)."""

        due = self.width_reads_due()
        if due is not None:
            self.apply_widths(self.read_widths(*due))
        return due is not None

    def width_reads_due(self) -> tuple[tuple[str, ...], int] | None:
        """Once per completed bar interval, and once for a newly held symbol: bars and quote.

        Each refresh is one batched bars request and one batched latest-quote request for all
        held symbols together (LEAD-1021 item 6 and addendum 1), so 2 x 60 / bar minutes
        requests an hour, plus one pair when the owner adds a symbol mid-interval.
        """

        p = self._parameters
        if not p.width_on or p.trail_atr_bar_minutes is None:
            return None
        now_ns = self._now_ns()
        symbols = self.held_symbols()
        if not symbols or session_at(now_ns) is not Session.RTH:  # bars are RTH only (bw1 D)
            return None
        bucket = now_ns // int(p.trail_atr_bar_minutes * 60 * NS_PER_SECOND)
        if bucket == self._width_bucket and set(symbols) <= self._width_seen:
            return None
        self._width_bucket = bucket
        self._width_seen.update(symbols)
        return symbols, now_ns

    def read_widths(self, symbols: tuple[str, ...], now_ns: int) -> _WidthReads:
        """The bars and the quote request only, market data off the account pacer (LEAD-1445);
        no protector state but their count, so it may run off the loop."""

        assert self._parameters.trail_atr_bar_minutes is not None
        minutes = int(self._parameters.trail_atr_bar_minutes)
        rows: Mapping[str, Sequence[BarRow]] | None = None
        fetched_ns, bars_detail = now_ns, "no bars source; width kept"
        if self._bars is not None:
            self.data_requests += 1
            try:
                rows, token, fetched_ns = self._bars.bar_page(symbols, minutes, now_ns)
                if token is not None:  # a partial history gives no width (bw1 C)
                    rows, bars_detail = None, "bars page partial; width kept"
            except Exception as exc:
                bars_detail = f"bars request failed: {type(exc).__name__}; width kept"
        quotes: Sequence[QuoteObservation] | None = None
        quote_detail = "no quote source; width kept"
        if self._quotes is not None:
            self.data_requests += 1
            try:
                quotes = self._quotes(symbols, feed_for(session_at(now_ns)) or "sip")
                quote_detail = "no valid quote this interval; width kept"
            except Exception as exc:
                quote_detail = f"quote request failed: {type(exc).__name__}; width kept"
        return _WidthReads(symbols, now_ns, rows, fetched_ns, bars_detail, quotes, quote_detail)

    def apply_widths(self, reads: _WidthReads) -> None:
        """Recompute each width from completed bars and one valid quote; else keep the last."""

        p = self._parameters
        assert p.trail_atr_period is not None and p.trail_atr_bar_minutes is not None
        assert p.alpaca_min_trail_fraction is not None and p.trail_atr_multiple is not None
        symbols, now_ns, rows, bars_detail = (
            reads.symbols,
            reads.now_ns,
            reads.rows,
            reads.bars_detail,
        )
        minutes, quote_detail = int(p.trail_atr_bar_minutes), reads.quote_detail
        views: dict[str, MarketView] = {}
        session_start = session_start_ns(now_ns)
        for seen in reads.quotes or ():
            taken = (self._last_quote_ns, self._width_quote_ns)
            newest = max(seen_ns.get(seen.symbol, -1) for seen_ns in taken)
            # on_quote's own prior-segment and out-of-order rules
            if seen.ts_ns >= session_start and not _out_of_order(newest, seen.ts_ns):
                views[seen.symbol] = view_from_quote(seen.bid, seen.ask)
                self._width_quote_ns[seen.symbol] = seen.ts_ns
        for symbol in symbols:
            atr = None
            if rows is not None:
                period = int(p.trail_atr_period)
                atr = completed_atr(rows.get(symbol, ()), period, minutes, now_ns, reads.fetched_ns)
            if atr is None:
                detail = (
                    bars_detail
                    if rows is None
                    else (f"fewer than {p.trail_atr_period} completed bars; width kept")
                )
                self._width_unavailable(symbol, "dynamic_width_bars_unavailable", detail)
                continue
            fraction = trail_fraction(
                native=p.native_trail_fraction,
                minimum=p.alpaca_min_trail_fraction,
                multiple=p.trail_atr_multiple,
                atr=atr,
                view=views.get(symbol),
            )
            if fraction is None:
                self._width_unavailable(symbol, "dynamic_width_quote_unavailable", quote_detail)
                continue
            self._width[symbol] = fraction
            self._width_paged.discard(symbol)
            self._native_paged.discard(symbol)

    def _width_unavailable(self, symbol: str, reason: str, detail: str) -> None:
        """Page once per symbol per episode; an episode ends at the next computed width."""

        if symbol not in self._width_paged:
            self._width_paged.add(symbol)
            self.page(reason, symbol, detail)

    def note_poll(self, ts_ns: int | None = None) -> None:
        """Record one successful poll; liveness is measured from this receipt, not a quote."""

        at = self._now_ns() if ts_ns is None else ts_ns
        self._last_poll_ns = at if self._last_poll_ns is None else max(self._last_poll_ns, at)

    def watch(self, now_ns: int | None = None) -> None:
        """Page when the poller stops, then announce the state and any standing-quote age."""

        at = self._now_ns() if now_ns is None else now_ns
        session = session_at(at)
        if session in (Session.PRE, Session.POST, Session.OVERNIGHT):
            self._watch_feed(session, at)
            self._announce_standing_ages(at)
        else:
            self._feed_silent.clear()
            self._age_flagged.clear()
        self._announce_states(at)

    def _watch_feed(self, session: Session, now_ns: int) -> None:
        held = set(self._last_position)
        self._feed_silent &= held
        if self._poll_fresh(now_ns):
            for symbol in sorted(held):
                if symbol in self._last_quote_ns:
                    self._feed_silent.discard(symbol)
                    continue
                if symbol in self._feed_silent:
                    continue
                self._feed_silent.add(symbol)
                self.page(
                    "outside_rth_feed_silent",
                    symbol,
                    "polls succeed but no quote for this symbol; protection unarmed",
                )
            return
        bound_ns = int(self._parameters.feed_silence_s * NS_PER_SECOND)
        for symbol in sorted(held):
            if symbol in self._feed_silent:
                continue
            if self._feed == "none":
                detail = "no market-data feed; outside-RTH protection unarmed"
            elif self._last_poll_ns is None:
                started = self._feed_started_ns
                if started is None or now_ns - started <= bound_ns:
                    continue
                detail = (
                    f"no successful poll since feed {_safe_symbol(self._feed)} opened, "
                    f"bound {self._parameters.feed_silence_s}s; protection unarmed"
                )
            else:
                age_ns = max(0, now_ns - self._last_poll_ns)
                if age_ns <= bound_ns:
                    continue
                detail = (
                    f"last successful poll {Decimal(age_ns) / NS_PER_SECOND:.1f}s ago, "
                    f"bound {self._parameters.feed_silence_s}s; protection unarmed"
                )
            self._feed_silent.add(symbol)
            self.page("outside_rth_feed_silent", symbol, detail)

    def _announce_standing_ages(self, now_ns: int) -> None:
        """Emit one line when a still-armed quote crosses the silence bound.

        This is observability, not a second arm/disarm rule: the poller's success keeps the
        shares protected, and the line makes the older quote visible instead of calling it a
        silent feed.
        """

        if not self._poll_fresh(now_ns):
            return
        bound_ns = int(self._parameters.feed_silence_s * NS_PER_SECOND)
        for symbol in sorted(self._last_position):
            quote_ns = self._last_quote_ns.get(symbol)
            if quote_ns is None:
                continue
            age_ns = max(0, now_ns - quote_ns)
            if age_ns <= bound_ns:
                self._age_flagged.pop(symbol, None)
                continue
            if self._age_flagged.get(symbol) == quote_ns:
                continue
            line, key = self._state_snapshot(symbol, now_ns)
            self._emit_state(line)
            self._announced[symbol] = key
            self._age_flagged[symbol] = quote_ns

    def on_quote(
        self,
        symbol: str,
        *,
        bid: Decimal | None,
        ask: Decimal | None,
        last: Decimal | None,
        ts_ns: int | None = None,
    ) -> Decimal | None:
        """Feed one outside-RTH quote to the emulated trailing trigger.

        The node's data adapter calls this; in the regular session the native trail owns the
        trigger, so the observation is ignored there.
        """

        at = self._now_ns() if ts_ns is None else ts_ns
        session = session_at(at)
        if session not in (Session.PRE, Session.POST, Session.OVERNIGHT):
            return None
        if session is not self._last_session:
            # Adopt the resting native stop before the first observation of a new session can
            # seed an emulated trigger below it.
            self.reconcile()
        if at < self._segment_start_ns:
            return None
        if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
            return None
        previous = self._last_quote_ns.get(symbol)
        if _out_of_order(previous, at):
            return None
        self._last_quote_ns[symbol] = at if previous is None else max(previous, at)
        state = self._outside_state(symbol)
        before = state.source
        trigger = state.observe_quote(bid=bid, ask=ask, last=last, ts_ns=at)
        moved = state.source
        if moved is not None and moved != before:
            # A live quote, not the adopted row, raised the trigger: the record names it.
            self._trigger_source[symbol] = TriggerSource(
                kind="live_ratchet",
                level=moved.level,
                at_ns=moved.ts_ns,
                bid=moved.bid,
                ask=moved.ask,
            )
            self._trigger_high[symbol] = HighWater(value=moved.bid, kind="bid", at_ns=moved.ts_ns)
        return trigger

    def on_trade(self, symbol: str, price: Decimal, ts_ns: int | None = None) -> bool:
        """Feed one outside-RTH trade; true when it completes a persistent breach."""

        at = self._now_ns() if ts_ns is None else ts_ns
        session = session_at(at)
        if session not in (Session.PRE, Session.POST, Session.OVERNIGHT):
            return False
        if session is not self._last_session:
            self.reconcile()
        if at < self._segment_start_ns:
            return False
        state = self._outside.get(symbol)
        if state is None or not state.observe_trade(price, at):
            return False
        self._begin_outside_release(symbol, at)
        return True

    def _outside_state(self, symbol: str) -> OutsideRthTrail:
        state = self._outside.get(symbol)
        if state is None:
            state = OutsideRthTrail(
                trail_fraction=self._parameters.native_trail_fraction,
                params=self._exit_params,
            )
            self._outside[symbol] = state
        return state

    def rebuild_tapes(self) -> None:
        """At process start only: one bounded tape read per held position, then never again.

        In any session, the outside-hours high since each position's adoption instant is
        rebuilt from the recorded quotes before the stream starts, on the loop's own thread,
        and applied at the first outside-hours adoption. A position adopted while B runs
        needs no read: its high starts at the adoption mark. A symbol stops at its first
        failure. G1: a call starts only while one tape_read_timeout_s still fits before
        tape_deadline_s, and the standard library's interval timer on the main thread
        (signal.setitimer, ITIMER_REAL; its handler raises, so PEP 475 does not retry the
        call) ends the read at tape_deadline_s, so the first reconcile begins by then.
        Off the main thread nothing is read. Every position not completed is partial and
        keeps the level of the pages it did read (R2/R3).
        """

        history = self._history
        deadline_s = self._parameters.tape_deadline_s
        budget = self._parameters.tape_max_requests
        if history is None or deadline_s is None or budget is None:
            return
        if threading.current_thread() is not threading.main_thread():
            self.page("outside_rth_tape_unreadable", None, "start read off the main thread")
            self._start_unread = True  # the first reconcile marks each held position partial
            return
        reserve = float(self._parameters.tape_read_timeout_s or 0)
        now_ns = self._now_ns()
        deadline = self._monotonic() + float(deadline_s)
        left = [int(budget)]
        held: list[tuple[str, Decimal]] = []
        done: set[str] = set()

        def spend() -> bool:
            if left[0] < 1 or self._monotonic() + reserve > deadline:
                return False
            left[0] -= 1
            return True

        def read() -> None:
            if not spend():
                return
            for position in self._start_client.get_all_positions():
                try:
                    qty = _decimal(position.qty, "position.qty")
                except ValueError:
                    continue
                if qty > 0:
                    held.append((position.symbol, qty))
            for symbol, qty in held:
                tape = self._read_tape(history, symbol, qty, now_ns, spend)
                if tape is not None:
                    self._tapes[symbol] = tape
                done.add(symbol)

        detail = _bounded(float(deadline_s), read)
        if detail is not None and not held:
            self.page("outside_rth_tape_unreadable", None, f"positions: {detail}")
        for symbol, qty in held:
            if symbol not in done:
                read_so_far = self._tapes.get(symbol)
                level = None if read_so_far is None else read_so_far.level
                self._tapes[symbol] = TapeRead(symbol, qty, level, "partial", f"read {detail}")
            tape = self._tapes.get(symbol)
            if tape is not None and tape.status != "ok":
                reason = "outside_rth_entry_unknown"
                if tape.status != "entry_unknown":
                    reason = "outside_rth_tape_unreadable"
                self.page(reason, tape.symbol, tape.detail)

    def _read_tape(
        self,
        history: HistoricalTape,
        symbol: str,
        qty: Decimal,
        now_ns: int,
        spend: Callable[[], bool],
    ) -> TapeRead | None:
        """The adoption instant from the broker record, then each outside-hours span since.

        The adoption instant is B's first house trail submitted at or after the fill time
        of the latest quantity-increasing owner fill (ADR-0005: a restart is not an
        adoption; an owner add is). Without such a trail B adopts now: nothing is read and
        no status is claimed (None). alpaca-py 0.44.0 ``Order.filled_at`` is
        ``Optional[datetime]``, "Timestamp when the order was filled" (alpaca/trading/
        models.py:178,217), with no partial-fill meaning, so a buy that is not fully filled
        has no fill time; if it was last updated after the latest full fill, the entry is
        unknown (G2).
        """

        over = TapeRead(symbol, qty, None, "partial", "start read over its deadline or budget")
        if not spend():
            return over
        try:
            orders = self._start_client.get_orders(
                GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    symbols=[symbol],
                    direction=Sort.DESC,
                    limit=OPEN_ORDER_LIMIT,
                )
            )
        except Exception as exc:
            return TapeRead(symbol, qty, None, "partial", f"orders: {type(exc).__name__}")
        fills: list[int] = [0]
        unknown: list[int] = [0]
        claims: list[int] = []
        for order in orders:
            if _event_name(getattr(order, "side", None)) == "buy":
                try:
                    filled = _decimal(getattr(order, "filled_qty", None), "order.filled_qty")
                except ValueError:
                    filled = Decimal(1)
                at = getattr(order, "filled_at", None)
                if filled <= 0:
                    continue
                if _event_name(order.status) == "filled" and isinstance(at, datetime):
                    fills.append(timestamp_ns(at))
                else:
                    updated = getattr(order, "updated_at", None)
                    stamp = updated if isinstance(updated, datetime) else None
                    unknown.append(now_ns if stamp is None else timestamp_ns(stamp))
            elif (
                self._is_ours(order)
                and _kind(order) == "trailing_stop"  # N2: only a house trail is the claim
                and isinstance(order.submitted_at, datetime)
            ):
                claims.append(timestamp_ns(order.submitted_at))
        fill_ns = max(fills)
        if fill_ns == 0 or max(unknown) > fill_ns:
            detail = "no fill time for the latest owner buy"
            return TapeRead(symbol, qty, None, "entry_unknown", detail)
        claimed = [at for at in claims if fill_ns <= at < now_ns]
        if not claimed:
            return None
        # R1-R3: each page is one paced request inside the same deadline and budget; a window
        # is complete at its page without a token. The level is the maximum over the pages
        # read (the rc8 trail only ratchets up), so a partial read keeps a lower bound.
        # No rows and no token is a quiet window; rows of which none is inside the window,
        # or a token the window already returned, is malformed: partial, no further page.
        level: Decimal | None = None
        source: TriggerQuote | None = None
        window: tuple[int, int, str] | None = None
        pages = 0

        def tape_read(state: str, detail: str = "") -> TapeRead:
            if source is None or window is None:
                return TapeRead(symbol, qty, level, state, detail, pages=pages)
            return TapeRead(
                symbol,
                qty,
                level,
                state,
                detail,
                source_bid=source.bid,
                source_ask=source.ask,
                source_ns=source.ts_ns,
                window_start_ns=window[0],
                window_end_ns=window[1],
                feed=window[2],
                pages=pages,
            )

        for start_ns, end_ns, feed in tape_windows(min(claimed), now_ns):
            token: str | None = None
            seen: set[str] = set()
            while True:
                why = f"{pages} page(s) read, the level a lower bound; at {feed}"
                if not spend():
                    return tape_read("partial", f"{why}: {over.detail}")
                try:
                    rows, token = history.quote_page(symbol, start_ns, end_ns, feed, token)
                except Exception as exc:
                    return tape_read("partial", f"{why}; {type(exc).__name__}")
                pages += 1
                inside = [row for row in rows if start_ns <= row.ts_ns <= end_ns]
                found = rebuild_trigger_with_source(
                    inside,
                    trail_fraction=self._parameters.native_trail_fraction,
                    params=self._exit_params,
                )
                if found is not None and (level is None or found.level > level):
                    level = found.level
                    source = found
                    window = (start_ns, end_ns, feed)
                # Kept at once, so the interval timer's expiry keeps the pages already read.
                self._tapes[symbol] = tape_read("partial", why)
                why = f"{pages} page(s) read, the level a lower bound; at {feed}"
                if rows and not inside:
                    return tape_read("partial", f"{why}: no row inside its window")
                if token is None:
                    break
                if token in seen:
                    return tape_read("partial", f"{why}: a repeated continuation token")
                seen.add(token)
        return tape_read("ok")

    def _adopt_outside(self, symbol: str, order: Order) -> None:
        """Keep the highest of the native stop, the broker hwm's level, the trigger and tape."""

        state = self._outside_state(symbol)
        if state.released or state.pending:
            return
        # G3: the maximum over every valid source, never below any of them. Each candidate
        # keeps its provenance, so the record can say which source the level came from.
        submitted = getattr(order, "submitted_at", None)
        submitted_ns = timestamp_ns(submitted) if isinstance(submitted, datetime) else None
        candidates: list[tuple[Decimal, TriggerSource, HighWater, TriggerQuote | None]] = []
        stop_price = getattr(order, "stop_price", None)
        if stop_price is not None:
            stop = _decimal(str(stop_price), "order.stop_price")
            candidates.append(
                (
                    stop,
                    TriggerSource(kind="native_stop", level=stop, stop_price=stop),
                    HighWater(
                        value=stop,
                        kind="native_stop",
                        at_ns=submitted_ns,
                        detail="the broker-held stop, the only high the resting trail reports",
                    ),
                    None,
                )
            )
        try:
            hwm = _decimal(str(getattr(order, "hwm", None)), "order.hwm")
            percent = _decimal(str(getattr(order, "trail_percent", None)), "trail_percent")
            implied = hwm * (1 - percent / 100)
        except ValueError:
            hwm, percent, implied = Decimal(0), Decimal(0), Decimal(0)
        if implied > 0 and percent > 0:
            candidates.append(
                (
                    implied,
                    TriggerSource(kind="broker_hwm", level=implied, hwm=hwm, trail_percent=percent),
                    HighWater(
                        value=hwm,
                        kind="broker_hwm",
                        at_ns=submitted_ns,
                        detail="the broker's own high-water mark behind the resting stop",
                    ),
                    None,
                )
            )
        tape = self._tapes.get(symbol)
        # R3: a partial read's level is a lower bound on the complete read's, so it counts too.
        if tape is not None and tape.status in ("ok", "partial") and tape.level is not None:
            span = None
            if tape.window_start_ns is not None and tape.window_end_ns is not None:
                span = TapeSpan(
                    start_ns=tape.window_start_ns,
                    end_ns=tape.window_end_ns,
                    feed=tape.feed,
                    pages=tape.pages,
                    status=tape.status,
                )
            quote = None
            if tape.source_bid is not None and tape.source_ns is not None:
                quote = TriggerQuote(
                    bid=tape.source_bid,
                    ask=tape.source_ask if tape.source_ask is not None else tape.source_bid,
                    ts_ns=tape.source_ns,
                    level=tape.level,
                )
            candidates.append(
                (
                    tape.level,
                    TriggerSource(
                        kind="tape_high",
                        level=tape.level,
                        at_ns=tape.source_ns,
                        bid=tape.source_bid,
                        ask=tape.source_ask,
                        tape=span,
                    ),
                    HighWater(
                        value=tape.source_bid if tape.source_bid is not None else tape.level,
                        kind="bid" if tape.source_bid is not None else "tape_level",
                        at_ns=tape.source_ns,
                    ),
                    quote,
                )
            )
        if stop_price is None:
            self.page(
                "outside_rth_trigger_missing",
                symbol,
                "the resting native trail has no stop price to emulate",
            )
        if not candidates:
            return
        seed, source, high, quote = max(candidates, key=lambda item: item[0])
        if state.trigger is not None and state.trigger >= seed:
            return
        now_ns = self._now_ns()
        state.adopt(seed, now_ns, source=quote)
        self._trigger_source[symbol] = source
        self._trigger_high[symbol] = high
        noise, noise_detail = self._noise(state)
        self._record(
            ExitRecord(
                action="adopt",
                at_ns=now_ns,
                symbol=symbol,
                trigger=seed,
                source=source,
                high=high,
                observations=self._observations(state),
                book=self._trigger_book(symbol),
                noise=noise,
                noise_detail=noise_detail,
                outcome="armed",
            )
        )

    def _begin_outside_release(self, symbol: str, at_ns: int) -> None:
        """Cancel the trail and release one LIMIT after its terminal cancellation."""

        state = self._outside.get(symbol)
        if state is None or state.released or state.pending or symbol in self._released_symbols:
            return
        trails = [order for order in self._house.get(symbol, []) if not _is_limit(order)]
        if len(trails) != 1:
            self.page(
                "outside_rth_no_trail_at_breach",
                symbol,
                "the breach has no single resting native trail to cancel",
            )
            return
        now_ns = self._now_ns()
        fresh = self._priceable(session_at(now_ns), symbol, now_ns)
        try:
            price = state.release_price(now_ns, fresh=fresh)
        except OutsideRthError as exc:
            self.page("outside_rth_release_unpriced", symbol, str(exc))
            return
        available = self._last_position.get(symbol, Decimal(0)) - self._last_reserved.get(
            symbol, Decimal(0)
        )
        whole = int(available.to_integral_value(rounding=ROUND_FLOOR)) if available > 0 else 0
        if whole <= 0:
            self.page("outside_rth_release_unquantified", symbol, "no unreserved whole share")
            return
        source, high = self._provenance(symbol, state)
        noise, noise_detail = self._noise(state)
        self._pending_decision[symbol] = _PendingDecision(
            at_ns=at_ns,  # the print that completed the breach, not the submit time
            trigger=state.trigger,
            source=source,
            high=high,
            observations=self._observations(state),
            book=self._trigger_book(symbol, Decimal(whole)),
            quantity=Decimal(whole),
            noise=noise,
            noise_detail=noise_detail,
            price=price,
        )
        if not self._cancel(trails[0], "outside_rth_breach"):
            self._record_release(
                symbol,
                self._pending_decision.pop(symbol, None),
                order=None,
                outcome="abandoned",
                note="the native trail could not be cancelled",
            )
            return
        self._pending_release[symbol] = _PendingRelease(trail_order_id=str(trails[0].id))
        state.begin_release(now_ns)
        self._announce_symbol(symbol, now_ns)

    def _complete_outside_release(self, symbol: str) -> None:
        """Reprice and re-quantify from a fresh snapshot, then submit the one LIMIT."""

        state = self._outside.get(symbol)
        if state is None:
            self._released_symbols.discard(symbol)
            self._record_release(
                symbol,
                self._pending_decision.pop(symbol, None),
                order=None,
                outcome="abandoned",
                note="no emulated trail was left to release",
            )
            return
        now_ns = self._now_ns()
        if session_at(now_ns) is Session.CLOSED:
            state.rearm()
            self._released_symbols.discard(symbol)
            self.page("market_closed", symbol, "B writes only in an open session")
            self._record_release(
                symbol,
                self._pending_decision.pop(symbol, None),
                order=None,
                outcome="abandoned",
                note="the session closed before the release",
            )
            return
        positions = self.reconcile()
        if symbol not in positions:
            state.rearm()
            self._released_symbols.discard(symbol)
            self._record_release(
                symbol,
                self._pending_decision.pop(symbol, None),
                order=None,
                outcome="abandoned",
                note="the position was gone before the release",
            )
            return
        now_ns = self._now_ns()
        fresh = self._priceable(session_at(now_ns), symbol, now_ns)
        try:
            price = state.release_price(now_ns, fresh=fresh)
        except OutsideRthError as exc:
            state.rearm()
            self._released_symbols.discard(symbol)
            self.page("outside_rth_release_unpriced", symbol, str(exc))
            self._record_release(
                symbol,
                self._pending_decision.pop(symbol, None),
                order=None,
                outcome="abandoned",
                note=str(exc),
            )
            return
        available = self._last_position.get(symbol, Decimal(0)) - self._last_reserved.get(
            symbol, Decimal(0)
        )
        whole = int(available.to_integral_value(rounding=ROUND_FLOOR)) if available > 0 else 0
        if whole <= 0:
            state.rearm()
            self._released_symbols.discard(symbol)
            self.page("outside_rth_release_unquantified", symbol, "no unreserved whole share")
            self._record_release(
                symbol,
                self._pending_decision.pop(symbol, None),
                order=None,
                outcome="abandoned",
                note="no unreserved whole share",
            )
            return
        self._submit_outside_limit(symbol, whole, price)

    def _submit_outside_limit(self, symbol: str, whole: int, price: Decimal) -> None:
        self._sequence += 1
        client_order_id = (
            f"{self._account.house_prefix}outside-{_safe_symbol(symbol)}-"
            f"{self._now_ns()}-{self._sequence}"
        )[:MAX_CLIENT_ORDER_ID]
        request = LimitOrderRequest(
            symbol=symbol,
            qty=whole,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            limit_price=str(price),
            extended_hours=True,
            client_order_id=client_order_id,
        )
        try:
            submitted = self._client.submit_order(request)
        except Exception as exc:
            state = self._outside.get(symbol)
            if state is not None:
                state.rearm()
            self._released_symbols.discard(symbol)
            self.page("outside_limit_submit_failed", symbol, type(exc).__name__)
            self._record_release(
                symbol,
                self._pending_decision.pop(symbol, None),
                order=self._order_line(client_order_id, whole, price, submitted=False),
                outcome="abandoned",
                note=f"the release LIMIT was not submitted: {type(exc).__name__}",
            )
            return
        order_id = str(submitted.id) if isinstance(submitted, Order) else None
        if isinstance(submitted, Order):
            self._remember(submitted)
        state = self._outside.get(symbol)
        if state is not None:
            state.mark_released(self._now_ns())
        self._announce_symbol(symbol, self._now_ns())
        context = self._pending_decision.pop(symbol, None)
        order_line = self._order_line(
            client_order_id, whole, price, submitted=True, order_id=order_id
        )
        if context is not None and context.price != price:
            order_line["repriced_from"] = context.price
        self._record_release(symbol, context, order=order_line, outcome="released")

    def handle(self, update: TradeUpdate) -> None:
        symbol = update.order.symbol
        if not symbol:
            return
        try:
            self._handle_update(update, symbol)
        except PacerRefused as exc:
            # The pacer retains the deferred call; the next event reconciles again.
            self.page("pacer_refused", symbol, str(exc))

    def _handle_update(self, update: TradeUpdate, symbol: str) -> None:
        event = _event_name(update.event)
        if note := getattr(self._client, "entry_update", None):
            note(update.order, event)
        if self._is_ours(update.order):
            self._handle_house(update, symbol, event)
            return
        # Any owner order event may change a reservation or the position: reconcile and
        # re-arm. Non-fill events never freeze the symbol.
        self.reconcile()

    def reconcile_when_due(self) -> bool:
        """Reconcile on B's cadence, and once when the session changes.

        A trade update reconciles only because the owner or the broker acted; the cadence is
        what notices a trail the broker lost while nothing happened. A session change is its
        own trigger so the native trail's stop is adopted at the boundary, before the new
        session's first quote can seed an emulated trigger below it. Returns whether a
        reconcile ran.
        """

        now = self._now_ns()
        session = session_at(now)
        changed = session is not self._last_session
        interval_ns = int(self._parameters.reconcile_interval_s * NS_PER_SECOND)
        if not changed and now - self._last_reconcile_ns < interval_ns:
            return False
        self.reconcile()
        return True

    def reconcile(self, *, announce: bool = True) -> dict[str, Position]:
        """Match the broker's open orders and positions, then protect the whole net shares.

        Every unreserved whole long share is covered by exactly one house trail of the full
        net quantity; a change in that quantity re-arms it during regular market hours.
        ``announce=False`` lets a startup reconcile precede the first market-data poll, so the
        one startup line names the live feed rather than an empty one.
        """

        now_ns = self._now_ns()
        self._last_reconcile_ns = now_ns
        self._last_session = session_at(now_ns)
        try:
            positions, house, owner_sells, unrecognized = self._snapshot()
        except ProtectorRefusal as exc:
            self.page("open_orders_truncated", None, str(exc))
            return {}
        self._house = {symbol: list(ours) for symbol, ours in house.items()}
        self._last_position = {
            symbol: _decimal(position.qty, "position.qty") for symbol, position in positions.items()
        }
        self._last_reserved = {symbol: owner_sells.get(symbol, Decimal(0)) for symbol in positions}
        # G8 (F4): a start read for another quantity is dropped here, in every session.
        self._tapes = {
            s: tape for s, tape in self._tapes.items() if tape.qty == self._last_position.get(s)
        }
        if self._start_unread:  # nothing was read off the main thread: say partial (LEAD-0079)
            self._start_unread = False
            for s, qty in self._last_position.items():
                self._tapes.setdefault(s, TapeRead(s, qty, None, "partial", "off the main thread"))
        for symbol, position in positions.items():
            self._reconcile_symbol(
                symbol,
                position,
                house.pop(symbol, []),
                owner_sells.get(symbol, Decimal(0)),
                symbol in unrecognized,
            )
        for symbol, ours in house.items():
            if symbol not in positions:
                for order in ours:
                    self._cancel(order, "flat_with_house_exit")
        held = set(positions)
        self._outside = {s: state for s, state in self._outside.items() if s in held}
        self._pending_release = {
            s: pending for s, pending in self._pending_release.items() if s in held
        }
        self._pending_rearm = {
            s: order_id for s, order_id in self._pending_rearm.items() if s in held
        }
        self._released_symbols &= held
        self._trigger_source = {
            s: source for s, source in self._trigger_source.items() if s in held
        }
        self._trigger_high = {s: high for s, high in self._trigger_high.items() if s in held}
        self._width = {s: width for s, width in self._width.items() if s in held}
        self._width_paged &= held
        self._native_paged &= held
        self._width_seen &= held
        if announce:
            self._announce_states(now_ns)
        return positions

    def _snapshot(
        self,
    ) -> tuple[
        dict[str, Position],
        dict[str, list[Order]],
        dict[str, Decimal],
        set[str],
    ]:
        """Read positions and open orders, without changing anything at the broker."""

        positions = {
            position.symbol: position
            for position in self._client.get_all_positions()
            if position.symbol
        }
        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=OPEN_ORDER_LIMIT)
        )
        if len(orders) >= OPEN_ORDER_LIMIT:
            raise ProtectorRefusal("open order snapshot hit the page limit and may be truncated")
        house: dict[str, list[Order]] = {}
        owner_sells: dict[str, Decimal] = {}
        unrecognized: set[str] = set()
        for order in orders:
            if not order.symbol:
                continue
            if self._is_ours(order):
                house.setdefault(order.symbol, []).append(order)
                continue
            side = _event_name(order.side).lower()
            if side == "sell":
                leaves = owner_sells.get(order.symbol, Decimal(0)) + _leaves(order)
                owner_sells[order.symbol] = leaves
            elif side != "buy":
                unrecognized.add(order.symbol)
        return positions, house, owner_sells, unrecognized

    def _cancel_all(self, ours: list[Order], reason: str) -> None:
        for order in ours:
            self._cancel(order, reason)

    def _cancel_excess(self, ours: list[Order], protective: int, reason: str) -> list[Order]:
        """Cancel house exits beyond one trail no larger than the protective quantity."""

        if len(ours) > 1:
            eligible = [order for order in ours if _leaves(order) <= protective]
            keeper = max(eligible, key=_leaves) if eligible else None
            for order in ours:
                if order is not keeper:
                    self._cancel(order, reason)
            return [keeper] if keeper is not None else []
        if ours and _leaves(ours[0]) > protective:
            self._cancel(ours[0], reason)
            return []
        return list(ours)

    def _handle_house(self, update: TradeUpdate, symbol: str, event: str) -> None:
        if event in FILL_EVENTS:
            self.reconcile()
            return
        order_id = str(update.order.id)
        if event == "replaced":
            self.page("house_exit_lost", symbol, "a house exit was replaced elsewhere")
            self.reconcile()
            return
        if event in TERMINAL_EVENTS:
            known = order_id in self._cancelling
            self._cancelling.discard(order_id)
            pending = self._pending_release.get(symbol)
            if pending is not None and pending.trail_order_id == order_id:
                self._pending_release.pop(symbol, None)
                if event == "canceled":
                    self._released_symbols.add(symbol)
                    self._complete_outside_release(symbol)
                else:
                    state = self._outside.get(symbol)
                    if state is not None:
                        state.rearm()
                    self.page(
                        "outside_rth_release_failed",
                        symbol,
                        f"the resting trail ended {event} before the release",
                    )
                    self._record_release(
                        symbol,
                        self._pending_decision.pop(symbol, None),
                        order=None,
                        outcome="abandoned",
                        note=f"the resting trail ended {event} before the release",
                    )
            elif self._pending_rearm.get(symbol) == order_id:
                self._pending_rearm.pop(symbol, None)
            elif not known:
                self.page("house_exit_lost", symbol, f"a house exit ended {event}")
            self.reconcile()

    def _reconcile_symbol(
        self,
        symbol: str,
        position: Position,
        ours: list[Order],
        owner_sell_reserved: Decimal,
        unrecognized_open_order: bool,
    ) -> None:
        quantity = _decimal(position.qty, "position.qty")
        if quantity < 0:
            self.page("short_position_unprotected", symbol)
            self._cancel_all(ours, "short_position")
            return
        if quantity == 0:
            self._cancel_all(ours, "flat_with_house_exit")
            return
        if unrecognized_open_order:
            self.page(
                "unrecognized_open_order",
                symbol,
                "an open order has no buy/sell side; only owner sells are netted out",
            )
        available = quantity - owner_sell_reserved
        protective = 0
        if available > 0:
            protective = int(available.to_integral_value(rounding=ROUND_FLOOR))
        if quantity != quantity.to_integral_value() or available != protective:
            self.page("fractional_custody_unverified", symbol)
        if protective == 0:
            self._cancel_all(ours, "owner_reserved_unprotected")
            self.page("owner_reserved_unprotected", symbol)
            return
        if symbol in self._unresolved:
            client_order_id = self._unresolved[symbol]
            if any(order.client_order_id == client_order_id for order in ours):
                self._unresolved.pop(symbol)
            else:
                status, detail = self._resolve_unresolved(client_order_id)
                if status == "terminal":
                    self._unresolved.pop(symbol)
                    self.page("submit_unresolved_terminal", symbol, detail)
                    return
                if status != "absent":
                    self.page("submit_unresolved", symbol, detail)
                    return
        session = session_at(self._now_ns())
        in_rth = session is Session.RTH
        # An oversized GTC trail is cancelled before the RTH guard, so it can never
        # ride into the next open; only the replacement submit waits for RTH.
        rest = self._cancel_excess(ours, protective, "rearm_full_protective_quantity")
        if symbol in self._pending_release:
            # The breach already decided the release; wait for the trail's terminal
            # cancellation instead of arming a second exit beside it.
            return
        if symbol in self._pending_rearm:
            # A resize waits for the old exit's terminal event before it replaces it.
            return
        if session is Session.CLOSED:
            self.page("market_closed", symbol, "B writes only in an open session")
            return
        if len(rest) == 1 and _leaves(rest[0]) == protective:
            if _is_limit(rest[0]):
                self._released_symbols.discard(symbol)
            elif not in_rth:
                self._adopt_outside(symbol, rest[0])
            return
        if not in_rth:
            for order in rest:
                if self._cancel(order, "outside_rth_resize"):
                    self._pending_rearm[symbol] = str(order.id)
            if ours:
                return
            if symbol in self._released_symbols:
                return
            self._submit_trail(symbol, protective)
            return
        for order in rest:
            if self._cancel(order, "rearm_full_protective_quantity"):
                self._pending_rearm[symbol] = str(order.id)
        # The terminal canceled event of the house's own trail drives reconcile(),
        # which submits the single replacement; never submit over a live exit.
        if ours:
            return
        if symbol in self._released_symbols:
            # The release handover is about to submit its LIMIT from a fresh
            # snapshot; never arm a second full-position exit beside it.
            return
        self._submit_trail(symbol, protective)

    def _resolve_unresolved(self, client_order_id: str) -> tuple[str, str]:
        """Resolve a submit that timed out before allocating another exit identity."""

        try:
            order = self._client.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if exc.status_code == 404:
                return "absent", "the broker has no order with the retained client order id"
            return "unknown", f"APIError {exc.status_code}"
        except Exception as exc:
            return "unknown", type(exc).__name__
        status = _event_name(order.status)
        if status == "filled" or status in TERMINAL_EVENTS:
            return "terminal", f"the broker order with the retained identity is {status}"
        return "open", f"the broker still lists the retained order as {status}"

    def _width_in_force(self, symbol: str) -> tuple[Decimal, str]:
        """The percent a new trail arms at, rounded up (b_width.trail_percent), and its source."""

        if symbol in self._width:
            return trail_percent(self._width[symbol]), "computed"
        return trail_percent(self._parameters.native_trail_fraction), "native"

    def _arm_percent(self, symbol: str) -> tuple[float, bool]:
        """A new trail's percent, and whether native opens a page episode; never moves a
        working trail."""

        if symbol in self._width:
            return float(trail_percent(self._width[symbol])), False  # the boundary float
        fallback = self._parameters.width_on and symbol not in self._native_paged
        if fallback:
            self._native_paged.add(symbol)
        return float(self._parameters.native_trail_fraction * 100), fallback

    def _submit_trail(self, symbol: str, whole: int) -> None:
        client_order_id = self._unresolved.get(symbol)
        if client_order_id is None:
            self._sequence += 1
            client_order_id = (
                f"{self._account.house_prefix}trail-{_safe_symbol(symbol)}-"
                f"{self._now_ns()}-{self._sequence}"
            )[:MAX_CLIENT_ORDER_ID]
        percent, fallback = self._arm_percent(symbol)
        request = TrailingStopOrderRequest(
            symbol=symbol,
            qty=whole,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_percent=percent,
            client_order_id=client_order_id,
        )
        try:
            submitted = self._client.submit_order(request)
        except PacerRefused:
            # The pacer retains the deferred submit and sends it on a later drain.
            raise
        except Exception as exc:
            self._unresolved[symbol] = client_order_id
            self.page("trail_submit_failed", symbol, type(exc).__name__)
            return
        finally:  # after the submit; a failing page sink is counted, never raised (P1 rule)
            if fallback:
                try:
                    self.page("dynamic_width_native_fallback", symbol, "no width stored; native")
                except Exception:
                    self.page_sink_failures += 1
        self._unresolved.pop(symbol, None)
        if isinstance(submitted, Order):
            self._remember(submitted)

    def _remember(self, order: Order) -> None:
        """Keep an order the broker just accepted so the next breach can find it."""

        if not order.symbol:
            return
        ours = self._house.setdefault(order.symbol, [])
        if all(str(existing.id) != str(order.id) for existing in ours):
            ours.append(order)

    def _cancel(self, order: Order, reason: str) -> bool:
        if not self._is_ours(order):
            raise ProtectorRefusal("refusing to cancel an order the house does not own")
        order_id = str(order.id)
        self._cancelling.add(order_id)
        try:
            self._client.cancel_order_by_id(order.id)
        except Exception as exc:
            self._cancelling.discard(order_id)
            self.page("cancel_failed", order.symbol, f"{reason}: {type(exc).__name__}")
            return False
        if _is_limit(order) and order.symbol is not None:
            # A cancelled release no longer blocks re-arming protection, and the
            # emulator must accept quotes again for whatever exit replaces it.
            self._released_symbols.discard(order.symbol)
            state = self._outside.get(order.symbol)
            if state is not None:
                state.rearm()
        if order.symbol is not None and order.symbol not in self._pending_decision:
            # The release handover writes its own line once it resolves.
            self._record_cancel(order, reason)
        return True

    def _is_ours(self, order: Order) -> bool:
        return bool(order.client_order_id) and order.client_order_id.startswith(
            self._account.house_prefix
        )


class ProtectorStream(TradingStream):
    """The SDK's trade-update stream with reconcile hooks on (re)connect and on a cadence."""

    # The registry interval is the policy; a one-second tick only bounds its jitter.
    SCHEDULE_TICK_S = 1.0

    def __init__(
        self,
        client: TradingClientLike,
        account: PaperAccount,
        parameters: BParameters,
        *,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        url_override: str | None = None,
        now_ns: Callable[[], int] = time.time_ns,
        on_page: Callable[[Page], None] | None = None,
        feed: str = "none",
        on_state: Callable[[str], None] | None = None,
        data_client: LatestDataClient | None = None,
        history: HistoricalTape | None = None,
        start_client: TradingClientLike | None = None,
        decisions: DecisionLog | None = None,
        bars: HistoricalBars | None = None,
        width_quotes: LatestQuotes | None = None,
    ) -> None:
        if url_override is None:
            super().__init__(api_key=api_key, secret_key=secret_key, paper=paper)
        else:
            super().__init__(
                api_key=api_key,
                secret_key=secret_key,
                paper=paper,
                url_override=url_override,
            )
        self._parameters = parameters
        self._now_ns = now_ns
        self._market_data = BMarketDataFeed(data_client) if data_client is not None else None
        self._last_feed_poll_ns = 0
        self._feed_failure: str | None = None
        self._feed_label = "none"
        self._poll_task: asyncio.Task[FeedBatch] | None = None
        self._width_task: asyncio.Task[_WidthReads] | None = None
        self.protector = Protector(
            client,
            account,
            parameters,
            now_ns=now_ns,
            on_page=on_page,
            feed=feed,
            on_state=on_state,
            history=history,
            start_client=start_client,
            decisions=decisions,
            bars=bars,
            quotes=width_quotes,
        )
        self.subscribe_trade_updates(self._on_trade_update)

    def _page_feed_failure(self, detail: str) -> None:
        if self._feed_failure == detail:
            return
        self._feed_failure = detail
        self.protector.page("feed_failed", None, detail)

    async def _await_startup_poll(self) -> None:
        """Wait only the silence bound for the first poll; a stalled poll must not hide it."""

        task = self._poll_task
        if task is None:
            return
        bound_s = float(self._parameters.feed_silence_s)
        done, _ = await asyncio.wait({task}, timeout=bound_s)
        if not done:
            self._page_feed_failure(f"{self._feed_label}: TimeoutError")
            return
        self._drain_poll()

    def _drain_poll(self) -> None:
        """Apply a completed poll and record its success; a stalled poll keeps the old receipt."""

        task = self._poll_task
        if task is None or not task.done():
            return
        self._poll_task = None
        try:
            batch = task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._page_feed_failure(f"{self._feed_label}: {type(exc).__name__}")
            return
        self._feed_failure = None
        self.protector.note_poll(self._now_ns())
        for quote in batch.quotes:
            self.protector.on_quote(
                quote.symbol,
                bid=quote.bid,
                ask=quote.ask,
                last=quote.last,
                ts_ns=quote.ts_ns,
            )
        for trade in batch.trades:
            self.protector.on_trade(trade.symbol, trade.price, trade.ts_ns)

    def _poll_feed(self, *, force: bool = False) -> None:
        """Start one background REST poll; the watch keeps running while it is in flight."""

        if self._market_data is None:
            return
        now_ns = self._now_ns()
        feed = feed_for(session_at(now_ns))
        self.protector.set_feed(feed or "none")
        if feed is None:
            return
        symbols = self.protector.held_symbols()
        if not symbols:
            return
        if self._poll_task is not None:
            return
        interval_ns = int(self._parameters.feed_poll_interval_s * 1_000_000_000)
        if not force and now_ns - self._last_feed_poll_ns < interval_ns:
            return
        self._last_feed_poll_ns = now_ns
        self._feed_label = feed
        self._poll_task = asyncio.create_task(
            asyncio.to_thread(self._market_data.poll, symbols, feed)
        )

    def _refresh_widths(self) -> None:
        """The width's two reads run on a worker thread, as the poll does, never beside one (P1)."""

        if self._poll_task is not None or self._width_task is not None:
            return
        due = self.protector.width_reads_due()
        if due is not None:
            read = asyncio.to_thread(self.protector.read_widths, *due)
            self._width_task = asyncio.create_task(read)

    def _drain_widths(self) -> None:
        """Apply finished width reads on the loop's thread, where every order write runs."""

        task = self._width_task
        if task is not None and task.done():
            self._width_task = None
            if task.cancelled() or task.exception() is not None:  # the last width stays (1021)
                for symbol in self.protector.held_symbols():
                    detail = "width read did not finish; width kept"
                    self.protector._width_unavailable(
                        symbol, "dynamic_width_bars_unavailable", detail
                    )
                return
            self.protector.apply_widths(task.result())

    async def _on_trade_update(self, update: TradeUpdate) -> None:
        try:
            self.protector.handle(update)
        except Exception as exc:
            self.protector.page("trade_update_failed", update.order.symbol, type(exc).__name__)

    async def _start_ws(self) -> None:
        await super()._start_ws()
        try:
            self.protector.reconcile(announce=False)
        except Exception as exc:
            self.protector.page("reconcile_failed", None, type(exc).__name__)
        try:
            self._poll_feed(force=True)
            await self._await_startup_poll()
        except Exception as exc:
            self.protector.page("feed_failed", None, type(exc).__name__)
        try:
            self.protector.watch()
        except Exception as exc:
            self.protector.page("feed_watch_failed", None, type(exc).__name__)

    async def _scheduled_reconcile(self) -> None:
        while True:
            await asyncio.sleep(self.SCHEDULE_TICK_S)
            try:
                self._drain_poll()
                self._drain_widths()
                self._refresh_widths()
                self._poll_feed()
            except Exception as exc:
                self.protector.page("feed_failed", None, type(exc).__name__)
            try:
                self.protector.watch()
            except Exception as exc:
                self.protector.page("feed_watch_failed", None, type(exc).__name__)
            try:
                self.protector.reconcile_when_due()
            except Exception as exc:
                self.protector.page("reconcile_failed", None, type(exc).__name__)

    async def _run_forever(self) -> None:
        schedule = asyncio.create_task(self._scheduled_reconcile())
        try:
            await super()._run_forever()
        finally:
            schedule.cancel()
            try:
                await schedule
            except asyncio.CancelledError:
                pass

    def run(self) -> None:
        self.protector.bind()
        try:
            self.protector.rebuild_tapes()
        except Exception as exc:
            self.protector.page("outside_rth_tape_unreadable", None, type(exc).__name__)
        try:
            super().run()
        finally:
            self.protector.close()

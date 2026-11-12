"""Paper B's lifecycle as one NautilusTrader Strategy (LEAD-2201 S1, component map rows 1-2).

S1-1, ranking in: ``on_start`` subscribes to the ranking data type and ``on_data`` feeds the
reviewed ``RankingConsumer``, the code ``RankingDataActor`` runs (consumer.py:221-232), moved
into the Strategy because a Strategy is an Actor (rc8 trading stub ``subscribe_data``
:690-694, ``on_data`` :632). Each candidate decision reads the consumer's current
``entry_rank`` and the default-off rank gate; a stale, missing or refused generation gives no
candidate and the consumer's reason (ADR-0003, ADR-0006 R5). The heartbeat reaches
``consumer.accept_heartbeat`` from its host; this Strategy never manufactures one.

S1-2, entry (ADR-0004 N1/N4/N5): C's reviewed first-leg wiring (c_session.py: EntryState,
advance_surge, first_leg_ready) and the reviewed native entry episode (entry/replay.py: LIMIT
at ``limit_price(ask)``, reprice inside ``entry_chase_bps``, TTL cancel by its own id). The
order is built in the completing trade's handler with ``order_factory.limit`` (common stub
:1388-1406), sized by ``size_entry``/``FixedRiskSizer`` as a ``Quantity``, and sent with
``submit_order(params={"extended_hours": ...})`` (trading stub :538-543), native.py's
convention. Without B's entry rows the Strategy refuses entries and names the missing rows;
no row has a default here (Rule 2). With the rank gate off there are no house entries. One
entry per instrument: re-entry belongs to a later part. Money is Price, Quantity or Decimal.

S1-3, first protection (LEAD-2201 A4, A8, B2-B5, F): from the first fill, venue-held
``trailing_stop_market`` SELLs (GTC, reduce-only, no emulation, BASIS_POINTS) cover the
unreserved whole long quantity, per ``reconcile``'s quantity rule; they rest across sessions and
each session change writes one record. Every cancel or modify goes through the house guard.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, cast

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core import unix_nanos_to_dt
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import (
    Bar,
    BarType,
    ClientId,
    ClientOrderId,
    ContingencyType,
    CustomData,
    Equity,
    InstrumentId,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    QuoteTick,
    StrategyId,
    TimeInForce,
    TradeTick,
    TrailingOffsetType,
)
from nautilus_trader.trading import Strategy

from sextant.entry.indicators import DecimalATR
from sextant.entry.orders import limit_price, within_chase
from sextant.entry.parameters import FIRST_LEG, SIZING, EntryParameters
from sextant.entry.signals import EntryState
from sextant.entry.sizing import SegmentBasis, Sizing, size_entry
from sextant.entry.surge import SurgeState, advance_surge
from sextant.exits.engine import CANCEL_RETRIES, CANCEL_RETRY_NS
from sextant.exits.policy import ExitParams, buffer
from sextant.paper.b_width import MAX_TRAIL_FRACTION, trail_fraction, trail_percent, view_from_quote
from sextant.paper.sessions import Session, session_at, session_start_ns
from sextant.ranking.baseline import BaselineTable
from sextant.ranking.consumer import RankingConsumer
from sextant.ranking.heartbeat import heartbeat_signal
from sextant.ranking.publisher import ranking_data_type
from sextant.ranking.rank_gate import RankGateDecision, RankGateStatus, decide_rank_gate
from sextant.ranking.registry import RegistryRow

Journal = Callable[[dict[str, Any]], None]
ENTRY_ROWS = FIRST_LEG | SIZING
WIDTH_ROWS = frozenset(
    {"alpaca_min_trail_fraction", "trail_atr_period", "trail_atr_bar_minutes", "trail_atr_multiple"}
)
NS = 1_000_000_000
ABSENT = object()  # a config without the client row: refused, never read as None


# The engine fields a caller may pass through (trading stub StrategyConfig :1021-1041): the
# event and command logging switches. strategy_id and order_id_tag come from the namespace, and
# every other StrategyConfig field is refused, since B's house guard and its never-used exits
# depend on them; any key that is neither B's own nor one of these is refused by name.
ENGINE_FIELDS = frozenset({"log_events", "log_commands"})


class BLifecycleConfig(StrategyConfig):
    def __new__(cls, *, namespace: str, **kwargs: Any):
        # rc8's PyO3 configs initialize their native fields in __new__ (native.py's shape); the
        # constructor's own keywords arrive here too, so only the engine fields are forwarded.
        engine = {key: kwargs[key] for key in ENGINE_FIELDS if key in kwargs}
        # B claims the external (owner) orders on every instrument it trades or protects
        # (LEAD-2201 A8; trading stub StrategyConfig.external_order_instrument_ids), so owner
        # fills and reservations reach its position; the client filters the unclaimed.
        return cast(Any, super().__new__)(
            cls,
            strategy_id=StrategyId.from_str(f"BLIFECYCLE-{namespace}"),
            order_id_tag=namespace,
            external_order_instrument_ids=list(kwargs.get("instrument_ids", ())),
            **engine,
        )

    def __init__(
        self,
        *,
        namespace: str,
        instrument_ids: Sequence[InstrumentId],
        consumer: RankingConsumer | None,
        rank_gate: RegistryRow,
        journal: Journal | None = None,
        entry_params: EntryParameters | None = None,
        exit_params: ExitParams | None = None,
        commission_rate: Decimal | None = None,
        basis_path: Path | None = None,
        baseline: BaselineTable | None = None,
        native_trail_fraction: Decimal | None = None,
        width_rows: Mapping[str, Decimal] | None = None,
        trail_reduce_only: bool = True,
        client_id: str | None = None,
        **engine: object,
    ) -> None:
        unknown = sorted(set(engine) - ENGINE_FIELDS)
        if unknown:
            raise TypeError(f"{type(self).__name__}() got unexpected keyword arguments {unknown}")
        super().__init__()
        ids = tuple(instrument_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("B needs unique instrument ids")
        if not all(isinstance(iid, InstrumentId) for iid in ids):
            raise ValueError("B instrument ids must be native InstrumentIds")
        if not namespace.isalnum():  # the house guard reads it as one id segment
            raise ValueError("B's namespace must be letters and digits")
        if not isinstance(rank_gate, RegistryRow) or rank_gate.name != "rank_gate_enabled":
            raise ValueError("B needs the rank_gate_enabled registry row")
        # With the gate off no rank is read, so protection needs no consumer (lead 09:1x); the
        # entry then has no session segment and is refused. Only the bool False is off
        # (ranking/registry.py:50-61): 0 == False in Python, so 0 and Decimal(0) are refused.
        if not isinstance(consumer, RankingConsumer) and not (
            consumer is None and rank_gate.value is False
        ):
            raise ValueError("B needs the reviewed RankingConsumer")
        if (
            consumer is not None
            and baseline is not None
            and (
                baseline.identity != consumer.reader.baseline_hash or baseline.feed != consumer.feed
            )
        ):
            raise ValueError("the surge baseline differs from the ranking's baseline")
        self.namespace, self.instrument_ids = namespace, ids
        self.consumer, self.rank_gate, self.journal = consumer, rank_gate, journal
        self.entry_params, self.exit_params = entry_params, exit_params
        self.commission_rate, self.basis_path, self.baseline = commission_rate, basis_path, baseline
        self.native_trail_fraction = native_trail_fraction
        if width_rows is not None:
            if set(width_rows) != WIDTH_ROWS or native_trail_fraction is None:
                raise ValueError(f"the width needs exactly {sorted(WIDTH_ROWS)} and the native row")
            for name in ("trail_atr_period", "trail_atr_bar_minutes"):
                if width_rows[name] % 1 or width_rows[name] < 1:
                    raise ValueError(f"{name} must be a whole number of at least one")
        self.width_rows = width_rows
        # The venue's row for the trail's reduce-only flag, read where the trail is built: True on
        # Alpaca (ADR-0005:34); False on the official Interactive Brokers adapter, which denies a
        # reduce-only order (nautilus_trader rc8 adapters/interactive_brokers/src/execution/
        # core.rs:1032-1043, :2191-2196). Never an `if venue` in the Strategy.
        if not isinstance(trail_reduce_only, bool):
            raise ValueError("trail_reduce_only must be True or False")
        self.trail_reduce_only = trail_reduce_only
        # The venue's row for the execution client named on every command: unset on Alpaca (the
        # venue routes); "IB" on Interactive Brokers, whose client the node routes by the venue
        # it is configured for (nautilus_trader rc8 crates/live/src/node/builder.rs:719-772),
        # as upstream names it (examples/live/interactive_brokers/exec_tester.py:116).
        if client_id is not None and (not isinstance(client_id, str) or not client_id):
            raise ValueError("client_id must be a non-empty string or None")
        self.client_id = client_id


@dataclass(slots=True)
class _Book:
    """Per-instrument state; later parts add protection and campaign state."""

    instrument_id: InstrumentId
    gate: RankGateDecision | None = None
    gate_ns: int | None = None
    instrument: Equity | None = None
    entry: EntryState | None = None
    surge: SurgeState = field(default_factory=SurgeState)
    quote: QuoteTick | None = None
    trades: deque[tuple[int, Decimal]] = field(default_factory=deque)
    entry_id: ClientOrderId | None = None
    first_limit: Decimal | None = None
    expiry_ns: int | None = None
    refused_flag: int | None = None
    session: Session | None = None
    stale_since: int | None = None
    paged: str | None = None
    rearm: bool = False  # a trail the RiskEngine denied: cover again on the next price
    no_trade: bool = False  # denied with no trade cached: cover again on the next trade
    width_bar: BarType | None = None
    oracle: AverageTrueRange | None = None  # the engine's float ATR, compared only
    atr: DecimalATR | None = None  # the value used (ADR-0004's Decimal carry)
    atr_session: int | None = None
    width: Decimal | None = None
    high: Decimal | None = None  # the tape high since the position opened
    last: Decimal | None = None
    offsets: dict[ClientOrderId, Decimal] = field(default_factory=dict)  # bps in force
    session_bars: list[Bar] = field(default_factory=list)
    rearm_from: int | None = None  # a re-arm's cancel was sent then; its new trail is owed
    cancelling: set[ClientOrderId] = field(default_factory=set)  # cancels sent, until the end
    # The cancel's record (exits/engine.py:303-330, ADR-0007:27): when a rejected cancel's retry
    # is due, how many went out for the order, the last send, and the orders past K+1.
    recancel: dict[ClientOrderId, int] = field(default_factory=dict)
    cancel_tries: dict[ClientOrderId, int] = field(default_factory=dict)
    cancel_sent: dict[ClientOrderId, int] = field(default_factory=dict)
    exhausted: set[ClientOrderId] = field(default_factory=set)
    started: set[ClientOrderId] = field(default_factory=set)  # completing trails, paged once
    not_in_cache: set[ClientOrderId] = field(default_factory=set)  # paged once per id, kept


class BLifecycle(Strategy):
    """One writer's Strategy for every instrument B trades or protects (LEAD-2201 A1)."""

    def __init__(self, config: BLifecycleConfig) -> None:
        super().__init__(config)
        self.consumer, self.rank_gate = config.consumer, config.rank_gate
        self._journal = config.journal
        self.journal_errors = 0
        self.books = {iid: _Book(iid) for iid in config.instrument_ids}
        self.params, self.exit = config.entry_params, config.exit_params
        self.commission, self.baseline = config.commission_rate, config.baseline
        self.basis_path = config.basis_path
        self.basis: Decimal | None = None  # N5: frozen at node start, or no entry
        self.tag, self._config = config.namespace, config
        rows = self.params
        missing = sorted(n for n in ENTRY_ROWS if rows is None or not rows.has(n))
        named = (
            ("ranking_consumer", self.consumer),
            ("exit_params", self.exit),
            ("commission_rate", self.commission),
            ("basis_path", self.basis_path),
            ("baseline", self.baseline),
        )
        self.missing = missing + [name for name, value in named if value is None]

    def on_start(self) -> None:
        now = self.clock.timestamp_ns()
        if self.consumer is not None:
            self.subscribe_data(ranking_data_type(self.consumer.feed))
            self.subscribe_signal(heartbeat_signal(self.consumer.feed))
        for iid, book in self.books.items():
            self.subscribe_trades(iid)
            self.subscribe_quotes(iid)
            instrument = self.cache.instrument(iid)
            if not isinstance(instrument, Equity):
                continue
            book.instrument = instrument  # protection never waits on entry rows
            self.reconcile(book)  # a (re)start covers what is held at once (ADR-0007:17)
            if self.missing or self.consumer is None:
                continue
            segment = self.consumer.reader.segment
            book.entry = EntryState(iid, self._p, segment.name, start_ns=segment.start_ns)
            self.subscribe_bars(book.entry.bar_type)
        rows = self.width_rows
        for iid, book in self.books.items():
            if rows is None:
                continue
            minutes = int(rows["trail_atr_bar_minutes"])
            book.width_bar = BarType.from_str(f"{iid}-{minutes}-MINUTE-LAST-EXTERNAL")
            book.oracle = AverageTrueRange(int(rows["trail_atr_period"]))
            self.register_indicator_for_bars(book.width_bar, book.oracle)
            if book.entry is None or book.entry.bar_type != book.width_bar:
                self.subscribe_bars(book.width_bar)
        if self.missing:
            self._record(kind="entry_refused", ts=now, missing=self.missing)
            return
        book = next((b for b in self.books.values() if b.instrument is not None), None)
        account = None if book is None else self.portfolio.account(book.instrument_id.venue)
        if book is not None and account is not None and self.basis_path is not None:
            assert self.consumer is not None  # "ranking_consumer" is not missing
            segment = self.consumer.reader.segment
            key = f"{segment.session_date.isoformat()}:{segment.name}:{segment.start_ns}"
            store = SegmentBasis(self.basis_path, str(account.id), self._p)
            free = self._balance(book, "free")
            self.basis = store.freeze(key, free, first_read="node_start")  # N5's frozen basis

    def _seed(self, book: _Book) -> None:
        """The trail seed, asked once per wait when a trail is denied for want of a trade
        (never at start: the node's 1,024-slot command queue, node_order.QUEUE): this session's
        latest trade (limit 1) through the engine's request, so the RiskEngine can price the
        trail before the next print (crates/risk/src/engine/mod.rs:2368-2391). The window is
        this session (``session_start_ns``, b_width.completed_atr's scope); no age bound inside
        it has a row, so the seed carries the trade's own timestamp."""
        iid, now = book.instrument_id, self.clock.timestamp_ns()
        if self.cache.trade(iid) is None:
            start, end = unix_nanos_to_dt(session_start_ns(now)), unix_nanos_to_dt(now)
            self.request_trades(iid, start=start, end=end, limit=1)

    def on_historical_trades(self, trades: Sequence[TradeTick]) -> None:
        """The seed's answer: the DataEngine caches it before this hook (crates/data/src/
        engine/mod.rs:1946-1951, :1974-1981), so a trail denied for want of a trade is armed
        now, as on the next print."""
        for trade in trades:
            book = self.books.get(trade.instrument_id)
            if book is None:
                continue
            now = self.clock.timestamp_ns()
            iid, price = str(trade.instrument_id), str(trade.price)
            self._record(kind="trail_seed", ts=now, instrument=iid, price=price, at=trade.ts_event)
            self._rearm(book, trade=True)

    @property
    def width_rows(self) -> Any:
        return self._config.width_rows

    def _route(self, book: _Book | None, **row: str) -> tuple[bool, ClientId | None]:
        """The venue's execution client for a command, or None where the venue routes, resolved
        before any of its bookkeeping (review s1p2b-eb33a6a). A row ``ClientId`` refuses or an
        absent one sends nothing: one refusal naming ``client_id``, and the book re-arms on its
        next price, so a restored row recovers (AGENTS.md:16, ADR-0007:17)."""
        value = getattr(self._config, "client_id", ABSENT)
        try:
            return True, None if value is None else ClientId(cast(str, value))
        except (TypeError, ValueError):
            if book is not None:
                book.rearm = True
            self._record(kind="refuse", ts=self.clock.timestamp_ns(), reason="client_id", **row)
            return False, None

    @property
    def native(self) -> Any:
        """The native row in force at this decision, never a construction copy (review P2)."""
        return self._config.native_trail_fraction

    @property
    def _p(self) -> EntryParameters:
        assert self.params is not None  # only reached when no entry row is missing
        return self.params

    def on_data(self, data: CustomData) -> None:
        if self.consumer is None:
            return
        now = self.clock.timestamp_ns()
        accepted = self.consumer.handle(data, now=now)
        self._record(kind="generation", ts=now, accepted=accepted, reason=self.consumer.reason)

    def on_signal(self, signal: Any) -> None:
        # Same native heartbeat receiver as ranking/consumer.py RankingDataActor.
        if self.consumer is not None and signal.name == heartbeat_signal(self.consumer.feed):
            self.consumer.accept_heartbeat(
                at=signal.ts_event, received_ns=self.clock.timestamp_ns()
            )

    def on_bar(self, bar: Bar) -> None:
        book = self.books.get(bar.bar_type.instrument_id)
        if book is not None and book.entry is not None and bar.bar_type == book.entry.bar_type:
            book.entry.on_bar(bar, now=self.clock.timestamp_ns())
        if book is not None and bar.bar_type == book.width_bar:
            self._width(book, bar)

    def on_quote(self, quote: QuoteTick) -> None:
        book, now = self.books.get(quote.instrument_id), self.clock.timestamp_ns()
        if book is None or not 0 <= quote.ts_event <= quote.ts_init <= now:
            return
        if book.quote is not None and quote.ts_event < book.quote.ts_event:
            return
        book.quote = quote
        self._session(book, now)
        self._rearm(book)
        if book.stale_since is not None and quote.ts_event >= book.stale_since:
            gap = quote.ts_event - book.stale_since  # the carried trigger's unquoted interval
            self._record(
                kind="session_quote", ts=now, instrument=str(book.instrument_id), gap_ns=gap
            )
            book.stale_since = None
        if book.entry_id is not None:
            self._reprice(book, now)

    def on_trade(self, trade: TradeTick) -> None:
        book = self.books.get(trade.instrument_id)
        if book is None:
            return
        now = self.clock.timestamp_ns()
        self._session(book, now)
        self._rearm(book, trade=True)  # the engine caches the trade first (dispatch.rs:117-128)
        price, book.last = trade.price.as_decimal(), trade.price.as_decimal()
        book.high = None if self._held(book) <= 0 else max(book.high or price, price)
        allowed = self.candidate(book, now)
        if book.entry is None or self.baseline is None or self.exit is None:
            return
        book.entry.on_trade(trade, now=now)
        book.trades.append((trade.ts_event, trade.price.as_decimal()))  # R_w (exits/engine.py)
        start = trade.ts_event - int(self.exit.prearm_range_window_s * NS)
        while book.trades[0][0] < start:
            book.trades.popleft()
        quote = book.quote
        quote = quote if quote is not None and quote.ts_event <= trade.ts_event else None
        try:  # C's order: the entry state first, then the surge detector (c_session.py:200-222)
            book.surge, flag = advance_surge(
                book.surge,
                trade,
                self.baseline,
                segment=cast(RankingConsumer, self.consumer).reader.segment,  # entry: consumer
                feed=self.baseline.feed,
                params=self._p,
                quote=quote,
            )
        except ValueError as error:
            self._record(
                kind="refuse", ts=now, instrument=str(book.instrument_id), reason=str(error)
            )
            return
        if flag is not None:
            book.entry.arm(flag)
        if book.entry_id is not None:
            self._reprice(book, now)
        elif allowed:
            self._enter(book, now)

    def candidate(self, book: _Book, now: int) -> bool:
        """The rank gate over one current ``entry_rank``; DISABLED means no house entry."""
        enabled, consumer = self.rank_gate.value, self.consumer
        rank = None
        if enabled is True and consumer is not None:
            rank = consumer.entry_rank(str(book.instrument_id.symbol), now=now)
        decision = decide_rank_gate(
            enabled=cast(bool, enabled),
            rank=rank,
            consumer_reason=None if consumer is None else consumer.reason,
        )
        book.gate, book.gate_ns = decision, now
        allowed = decision.status is RankGateStatus.PASS
        self._record(
            kind="rank",
            ts=now,
            instrument=str(book.instrument_id),
            status=decision.status.name,
            reason=decision.reason,
            rank=rank,
            candidate=allowed,
        )
        return allowed

    def _enter(self, book: _Book, now: int) -> None:
        """N1/N4 in the completing trade's handler: one LIMIT BUY at the ask, never market."""
        entry, quote, p = book.entry, self._fresh_quote(book, now), self._p
        assert entry is not None and book.instrument is not None
        surge = entry.surge
        if surge is None or quote is None or surge.flag_ts == book.refused_flag:
            return
        if not entry.first_leg_ready(quote_ask(quote), now):
            return
        expiry = min(now + p.ns("entry_ttl_s"), surge.flag_ts + p.ns("first_leg_window_s"))
        if expiry <= now:  # replay.py: a submit here would cancel inside its own borrow
            return self._refuse(book, now, "first_leg_window_s")
        price = limit_price(quote_ask(quote), book.instrument.price_increment.as_decimal())
        sizing = self._size(book, price, quote, now)
        if sizing.refusal is not None:
            book.refused_flag = surge.flag_ts  # c_session.py: refused until a new flag
            return self._refuse(book, now, sizing.refusal)
        routed, client = self._route(book, instrument=str(book.instrument_id))
        if not routed:
            return
        order = self.order_factory.limit(
            book.instrument_id,
            OrderSide.BUY,
            sizing.quantity,
            Price.from_decimal_dp(price, book.instrument.price_precision),
            time_in_force=TimeInForce.DAY,
            tags=[f"params_hash={p.params_hash}", "setup=FIRST_LEG"],
        )
        book.entry_id, book.first_limit, book.expiry_ns = order.client_order_id, price, expiry
        self._record(kind="submit", ts=now, limit=str(price), **sizing.journal())
        iid = book.instrument_id
        self.clock.set_time_alert_ns(
            f"b-entry-ttl-{order.client_order_id}", expiry, callback=lambda _e: self._expire(iid)
        )
        outside = session_at(now) is not Session.RTH
        self.submit_order(order, client_id=client, params={"extended_hours": outside})

    def _reprice(self, book: _Book, now: int) -> None:
        """Reprice up to the ask, never past ``entry_chase_bps`` above the first limit."""
        order = None if book.entry_id is None else self.cache.order(book.entry_id)
        quote = self._fresh_quote(book, now)
        if order is None or not order.is_open or order.is_inflight or order.is_pending_cancel:
            return
        if quote is None or book.expiry_ns is None or now >= book.expiry_ns:
            return
        assert book.entry is not None and book.instrument is not None
        price = limit_price(quote_ask(quote), book.instrument.price_increment.as_decimal())
        if book.first_limit is None or price <= order.price.as_decimal():
            return
        if not book.entry.first_leg_ready(quote_ask(quote), now):
            return
        if not within_chase(price, book.first_limit, self._p):
            return self._refuse(book, now, "entry_chase_bps")
        sizing = self._size(book, price, quote, now, working=order)
        if sizing.refusal is not None or sizing.quantity < order.quantity:
            return self._refuse(book, now, sizing.refusal or "quantity")
        if not self.candidate(book, now):
            return
        self._record(kind="modify", ts=now, limit=str(price))
        native = Price.from_decimal_dp(price, book.instrument.price_precision)
        self._modify(order.client_order_id, price=native)

    def _expire(self, iid: InstrumentId) -> None:
        """TTL: cancel the remainder of this book's own entry, by its own id only."""
        book = self.books[iid]
        order = None if book.entry_id is None else self.cache.order(book.entry_id)
        if order is None or order.is_closed or order.is_pending_cancel:
            return
        self._record(kind="cancel", ts=self.clock.timestamp_ns(), order=str(order.client_order_id))
        self._cancel(order.client_order_id)

    # -- S1-3: first protection, the quantity rule and the house guard -------------------

    def on_order_filled(self, event: Any) -> None:
        self._on_order(event)

    def on_order_accepted(self, event: Any) -> None:
        self._on_order(event)

    def on_order_canceled(self, event: Any) -> None:
        self._on_order(event)

    def on_order_expired(self, event: Any) -> None:
        self._on_order(event)

    def on_order_denied(self, event: Any) -> None:
        self._refused(event, "denied")

    def on_order_rejected(self, event: Any) -> None:
        self._refused(event, "rejected")

    def _refused(self, event: Any, reason: str) -> None:
        """OrderDenied, by Nautilus before the venue (events/order/denied.rs:36-39), and
        OrderRejected, by the venue (rejected.rs:36): either pages, and a refused trail re-arms
        on the book's next quote or trade, never by a resubmit inside the refusal. A trail denied
        with no trade cached waits for the next trade, paged once: rc8's RiskEngine prices a
        default-trigger trail from the last trade only (crates/risk/src/engine/mod.rs:2368-2391,
        denied at :2144), so a quote cannot change it (stage 1 rehearsal finding 2)."""
        cid, iid = event.client_order_id, event.instrument_id
        book, order = self.books.get(iid), self.cache.order(cid)
        if book is None or order is None or not _is_trail(order):
            return self._record(kind="page", ts=event.ts_event, reason=reason, order=str(cid))
        priced = reason != "denied" or self.cache.trade(iid) is not None
        if priced or not book.no_trade:
            page = reason if priced else "denied_no_trade"
            self._record(kind="page", ts=event.ts_event, reason=page, order=str(cid))
        if priced:
            book.rearm = True
        elif not book.no_trade:  # a new wait: one seed request until a trade clears it
            book.no_trade = True
            self._seed(book)

    def _rearm(self, book: _Book, trade: bool = False) -> None:
        """A trail the RiskEngine denied (its submit rate, or no price) is armed again on the
        book's next quote or trade, or on its next trade when no trade was cached, not
        recursively inside the denial."""
        self._due_cancels(book)
        if trade and book.no_trade:
            book.no_trade, book.rearm = False, True
        if book.rearm:
            book.rearm = False
            self.reconcile(book)

    def _on_order(self, event: Any) -> None:
        book = self.books.get(event.instrument_id)
        if book is None:
            return
        cid = event.client_order_id
        if self.cache.order(cid) is None and cid not in book.not_in_cache:
            # An anomaly made visible once (lead ruling 07:5x): an absent row ends nothing and
            # counts toward no cover.
            book.not_in_cache.add(cid)
            now = self.clock.timestamp_ns()
            self._record(kind="page", ts=now, reason="order_not_in_cache", order=str(cid))
        for ended in [
            c for c in {*book.cancelling, *book.recancel, *book.exhausted} if self._ended(c)
        ]:
            self._forget(book, ended)  # the cancel's terminal event: re-arm may follow
        self._due_cancels(book)
        self.reconcile(book)

    def _forget(self, book: _Book, cid: ClientOrderId) -> None:
        """Everything the cancel's record holds for an order that has ended."""
        for record in (book.recancel, book.cancel_tries, book.cancel_sent):
            record.pop(cid, None)
        book.cancelling.discard(cid)
        book.exhausted.discard(cid)

    def _ended(self, cid: ClientOrderId) -> bool:
        order = self.cache.order(cid)
        return order is not None and order.is_closed

    def reconcile(self, book: _Book) -> None:
        """One house trail of the full unreserved whole long quantity (ADR-0007:17, the owner's
        2026-11-09 policy, ADR-0007:50): "protect every unreserved whole long share with exactly
        one house trail of the full net quantity and re-arm it (cancel plus one resubmit)
        whenever that quantity changes". Never a second trail beside a working one, never a
        quantity modify. Position and orders are the engine's cache under this strategy id.

        - target = floor(held - owner SELL reservations), an owner OCO or bracket pair counted
          once (only one of its two exits can execute: Alpaca orders page :165);
        - a trail that is TRIGGERED or partly filled is completing (LEAD-2201 B3 a): nothing is
          sent; one page; the target is armed for what remains once it ends;
        - a trail whose quantity in force differs from the target is cancelled, and one trail of
          the full target is armed on the cancel's terminal event; between the two the shares
          have no venue order (ADR-0007:25, the accepted gap), bounded by ``_rearm_due``.
        """
        iid, now = book.instrument_id, self.clock.timestamp_ns()
        live = [o for o in self.cache.orders(instrument_id=iid, strategy_id=self.strategy_id)]
        live = [o for o in live if not o.is_closed]
        trails = [o for o in live if self._house(o.client_order_id) and _is_trail(o)]
        ids = {o.client_order_id for o in trails}
        held = self._held(book)
        reserved = _reserved(
            [o for o in live if o.side is OrderSide.SELL and o.client_order_id not in ids]
        )
        target = max(Decimal(0), held - reserved).to_integral_value(rounding=ROUND_FLOOR)
        if held > 0 and target == 0:
            self._page(book, now, "owner_reserved_unprotected")
        if any(o.status is OrderStatus.TRIGGERED or o.filled_qty.as_decimal() > 0 for o in trails):
            return self._page(book, now, "exit_triggered_left_to_complete")
        busy = [o for o in trails if o.is_inflight or o.is_pending_cancel]
        held_back = {*book.cancelling, *book.recancel, *book.exhausted}
        if busy or any(o.client_order_id in held_back for o in trails):
            return  # a submit or a cancel in flight: one command at a time, wait for its answer
        if not trails:
            if book.no_trade and self.cache.trade(iid) is None:
                return  # it would be denied again: no order id spent until the next trade
            if target > 0:
                cid = self._arm(book, now, target)
                if cid is not None and book.rearm_from is not None:
                    self._rearm_due(book, cid, now)
            return
        if len(trails) == 1 and trails[0].quantity.as_decimal() == target:
            return
        book.rearm_from = now  # cancel plus one resubmit: the new trail is owed at the end
        for order in trails:
            self._cancel(order.client_order_id)

    def _rearm_due(self, book: _Book, cid: ClientOrderId, now: int) -> None:
        """The re-arm's gap is bounded (ADR-0007:25, "asynchronous gaps are explicit"): a new
        trail the venue has not accepted within the cancel bound (exits/engine.py:96) pages."""
        self._record(kind="rearm", ts=now, order=str(cid), cancel_ns=book.rearm_from)
        book.rearm_from = None

        def check(_event: Any) -> None:
            order = self.cache.order(cid)
            if order is None or order.status in (OrderStatus.INITIALIZED, OrderStatus.SUBMITTED):
                self._page(book, self.clock.timestamp_ns(), "rearm_not_accepted")

        self.clock.set_time_alert_ns(f"b-rearm-{cid}", now + CANCEL_RETRY_NS, callback=check)

    def _arm(self, book: _Book, now: int, shares: Decimal) -> ClientOrderId | None:
        native = self.native
        if native is None or not isinstance(native, Decimal) or not native.is_finite():
            book.rearm = True  # held and uncovered: re-arm on the first price with valid authority
            reason = "missing" if native is None else "invalid"
            self._page(book, now, f"native_trail_fraction_{reason}")
            return None
        if not 0 < native <= MAX_TRAIL_FRACTION:  # finite first: NaN never reaches a compare
            book.rearm = True
            self._page(book, now, "native_trail_fraction_invalid")  # ADR-0025's cap
            return None
        routed, client = self._route(book, instrument=str(book.instrument_id))
        if not routed:
            return None
        width = book.width  # revalidated at each arm: the rows in force and the native cap
        bar = book.width_bar
        if width is not None and (
            bar is None or _width_period(self.width_rows, bar) is None or width > native
        ):
            width = None  # the native row, never a width the current authority does not give
        if width is None and self.width_rows is not None:
            self._page(book, now, "width_unavailable")  # Q4: arm at the native row and page
        bps = trail_percent(native if width is None else width) * 100  # rounded wider
        order = self.order_factory.trailing_stop_market(
            book.instrument_id,
            OrderSide.SELL,
            Quantity.from_int(int(shares)),
            bps,
            trailing_offset_type=TrailingOffsetType.BASIS_POINTS,
            time_in_force=TimeInForce.GTC,
            reduce_only=self._config.trail_reduce_only,  # the venue's row
            tags=["protect"],
        )
        self._record(kind="protect", ts=now, order=str(order.client_order_id), quantity=str(shares))
        book.offsets[order.client_order_id] = bps
        self.submit_order(order, client_id=client)
        return order.client_order_id

    # -- S1-4: width from completed bars, tighten-only modify -----------------------------

    def _width(self, book: _Book, bar: Bar) -> None:
        """D-30's width from this RTH session's completed bars (b_width.completed_atr's scope).

        The engine's ``AverageTrueRange`` is registered for the bars and updated before this
        handler; the Decimal ATR is fed the same bar and is the value used. A new RTH session
        resets both and re-feeds its first bar to the oracle.
        """
        rows, now = self.width_rows, self.clock.timestamp_ns()
        assert book.oracle is not None and book.width_bar is not None
        if session_at(bar.ts_init - 1) is not Session.RTH:
            return  # no new width outside RTH: the last one stays (b_width.py)
        start = session_start_ns(bar.ts_init - 1)
        if book.atr_session != start:
            book.atr_session, book.session_bars = start, []
            book.oracle.reset()
            book.oracle.handle_bar(bar)
        book.session_bars.append(bar)
        # Rows in force at each bar: invalid ones keep trails, arm at native, never tighten.
        period = _width_period(rows, book.width_bar)
        if period is None:
            book.width = None
            return self._page(book, now, "width_rows_invalid")
        # b_width.completed_atr: Wilder ATR over all of this RTH session's completed bars,
        # computed afresh, so a new session, a new period or a recovered row replays them all.
        book.atr = DecimalATR(period)
        for completed in book.session_bars:
            book.atr.handle_bar(completed)
        quote = book.quote
        if book.atr.value is None or quote is None:
            return
        view = view_from_quote(quote.bid_price.as_decimal(), quote_ask(quote))
        try:
            width = trail_fraction(
                native=self.native,
                minimum=rows["alpaca_min_trail_fraction"],
                multiple=rows["trail_atr_multiple"],
                atr=book.atr.value,
                view=view,
            )
        except (TypeError, ValueError):
            book.width = None
            return self._page(book, now, "width_rows_invalid")
        if width is None:
            return
        book.width = width
        self._record(
            kind="width",
            ts=now,
            bar_ts=bar.ts_init,
            atr=str(book.atr.value),
            oracle=book.oracle.value,
            width=str(width),
        )

    def on_order_updated(self, event: Any) -> None:
        self._on_order(event)  # a confirmed quantity replacement moves the reservation

    def _held(self, book: _Book) -> Decimal:
        positions = self.cache.positions_open(
            instrument_id=book.instrument_id, strategy_id=self.strategy_id
        )
        return sum((p.quantity.as_decimal() for p in positions if p.is_long), Decimal(0))

    def _session(self, book: _Book, now: int) -> None:
        """Section F: one record at every session change; the same trail rests across it."""
        session = session_at(now)
        if book.session is not None and session is not book.session:
            trails = [
                o
                for o in self.cache.orders(
                    instrument_id=book.instrument_id, strategy_id=self.strategy_id
                )
                if not o.is_closed and self._house(o.client_order_id) and _is_trail(o)
            ]
            self._record(
                kind="session",
                ts=now,
                boundary_ns=session_start_ns(now),
                instrument=str(book.instrument_id),
                carry=True,
                trails=[str(o.client_order_id) for o in trails],
                triggers=[
                    None if o.trigger_price is None else str(o.trigger_price) for o in trails
                ],
                **{"from": book.session.value, "to": session.value},
            )
            book.stale_since = session_start_ns(now)  # the boundary itself
        book.session = session

    def _house(self, cid: ClientOrderId) -> bool:
        """The house namespace: rc8's generator ``O-date-time-trader-{order_id_tag}-count``."""
        parts = str(cid).split("-")
        return len(parts) == 6 and parts[0] == "O" and parts[4] == self.tag

    def _cancel(self, cid: ClientOrderId) -> None:
        if not self._house(cid):  # LEAD-1818 D-c: never an owner order
            return self._record(kind="guard_refused", order=str(cid))
        order = self.cache.order(cid)
        book = None if order is None else self.books.get(order.instrument_id)
        if book is not None and order is not None:
            if _trail_started(order):  # LEAD-2201 B3(a): a completing trail is never cancelled
                return self._left_to_complete(book, cid)
            if cid in book.exhausted:  # K+1 cancels were refused (ADR-0007:27): no more
                return
        routed, client = self._route(book, order=str(cid))  # before the cancel's record
        if not routed:
            return
        if book is not None:
            book.cancelling.add(cid)  # an OrderUpdated clears PENDING_CANCEL (mod.rs:1314-1316)
            book.cancel_sent[cid] = self.clock.timestamp_ns()  # the retry's clock
            book.cancel_tries[cid] = book.cancel_tries.get(cid, 0) + 1
        self.cancel_order(cid, client_id=client)

    def _left_to_complete(self, book: _Book, cid: ClientOrderId) -> None:
        if cid not in book.started:
            book.started.add(cid)
            now = self.clock.timestamp_ns()
            self._record(
                kind="page", ts=now, reason="exit_triggered_left_to_complete", order=str(cid)
            )

    def _modify(self, cid: ClientOrderId, **change: Any) -> None:
        if not self._house(cid):
            return self._record(kind="guard_refused", order=str(cid))
        routed, client = self._route(None, order=str(cid))  # a reprice retries on its own
        if routed:
            self.modify_order(cid, client_id=client, **change)

    def _page(self, book: _Book, now: int, reason: str) -> None:
        if book.paged != reason:  # one page per state, not one per event
            self._record(kind="page", ts=now, instrument=str(book.instrument_id), reason=reason)
        book.paged = reason

    def on_order_cancel_rejected(self, event: Any) -> None:
        """A rejected cancel only marks. The cancel bound (exits/engine.py:303-330): sent again
        once CANCEL_RETRY_NS has passed since that order's last cancel send, by a clock alert or
        the next event, never inside this callback; at most K+1 cancels per order
        (ADR-0007:27), then the order is exhausted until it ends. A cancel refused before the
        venue accepted the order goes again on its acceptance."""
        cid = event.client_order_id
        self._record(kind="page", ts=event.ts_event, reason="cancel_rejected", order=str(cid))
        book = self.books.get(event.instrument_id)
        if book is None:
            return
        book.cancelling.discard(cid)
        now, order = self.clock.timestamp_ns(), self.cache.order(cid)
        if book.cancel_tries.get(cid, 0) > CANCEL_RETRIES:
            book.exhausted.add(cid)
            return self._page(book, now, "cancel_retries_exhausted")
        due = book.cancel_sent.get(cid, now - CANCEL_RETRY_NS) + CANCEL_RETRY_NS
        if order is not None and order.status is OrderStatus.SUBMITTED:
            due = now  # its retry waits for the acceptance event (_due_cancels)
        book.recancel[cid] = due
        if order is None or order.status is not OrderStatus.SUBMITTED:
            # On the clock too, so a quiet market still sends it; one already due fires at once
            # from the engine's timer, never inside this callback (nautilus_trader rc8
            # crates/common/src/clock/virtual.rs:327, live/clock.rs:215).
            name = f"b-recancel-{cid}-{book.cancel_tries.get(cid, 0)}"
            self.clock.set_time_alert_ns(
                name, max(due, now), callback=lambda _e, b=book: self._due_cancels(b)
            )

    def _due_cancels(self, book: _Book) -> None:
        """Each rejected cancel whose retry is due goes out again through ``_recancel``. An
        absent row and an order not yet accepted keep their retry."""
        now = self.clock.timestamp_ns()
        for cid, due in list(book.recancel.items()):
            order = self.cache.order(cid)
            if due > now or order is None or order.status is OrderStatus.SUBMITTED:
                continue
            del book.recancel[cid]
            self._recancel(cid)

    def _recancel(self, client_order_id: ClientOrderId) -> None:
        """The one re-send of a rejected cancel: only while the order still works
        (exits/engine.py:323-330, northstar strategies/base.py:2186-2189); a completing trail
        is refused at the send (``_cancel``)."""
        order = self.cache.order(client_order_id)
        if order is not None and not order.is_closed:
            self._cancel(client_order_id)

    def _fresh_quote(self, book: _Book, now: int) -> QuoteTick | None:
        quote = book.quote
        if quote is None or now - quote.ts_event > self._p.ns("max_data_age_s"):
            return None
        if not 0 < quote.bid_price.as_decimal() <= quote_ask(quote):
            return None  # crossed or one-sided: UNKNOWN, never the older quote (replay.py)
        return quote

    def _size(
        self, book: _Book, price: Decimal, quote: QuoteTick, now: int, working: Any = None
    ) -> Sizing:
        """N5 through ``size_entry``; the initial trigger is ``entry - buffer`` (ADR-0004)."""
        assert book.entry is not None and book.instrument is not None and self.exit is not None
        prices = [traded for _, traded in book.trades]
        spread = quote_ask(quote) - quote.bid_price.as_decimal()
        tick = book.instrument.price_increment.as_decimal()
        width = buffer(
            tick, spread, price, max(prices) - min(prices) if prices else None, self.exit
        )
        if width is None:  # an unknown R_w makes the buffer unknown (policy.buffer)
            return Sizing(Quantity.from_int(0), None, None, None, None, "buffer_unknown", None)
        free = self._balance(book, "free")
        if free is not None and working is not None:  # replay.py: the working order's own funds
            free += working.leaves_qty.as_decimal() * working.price.as_decimal()
        return size_entry(
            instrument=book.instrument,
            params=self._p,
            setup="FIRST_LEG",
            entry=price,
            initial_trigger=price - width,
            equity=self._balance(book, "total"),
            basis=self.basis,
            buying_power=free,
            dollar_volume=book.entry.flow_dollars(now),
            confidence=None,
            prearm_stopouts=0,
            commission_rate=self.commission,
        )

    def _balance(self, book: _Book, name: str) -> Decimal | None:
        account = self.portfolio.account(book.instrument_id.venue)
        if account is None or book.instrument is None:
            return None
        balance = getattr(account, f"balance_{name}")(book.instrument.quote_currency)
        return None if balance is None else balance.as_decimal()

    def _refuse(self, book: _Book, now: int, reason: str) -> None:
        self._record(kind="refuse", ts=now, instrument=str(book.instrument_id), reason=reason)

    def _record(self, **row: Any) -> None:
        if self._journal is None:
            return
        try:
            self._journal(row)
        except Exception:  # containment: evidence never changes a decision
            self.journal_errors += 1


def _reserved(sells: list[Any]) -> Decimal:
    """The owner's SELL reservations: each unlinked order's leaves, and an OCO or bracket pair
    once, at its larger leg, since only one of its two exits can execute (Alpaca orders page
    :165), read from the engine's native contingency fields."""
    single, pairs = Decimal(0), {}
    for o in sells:
        linked = o.linked_order_ids or []
        if o.contingency_type in (ContingencyType.OCO, ContingencyType.OUO) and linked:
            key = o.order_list_id or frozenset({o.client_order_id, *linked})
            pairs[key] = max(pairs.get(key, Decimal(0)), o.leaves_qty.as_decimal())
        else:
            single += o.leaves_qty.as_decimal()
    return single + sum(pairs.values(), Decimal(0))


def _trail_started(order: Any) -> bool:
    """A trailing stop that has started executing: TRIGGERED or partly filled (B3 a)."""
    started = order.status is OrderStatus.TRIGGERED or order.filled_qty.as_decimal() > 0
    return _is_trail(order) and started


def _is_trail(order: Any) -> bool:
    return order.order_type is OrderType.TRAILING_STOP_MARKET


def quote_ask(quote: QuoteTick) -> Decimal:
    return quote.ask_price.as_decimal()


def _width_period(rows: Mapping[str, Any] | None, bar_type: BarType) -> int | None:
    """The rows in force: exactly four positive Decimals, whole period and the bar's minutes."""
    if rows is None or set(rows) != WIDTH_ROWS:
        return None
    values = [rows[name] for name in sorted(WIDTH_ROWS)]
    if not all(isinstance(v, Decimal) and v.is_finite() and v > 0 for v in values):
        return None
    period, minutes = rows["trail_atr_period"], rows["trail_atr_bar_minutes"]
    if (
        period % 1
        or minutes % 1
        or not str(bar_type).endswith(f"-{int(minutes)}-MINUTE-LAST-EXTERNAL")
    ):
        return None
    return int(period)

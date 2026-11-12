"""The exit engine as a Nautilus strategy: protect the native position from its first fill, raise
the trigger on every event, sell on the first persisted breach and chase the managed LIMIT.

Policy: sextant.exits.policy (ADR-0005 X2-X6). Seams: nautechsystems/nautilus_trader@v2.0.0rc7
55c0e82053142bce3a5cba4de4221d3929c2d3bc: `Strategy.subscribe_quotes`, `subscribe_trades` and
`subscribe_bars`; `order_factory.limit` as in examples/live/architect_ax/strategies.py
(OrderBookImbalance.on_quote); `Strategy.modify_order` per docs/concepts/strategies.md. That
method raises on an unchanged price, so the engine amends only a changed level, one at a time.
Internal 1-minute bars come from eligible trades. Replay and sim only: this module places no
broker order and holds no key.

The protected quantity is the native position (R86, #129), read with `Cache.position` in
`on_position_opened/changed/closed` (docs/concepts/cache.md:659-672, strategies.md:229-234). Each
position event follows its `OrderFilled` (docs/concepts/events/index.md:123-160).
- Only a position a house order opened is protected; the house is what this engine's own
  `submit_order` sent (ADR-0007:16), and any other opener pauses the symbol and pages (#209).
- A house order's further fills grow the protection, so a partly filled entry is protected in full.
  H moves only on eligible trades, never on an `OrderFilled` (X2, #210).
- A BUY the house didn't send is an add, which pauses the symbol, pages and cancels the house's
  resting sell, which the venue would resize over it (ADR-0007:17, #170). No open position and no
  unfinished order is a verified flat: it resets the protection and clears a pause, re-checked on
  each quote while paused (#171).
- It assumes NETTING (one position per symbol) and an in-process house set; a restart or HEDGING
  needs durable order lineage first (ADR-0007:16).
- The basis is rc8's Decimal `Position.avg_px_open`, this cycle's own fills (v2.0.0rc8
  docs/concepts/positions.md:144-145, :419), never `Order.avg_px`, which blends cycles (#169).
- A persisted breach is retained intent and freezes the levels: the house cancels its own working
  orders (an entry's remainder) once each, and on their terminal cancel one reduce-only LIMIT sells
  the position. A rejected cancel goes out again once 5 s have passed since that order's last
  cancel request, while the order still works (northstar-trading@8256c16c strategies/base.py:101,
  :1728-1754, :2186-2189; alpaca.py:639-640, :977-978), up to ADR-0007:27's K+1 per order, then
  pages. A clock alert pages a wait past ADR-0005:36's 60-second deadline (alpaca.py:981-995;
  v2.0.0rc8 docs/concepts/strategies.md:960-967, managed GTD expiry; northstar-trading@8256c16c
  docs/adr/0002-alpaca-native-brackets.md:354-366). A working order the house didn't send freezes
  and pages instead (ADR-0007:19; #212, A2), as does a refused sell; a stale or one-sided quote
  never prices it, and an unavailable chase pages (#211).
- A level at the bid is refused, a raise into the bid (now or when H was made) keeps the previous
  level, and an unknown bid sets none (rc8 matching_engine/mod.rs:4311-4330, :6774-6812;
  investor.gov "Types of Orders", read 2026-11-07); a crossed level sells per ADR-0005:26 (#210).
- An equity crossing an XNYS RTH boundary (sextant.paper.sessions) switches the breach input and
  hands the exit over by a terminal cancel (ADR-0005, venue limits).

Scope of this slice: the take-profit and backstop are computed as desired levels and reported. They
are venue orders (Alpaca OCO/bracket, ADR-0005 X6), placed later by the custody row, not here.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from nautilus_trader.common import TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import (
    Bar,
    BarType,
    ClientOrderId,
    Equity,
    InstrumentId,
    OrderCanceled,
    OrderCancelRejected,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
    OrderSide,
    PositionChanged,
    PositionClosed,
    PositionId,
    PositionOpened,
    PositionSide,
    Price,
    QuoteTick,
    TimeInForce,
    TradeTick,
)
from nautilus_trader.trading import Strategy

from sextant.exits import policy
from sextant.exits.policy import Bar1mArming, BreachRun, ExitParams, Row
from sextant.paper.sessions import Session, session_at

NS = 1_000_000_000
CANCEL_DEADLINE_NS = 60 * NS  # ADR-0005:36: the first cancellation wait's escalation deadline
CANCEL_RETRIES = 3  # ADR-0007:27: K, so at most K+1 cancels for one order
NORTHSTAR = (
    "northstar-trading@8256c16c31dcb8e53fc726e0c1c77dc2c8d0d4fa:src/northstar_trading/strategies"
)
# The cancel-first wait's own row: an UNCALIBRATED start, never measured success (Rule 2).
REGISTRY: tuple[Row, ...] = (
    Row("cancel_retry_s", "seconds", Decimal(5), f"{NORTHSTAR}/base.py:101"),
)
_RETRY_S = REGISTRY[0].start
assert _RETRY_S is not None
CANCEL_RETRY_NS = int(_RETRY_S * NS)  # northstar's `CANCEL_RETRY_NS = 5 * SEC`
MINUTE_NS = 60 * NS  # XNYS boundaries fall on whole minutes (sextant.paper.sessions.session_at)


class ExitEngineConfig(StrategyConfig):
    def __init__(
        self,
        *,
        instrument_id: InstrumentId,
        params: ExitParams,
        round_trip_cost: Decimal,
        session: str,
        one_tick: Decimal | None = None,
        arm_bar_type: str | None = None,
        range_estimable: bool = True,
        **_kwargs: object,
    ) -> None:
        """`round_trip_cost` is ADR-0005's c_RT override; `session` is "rth" (breach on bids) or
        "outside" (breach on eligible trades). `one_tick` is the broker tick when the instrument's
        increment is a lossless matching precision (trading@8256c16c b_gain_protection.py:411-415);
        `arm_bar_type` names the completed 1-minute bars for arming (default: internal from trades);
        `range_estimable=False` drops R_w where the data cannot estimate it (#98)."""
        super().__init__()
        if session not in {"rth", "outside"}:
            raise ValueError("session must be rth or outside")
        self.instrument_id = instrument_id
        self.params = params
        self.round_trip_cost = round_trip_cost
        self.session = session
        self.one_tick = one_tick
        self.arm_bar_type = arm_bar_type
        self.range_estimable = range_estimable


@dataclass
class Protection:
    entry: Decimal
    qty: Decimal
    high: Decimal
    trigger: Decimal | None = None
    setup_gap: Decimal | None = None
    take_profit: Decimal | None = None
    tp_anchor: Decimal | None = None
    backstop: Decimal | None = None
    high_bid: Decimal | None = None  # the bid when H was made, judging a raise; None: unknown
    entry_bid: Decimal | None = None  # the bid at the entry, judging the first level (PR 2 F2)


class ExitEngine(Strategy):
    def __init__(self, config: ExitEngineConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.p = config.params
        self.cfg = config
        self.c_rt = config.round_trip_cost
        self.rth = config.session == "rth"
        self.bid: Decimal | None = None
        self.ask: Decimal | None = None
        self.quote_ns = 0
        self.last: Decimal | None = None
        self.trades: deque[tuple[int, Decimal]] = deque()
        self.arming = Bar1mArming()
        self.breach = BreachRun()
        self.protection: Protection | None = None
        self.position_id: PositionId | None = None
        self.entry_order_id: ClientOrderId | None = None
        self.sell_id: ClientOrderId | None = None
        self.one_tick = Decimal(0)
        self.amended_ns = 0
        self.paused = False
        self.pages: list[str] = []  # every page sent; notified dedupes it within a cycle
        self.notified: set[str] = set()
        self.events: list[dict[str, str]] = []
        self.house: set[ClientOrderId] = set()
        self.canceled: set[ClientOrderId] = set()
        self.cancel_sent: dict[ClientOrderId, int] = {}  # each reservation's last cancel request
        self.exits: set[ClientOrderId] = set()  # the house's own exits: each a later close
        self.rejects: Counter[ClientOrderId] = Counter()
        self.crossed: int | None = None  # when the persisted breach became exit intent
        self.xnys: Session | None = None
        self.minute: int | None = None  # the clock minute whose session is known

    def submit_order(self, order, *args, **kwargs) -> None:
        self.house.add(order.client_order_id)
        super().submit_order(order, *args, **kwargs)

    def on_start(self) -> None:
        instrument_id = self.instrument_id
        instrument = self.cache.instrument(instrument_id)
        assert instrument is not None
        self.one_tick = self.cfg.one_tick or instrument.price_increment.as_decimal()
        self.equity = isinstance(instrument, Equity)
        self.subscribe_quotes(instrument_id)
        self.subscribe_trades(instrument_id)
        bar_type = self.cfg.arm_bar_type or f"{instrument_id}-1-MINUTE-LAST-INTERNAL"
        self.subscribe_bars(BarType.from_str(bar_type))

    # Market data. Pre-entry ticks warm the range and the last quote/trade, never H or a breach.
    def on_quote(self, quote: QuoteTick) -> None:
        self.bid, self.ask = quote.bid_price.as_decimal(), quote.ask_price.as_decimal()
        self.quote_ns = quote.ts_event
        self._flat(quote.ts_event)
        self._session(quote.ts_event)
        eligible = self.rth and self._quote_ok()  # bids breach in RTH only
        self._step(quote.ts_event, self.bid if eligible else None)

    def on_trade(self, trade: TradeTick) -> None:
        price = trade.price.as_decimal()
        self.last = price
        self.trades.append((trade.ts_event, price))
        if self.protection is not None and price > self.protection.high:  # H's bid: fresh only
            self.protection.high, self.protection.high_bid = price, self._fresh_bid(trade.ts_event)
        self._session(trade.ts_event)
        if not self.rth:
            self._step(trade.ts_event, price)

    def on_bar(self, bar: Bar) -> None:
        if self.protection is None:
            return
        bid = self._fresh_bid(bar.ts_event)  # X3: an unavailable quote stays unarmed (#264)
        if bid is None or self.ask is None:  # and a minute it cannot judge ends a confirmation:
            self.arming.confirmed = False  # it holds only until a later close below (ADR-0005:29)
            return
        floor = policy.noise_floor(self.one_tick, self.ask - bid)
        level = self.protection.entry + self.c_rt + floor
        self.arming.on_bar_close(bar.close.as_decimal(), level)

    # Fills classify; the position events that follow each fill carry the quantity.
    def on_order_filled(self, event: OrderFilled) -> None:
        px, qty = event.last_px.as_decimal(), event.last_qty.as_decimal()
        if event.order_side != OrderSide.BUY:  # the house's exit, or an owner's own sell
            kind = "sell_fill" if event.client_order_id in self.house else "owner_sell_fill"
            self._record(event.ts_event, kind, price=px, qty=qty)
        elif self.entry_order_id is not None and event.client_order_id not in self.house:
            # ADR-0007:17: an add to a held symbol from another order pauses it and pages.
            self._pause_and_page(event.ts_event, "owner_add", qty=qty)
        elif self.entry_order_id is not None:  # a house order, filling further
            self._record(event.ts_event, "entry_fill", price=px, qty=qty)
        # A first fill opens the native position, and on_position_opened protects it.

    def on_position_opened(self, event: PositionOpened) -> None:
        if event.instrument_id != self.instrument_id:
            return
        ts = event.ts_event
        house = event.opening_order_id in self.house and event.side == PositionSide.LONG
        # Any reservation the crossing canceled keeps its exit owed, the entry or another (F2).
        late = house and self.crossed is not None and event.opening_order_id in self.canceled
        if house and not late:  # resolve a verified flat before a fresh opener (F3, D1)
            self._flat(ts, event.opening_order_id)
        if self.paused or self.position_id is not None or not house:
            self._pause_and_page(ts, "quantity_mismatch", qty=event.quantity.as_decimal())
            return
        self.position_id, self.entry_order_id = event.position_id, event.opening_order_id
        qty, entry = self._held()
        if late and self.protection is not None:  # the crossed entry's late fill: still owed (F2)
            self.protection.qty, self.protection.entry = qty, entry
            self._record(ts, "protected_from_fill", entry=entry, qty=qty)
            return self._sell(ts, self.protection)
        self.crossed, self.arming, self.breach, self.notified = (
            None,
            Bar1mArming(),
            BreachRun(),
            set(),
        )
        high, known = event.last_px.as_decimal(), self._fresh_bid(ts)  # anchors: fresh only
        self.protection = Protection(entry, qty, high, high_bid=known, entry_bid=known)
        self._record(ts, "protected_from_fill", entry=entry, qty=qty)
        self._step(ts, known if self.rth else self.last)

    def on_position_changed(self, event: PositionChanged) -> None:
        p = self.protection
        if p is None or self.paused or event.position_id != self.position_id:
            return
        p.qty, p.entry = self._held()
        self._record(event.ts_event, "protected_qty", qty=p.qty, entry=p.entry)
        # A reservation's fill during the wait: the exit is still owed, at the new size (F1).
        if self.crossed is not None and self.sell_id is None:
            self._sell(event.ts_event, p)

    def on_position_closed(self, event: PositionClosed) -> None:
        if event.instrument_id != self.instrument_id:
            return
        self._record(event.ts_event, "flat")
        # A close is no verified flat while a crossed entry is unfinished: keep its owed exit (F2).
        owed = self.crossed is not None and any(
            o.client_order_id in self.house for o in self._working()
        )
        self.position_id = self.sell_id = None
        if not owed:
            self.protection = self.entry_order_id = None
            self.crossed, self.arming, self.breach = None, Bar1mArming(), BreachRun()
            self.notified = set()  # each position cycle pages afresh
        self._flat(event.ts_event)

    def on_order_canceled(
        self, event: OrderCanceled | OrderExpired | OrderRejected | OrderDenied
    ) -> None:
        """Every terminal end, not only a cancel (rc8 dispatches each kind, crates/trading/src/
        strategy/mod.rs:1370-1393,1445-1464): it can complete a verified flat (F3), and a house
        reservation's end sends this position's owed exit at once (F1, ADR-0005:36)."""
        self._flat(event.ts_event)
        p, crossing = self.protection, self.crossed is not None and not self.paused
        if event.client_order_id in self.canceled and crossing and p is not None:
            self._sell(event.ts_event, p)

    on_order_expired = on_order_canceled

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        """A rejection cannot clear the intent (ADR-0005:36). As northstar's cancel-first wait does,
        the cancel goes out again once 5 s have passed since that order's last cancel request,
        never at once (northstar-trading@8256c16c strategies/base.py:101, :1728-1754, :2186-2189;
        alpaca.py:639-640, :977-978). A clock alert carries the wait, in rc8's restored-alert shape
        (crates/trading/src/strategy/mod.rs:2221-2227); if the 5 s have already passed, it goes at
        once (:2216-2219). Past K+1 cancels it pages. The order stays in `canceled`, the
        crossing's reservations, whatever its cancel attempts do."""
        cid, order = event.client_order_id, self.cache.order(event.client_order_id)
        if cid not in self.canceled or order is None or order.is_closed:
            return  # a late fill: the retained crossing sells it at the next step
        self.rejects[cid] += 1
        if self.rejects[cid] > CANCEL_RETRIES:
            return self._notify(event.ts_event, "cancel_rejected")
        now, due = self.clock.timestamp_ns(), self.cancel_sent[cid] + CANCEL_RETRY_NS
        if now >= due:
            return self._recancel(cid, now)
        name = f"cancel-retry-{cid}-{self.rejects[cid]}"
        self.clock.set_time_alert_ns(name, due, lambda e: self._recancel(cid, e.ts_event))

    def _recancel(self, cid: ClientOrderId, ts: int) -> None:
        """The re-send goes out only while the order still works ("if still working", northstar
        strategies/base.py:2186-2189): a terminal order is never canceled again."""
        order = self.cache.order(cid)
        if order is None or order.is_closed:
            return
        self._record(ts, "cancel_retry", order=str(cid))
        self._cancel(cid)

    def _cancel(self, cid: ClientOrderId) -> None:
        """The request time is the engine's clock at the send, as northstar's throttle reads
        `self._now()` (strategies/base.py:711, :1743), never an observation's own event time."""
        self.cancel_sent[cid] = self.clock.timestamp_ns()
        self.cancel_order(cid)

    def _deadline(self, event: TimeEvent) -> None:
        """ADR-0005:36's escalation on the clock, never on market data: rc8 expires a GTD order by
        a time alert (docs/concepts/strategies.md:960-967; crates/trading/src/strategy/mod.rs:
        2194-2232). It pages while any original reservation still works, or while this crossing's
        exit is owed with nothing working (F1). Only a later close is exempt: the house's own
        exit, resting or handed over (northstar ADR-0002:366-372; #231 gates N1, N2). Order side
        is no lineage, since a house SELL resting before the crossing is an original reservation.
        The exit goes out only once every house order has ended (`_cancel_house`), so while an
        original reservation works, no exit does."""
        late = self.crossed is not None and event.ts_event - self.crossed >= CANCEL_DEADLINE_NS
        sent = any(o.client_order_id in self.exits for o in self._working())
        if late and not self.paused and not sent:
            self._notify(event.ts_event, "partial_entry_cancel_timeout")

    def on_order_denied(self, event: OrderDenied | OrderRejected) -> None:
        if event.client_order_id == self.sell_id:  # the exit cannot go out (#211)
            return self._pause_and_page(event.ts_event, "sell_refused")
        self.on_order_canceled(event)  # any other order's end is terminal (F1, F3)

    on_order_rejected = on_order_denied

    # One step of X2-X5: levels and the breach run on an eligible observation (bid in RTH, trade
    # outside); any quote can send or reprice a retained exit, priced from the bid (#211).
    def _step(self, ts: int, observed: Decimal | None) -> None:
        p = self.protection
        if p is None or self.paused or self.bid is None or self.ask is None:
            return
        resting = self.sell_id is not None and self.position_id is not None
        if resting and self._foreign(ts):  # while the house sell rests too, not at the sell (C1)
            return
        fresh = self._quote_ok()  # the bid judges every level; a stale or one-sided one can't
        if observed is not None and fresh and self.crossed is None:
            self._levels(ts, p)
        if p.trigger is None:
            if not fresh:  # in both sessions (M5)
                self._notify(ts, "quote_unavailable")
            return
        if self.sell_id is None:
            breach = observed is not None and self.breach.observe(observed, p.trigger, ts, self.p)
            if self.crossed is not None or breach:
                self._sell(ts, p)
        else:
            self._chase(ts)

    def _levels(self, ts: int, p: Protection) -> None:
        assert self.bid is not None and self.ask is not None
        spread = self.ask - self.bid
        buf = policy.buffer(
            self.one_tick,
            spread,
            p.entry,
            self._range(ts),
            self.p,
            range_estimable=self.cfg.range_estimable,
        )
        if buf is not None:
            if p.trigger is None:
                # Never a level in the market (rc8 reject_stop_orders): one at or above the bid is
                # refused and pages unless the bid fell through it. That needs the entry bid above
                # it, so the trigger started below the market (ADR-0002:14; matching.rs:426-437
                # matches a SELL stop at market <= stop), and the bid now at or below it, as
                # northstar counts a stop crossed (exit_manager/strategy.py:1731; dsh, #231).
                level = p.entry - buf
                fell = p.entry_bid is not None and p.entry_bid > level
                if level >= self.bid and not fell:  # (#210, M1)
                    return self._notify(ts, "trigger_in_market")
                p.trigger, p.setup_gap = level, buf
                self._record(ts, "initial_trigger", trigger=p.trigger, buffer=buf)
            assert p.setup_gap is not None
            floor = policy.noise_floor(self.one_tick, spread)
            lock = policy.gain_lock(p.entry, self.c_rt, p.high, floor, self.p)
            if not self.arming.armed:
                armed_trigger = policy.next_trigger(p.trigger, p.high, buf, lock)
                executable = self.bid if self.rth else self.last
                if executable is not None and self.arming.try_arm(executable, armed_trigger):
                    self._record(ts, "armed", gain_lock=lock)
            trail = buf if self.arming.armed else max(buf, p.setup_gap)
            # X2's legs, each judged alone: a leg into the bid, now or when H was made, is
            # dropped (rc8's modify rule, #210), so a lock below the bid still raises (M3). With
            # no bid known at H (a fill or a B adoption before the first quote), a leg is judged
            # against the current bid, as rc8 keeps the old stop when a modify would put it in
            # the market (crates/execution/src/matching_engine/mod.rs:6774-6812; dsh, #231).
            cap = self.bid if p.high_bid is None else max(self.bid, p.high_bid)
            legs = [p.high - trail] + ([lock] if self.arming.armed else [])
            p.trigger = max([p.trigger] + [leg for leg in legs if leg < cap])
            p.take_profit, p.tp_anchor = policy.take_profit(
                p.take_profit, p.tp_anchor, p.high, self.p
            )
            p.backstop = policy.backstop(p.backstop, p.trigger, self.bid, self.p)

    def _sell(self, ts: int, p: Protection) -> None:
        """One managed LIMIT for the whole native position: one exit per quantity (ADR-0007:19)."""
        assert self.bid is not None and p.trigger is not None
        if self.position_id is None:  # flat while the crossed entry is unfinished (F2)
            return
        if self.crossed is None:  # retained intent: cancel the house's orders first (ADR-0005:36)
            # The wait starts on the engine's clock, as northstar's does: `_PartialEntryWait(...,
            # s._now())`, then `s._now() - wait.started_ns` (strategies/alpaca.py:942, :991).
            self.crossed = now = self.clock.timestamp_ns()
            self._record(now, "crossed", trigger=p.trigger, armed=str(self.arming.armed))
            self.clock.set_time_alert_ns(
                f"deadline-{now}", now + CANCEL_DEADLINE_NS, self._deadline
            )
        if self._foreign(ts) or self._cancel_house():  # cancel the house's own orders first
            return
        if not self._quote_ok():  # never price the exit from a stale or one-sided quote (#211)
            self._notify(ts, "awaiting_quote")
            return
        position = self.cache.position(self.position_id)
        assert position is not None and position.is_open
        # With either chase row unavailable (#98) the fast sell rests at the bid: chase off.
        last = self.last if self.p.chase_on else None
        order = self.order_factory.limit(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL,
            quantity=position.quantity,
            price=self._price(policy.chase_limit(self.bid, last, self.p)),
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
        )
        self.sell_id = order.client_order_id
        self.exits.add(order.client_order_id)
        self.amended_ns = ts
        self.submit_order(order)
        kind = "breach_sell_gain_lock" if self.arming.armed else "breach_sell_prearm"
        qty = position.quantity.as_decimal()
        self._record(ts, kind, trigger=p.trigger, limit=order.price.as_decimal(), qty=qty)
        if not self.p.chase_on:  # the sell rests at the breach bid and never follows it (#211)
            self._notify(ts, "chase_unavailable")

    def _chase(self, ts: int) -> None:
        if self.sell_id is None or not self.p.chase_on:
            return
        assert self.p.chase_min_interval_s is not None
        order = self.cache.order(self.sell_id)
        interval = int(self.p.chase_min_interval_s * NS)
        if order is None or not order.is_open or order.is_pending_update or self.bid is None:
            return
        if ts - self.amended_ns < interval:
            return
        if not self._quote_ok():  # never reprice from a stale or one-sided quote (#211)
            self._notify(ts, "awaiting_quote")
            return
        price = self._price(policy.chase_limit(self.bid, self.last, self.p))
        if price != order.price:
            self.amended_ns = ts
            self.modify_order(order.client_order_id, price=price)
            self._record(ts, "chase", limit=price.as_decimal())

    def _quote_ok(self) -> bool:
        """ADR-0005:27 at its pin, northstar `quote_fresh` (exit_manager/strategy.py:2165-2173):
        0 < bid <= ask, so a locked quote is valid and a crossed one is not (:2171), and the age
        on the engine clock, 0 <= now - ts_event <= `quote_max_age_s` (`_now()`, :2166-2169),
        never on the quote's own stamp, which made every age 0 (#231 r4 R2)."""
        assert self.bid is not None and self.ask is not None
        age = self.clock.timestamp_ns() - self.quote_ns
        fresh = 0 <= age <= int(self.p.quote_max_age_s * NS)
        return self.bid > 0 and self.bid <= self.ask and fresh

    def _fresh_bid(self, at: int | None = None) -> Decimal | None:
        """The bid for an anchor or a judgment, only when its quote passes `_quote_ok`, and for an
        anchor made at an observation stamped `at`, only from a quote that existed then,
        quote_ns <= at (#266): a later quote's bid did not exist when H was made. Else None,
        unknown, which each path already handles (#264). northstar reads no quote it has not
        checked fresh (exit_manager/strategy.py:2165-2173, :1820-1822), and orders quote and
        observation stamps before it judges (:1826-1827)."""
        if self.bid is None or self.ask is None or not self._quote_ok():
            return None
        if at is not None and self.quote_ns > at:
            return None
        return self.bid

    def _range(self, ts: int) -> Decimal | None:
        start = ts - int(self.p.prearm_range_window_s * NS)
        while self.trades and self.trades[0][0] < start:
            self.trades.popleft()
        if not self.trades:
            return None
        prices = [price for _, price in self.trades]
        return max(prices) - min(prices)

    def _held(self) -> tuple[Decimal, Decimal]:
        """The protected native position's quantity and Decimal basis, from the cache."""
        assert self.position_id is not None
        position = self.cache.position(self.position_id)
        assert position is not None and isinstance(position.avg_px_open, Decimal)
        return position.quantity.as_decimal(), position.avg_px_open

    def _session(self, ts: int) -> None:
        """An equity crossing an XNYS boundary between RTH and the extended sessions switches the
        breach input (bids in RTH, trades outside it) and hands the exit over. The session is read
        from the engine's clock, never a late event's stamp (O6), once per minute, since every
        XNYS boundary falls on a whole minute (sextant.paper.sessions.session_at, O7)."""
        now = self.clock.timestamp_ns()
        if not self.equity or now // MINUTE_NS == self.minute:
            return
        self.minute = now // MINUTE_NS
        before, self.xnys = self.xnys, session_at(now)
        if before not in (None, self.xnys) and (self.xnys is Session.RTH) != self.rth:
            self.rth, self.breach = self.xnys is Session.RTH, BreachRun()
            self._handover(ts)

    def _handover(self, ts: int) -> None:
        """Terminal cancel precedes handover (ADR-0005, venue limits): the sell, in flight or
        resting, is canceled (C3), and its terminal cancel re-sends the retained crossing in the
        new session at once (on_order_canceled, O1). A sell the house already canceled, as a
        freeze does, keeps that one intent: no second request, so its request time and its K+1
        budget stand (#231 GPT delta gate D1). Northstar's throttle refuses a repeat before it
        records one (strategies/base.py:1728-1754), and rc8 sends nothing for an order already
        pending cancel (crates/trading/src/strategy/mod.rs:818-828)."""
        order = None if self.sell_id is None else self.cache.order(self.sell_id)
        cid = None if order is None or order.is_closed else order.client_order_id
        if cid is not None and cid not in self.canceled:
            self.canceled.add(cid)
            self._cancel(cid)
        self.sell_id = None

    def _working(self) -> list:
        """Every order on the symbol not yet finished, held locally or at the venue: rc8 "test for a
        finished order with `is_closed`" (v2.0.0rc8 docs/concepts/orders/index.md:55-59)."""
        return [o for o in self.cache.orders(instrument_id=self.instrument_id) if not o.is_closed]

    def _foreign(self, ts: int) -> bool:
        """ADR-0007:19 (#212, A2): a working order the house didn't send freezes and pages."""
        if any(o.client_order_id not in self.house for o in self._working()):
            self._pause_and_page(ts, "foreign_order")
        return self.paused

    def _cancel_house(self) -> list:
        """Cancel each unfinished house order once (ADR-0005:36); return those still working."""
        working = [o for o in self._working() if o.client_order_id in self.house]
        for order in (o for o in working if o.client_order_id not in self.canceled):
            self.canceled.add(order.client_order_id)
            self._cancel(order.client_order_id)
        return working

    def _flat(self, ts: int, opener: ClientOrderId | None = None) -> None:
        """ADR-0007:21: no open position on the symbol and every order terminal is a verified flat.
        It retires a crossing kept through a close once that cycle's last reservation ends (D1),
        and clears a pause (#171). It is checked on each quote and terminal order event, and for
        a house opener against everything but that opener's own order (F3)."""
        kept = self.crossed is not None and self.position_id is None and self.protection is not None
        if not (self.paused or kept) or (self.protection is not None and not kept):
            return
        if not [o for o in self._working() if o.client_order_id != opener]:
            positions = self.cache.positions_open(instrument_id=self.instrument_id)
            if len(positions) == (opener is not None):
                if kept:  # the kept cycle ended flat: nothing is owed any more (D1)
                    self.protection = self.entry_order_id = None
                    self.crossed, self.arming, self.breach = None, Bar1mArming(), BreachRun()
                    self.notified = set()
                if self.paused:
                    self.paused = False
                    self._record(ts, "pause_cleared")

    def _pause_and_page(self, ts: int, reason: str, **values: Decimal) -> None:
        """ADR-0007:17-19: stop managing the symbol, cancel the house's own unfinished orders (a
        resting sell the venue would resize over an owner add, #170; an entry's remainder, O2),
        and page a person. Each pause is recorded."""
        self.paused = True
        self._cancel_house()
        kind = "add_paused" if reason == "owner_add" else "pause_and_page"
        if reason in self.notified:  # paged already in this episode
            self._record(ts, kind, reason=reason, **values)
        self._notify(ts, reason, kind, **values)

    def _notify(self, ts: int, reason: str, kind: str = "page", **values: Decimal) -> None:
        """Page a person once per reason in each position cycle: an ERROR log line, as
        northstar-trading@8256c16c exit_manager/strategy.py:1307 logs an unprotected holding, and
        the event record."""
        if reason not in self.notified:
            self.notified.add(reason)
            self.pages.append(reason)
            self.log.error(f"{kind}: {reason} on {self.instrument_id}")
            self._record(ts, kind, reason=reason, **values)

    def _price(self, value: Decimal) -> Price:
        """Round a sell limit up to the tick, so it never lands below the bid."""
        ticks = (value / self.one_tick).to_integral_value(rounding=ROUND_CEILING)
        return Price.from_str(str(ticks * self.one_tick))

    def _record(self, ts: int, event: str, **values: Decimal | str) -> None:
        self.events.append({"ts": str(ts), "event": event} | {k: str(v) for k, v in values.items()})

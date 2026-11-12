"""ADR-0004 N1/N2 entry state; late prints only refuse, under the manifest's owner exception."""

from collections import deque, namedtuple
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum

from nautilus_trader.model import Bar, BarType, InstrumentId, Price, TradeTick

from sextant.entry.indicators import DecimalATR
from sextant.entry.parameters import EntryParameters, number


class Extension(StrEnum):
    UNKNOWN = "UNKNOWN"
    NOT_EXTENDED = "NOT_EXTENDED"
    EXTENDED = "EXTENDED"


Surge = namedtuple(
    "Surge", "instrument_id segment first_print_ts flag_ts known_at expires_ts quiet_price source"
)
TradeCoverage = namedtuple(
    "TradeCoverage",
    "instrument_id segment start_ns end_ns known_at source counts",
    defaults=(None,),
)


def classify(price, level, atr, multiple) -> Extension:
    if level is None or atr is None or not atr.is_finite() or atr <= 0:
        return Extension.UNKNOWN
    return Extension.EXTENDED if price - level > multiple * atr else Extension.NOT_EXTENDED


class _Reclaim:
    def __init__(self, params: EntryParameters):
        self.params = params
        self.episode_started: int | None = None
        self.touch: tuple[int, Decimal] | None = None
        self._clear_retest()

    def _clear_retest(self) -> None:
        self.reclaim_count = self.reclaim_started = 0
        self.confirmed = False
        self.high: Decimal | None = None
        self.trough: Decimal | None = None
        self.hold_started: int | None = None
        self.reclaim_event_ns: int | None = None
        self.reclaim_consumed_price: Decimal | None = None

    def _reclaim(self, tick, level, extension) -> None:
        price, at, p = tick.price.as_decimal(), tick.ts_event, self.params
        self.reclaim_event_ns = None
        if extension is Extension.UNKNOWN or level is None:
            self.touch = None
            self._clear_retest()
            return
        if extension is Extension.EXTENDED:
            if self.episode_started is None:
                self.episode_started, self.touch = at, None
                self._clear_retest()
            return
        if self.episode_started is not None:
            if price <= level and at > self.episode_started:
                self.touch, self.episode_started = (at, price), None
                self._clear_retest()
            return
        if self.touch is None:
            return
        if at - self.touch[0] > p.ns("reclaim_lookback_s") or price < level:
            self.touch = None
            self._clear_retest()
            return
        if price == level and not self.confirmed:
            self._clear_retest()
            return
        if not self.confirmed:
            self.reclaim_started = self.reclaim_started if self.reclaim_count else at
            self.reclaim_count += 1
            self.high = max(self.high or price, price)
            self.confirmed = self.reclaim_count >= p[
                "reclaim_trades_count"
            ] and at - self.reclaim_started >= p.ns("reclaim_trades_span_s")
            return
        assert self.high is not None
        consumed, low, held = self.reclaim_consumed_price, self.trough, self.hold_started
        if consumed is not None and low is None and price >= consumed:
            self.high = max(self.high, price)
            return
        if low is not None and held is not None:
            if (
                price > low
                and at - held >= p.ns("reclaim_hold_s")
                and low >= level
                and low > self.touch[1]
            ):
                self.reclaim_event_ns = at
                self.reclaim_consumed_price = price
                self.trough, self.hold_started = None, None
                self.high = max(self.high, price)
                return
        if price > self.high:
            self.high, self.trough, self.hold_started = price, None, None
        elif price < self.high and (self.trough is None or price < self.trough):
            self.trough, self.hold_started = price, at


class _Setup(_Reclaim):
    def __init__(self, params):
        super().__init__(params)
        self.surge: Surge | None = None
        self.run, self.extension = (), Extension.UNKNOWN
        self.last: TradeTick | None = None
        self.identities, self.seen = set(), deque()  # upstream trade()'s retained identities
        self.retain = max(params.ns("first_leg_window_s"), params.ns("first_leg_span_s"))
        self.previous = self.vwap = self.last_atr = None
        self.dollars, self.volume, self.trusted, self.floor = Decimal(0), Decimal(0), True, -1
        flows = tuple(
            name for name in ("flow_share_window_s", "covered_flow_window_s") if params.has(name)
        )
        self.windows = {params.ns(name): (deque(), Decimal(0)) for name in flows}
        if params.has("covered_flow_baseline_min"):
            self.windows[params.ns("covered_flow_baseline_min") * 60] = deque(), Decimal(0)

    def window(self, duration, now):
        window, dollars = self.windows[duration]
        while window and window[0][0] <= now - duration:
            dollars -= window.popleft()[1]
        self.windows[duration] = window, dollars
        return len(window), dollars

    def advance(self, tick, atr) -> None:
        price, size = tick.price.as_decimal(), tick.size.as_decimal()
        self.dollars += price * size
        self.volume += size
        self.vwap = self.dollars / self.volume if self.trusted else None
        item = tick.ts_event, price * size
        for duration, (window, dollars) in self.windows.items():
            window.append(item)
            self.windows[duration] = window, dollars + item[1]
            self.window(duration, tick.ts_event)
        self.last_atr = atr
        flag = self.surge
        if flag is not None:
            in_window = (
                flag.first_print_ts
                <= tick.ts_event
                <= min(flag.expires_ts, flag.flag_ts + self.params.ns("first_leg_window_s"))
                and price > flag.quiet_price
                and tick.ts_event > self.floor  # #88's drop floor: never across a dropped print
            )
            if not in_window or (self.previous is not None and price < self.previous):
                self.run = ()
            if in_window:
                self.run += (tick,)
                if len(self.run) > self.params["first_leg_trades_count"] + 1:
                    self.run = self.run[:1] + self.run[2:]
        self.previous = price
        if self.params.has("entry_max_vwap_extension_atr"):  # N1:46: the first leg is exempt
            max_extension = self.params["entry_max_vwap_extension_atr"]
            self.extension = classify(price, self.vwap, atr, max_extension)
            self._reclaim(tick, self.vwap, self.extension)


class EntryState:
    def __init__(
        self, instrument_id: InstrumentId, params: EntryParameters, segment: str, *, start_ns: int
    ):
        self.instrument_id, self.params = instrument_id, params
        self.atr = DecimalATR(params["atr_period"])
        self.bar_type = BarType.from_str(
            f"{instrument_id}-{params['atr_bar_minutes']}-MINUTE-LAST-EXTERNAL"
        )
        self.last_bar_ns, self.atr_at, self.atr_prev_at, self.atr_before = -1, -1, -1, None
        self.setup = _Setup(params)
        self.reset_segment(segment, start_ns=start_ns)

    def __getattr__(self, name):
        return getattr(self.setup, name)

    def reset_segment(self, segment: str, *, start_ns: int) -> None:
        if not segment or type(start_ns) is not int or start_ns < 0:
            raise ValueError("segment identity and event-time start required")
        try:
            setup = _Setup(self.params)
        except Exception as exc:
            self.failure = f"{type(exc).__name__}: {exc}"
            return
        self.segment, self.segment_start_ns = segment, start_ns
        self.setup, self.failure, self.void_reason = setup, None, None

    def arm(self, surge: Surge) -> None:
        if (
            surge.instrument_id != self.instrument_id
            or surge.segment != self.segment
            or not self.segment_start_ns
            <= surge.first_print_ts
            <= surge.flag_ts
            <= surge.known_at
            < surge.expires_ts
            or not surge.source
            or not isinstance(surge.quiet_price, Decimal | Price)
        ):
            raise ValueError("invalid surge identity, segment, clocks, source or quiet price")
        surge = surge._replace(quiet_price=number(Decimal(str(surge.quiet_price)), "quiet_price"))
        if self.surge is None and self.failure is None:
            self.setup.surge, self.setup.run = surge, ()
            self.setup.reclaim_event_ns = None

    def on_bar(self, bar: Bar, *, now: int) -> bool:
        if (
            self.failure
            or bar.bar_type != self.bar_type
            or not self.last_bar_ns < bar.ts_event <= bar.ts_init <= now
        ):
            return False
        if now > self.atr_at:
            self.atr_prev_at, self.atr_before, self.atr_at = self.atr_at, self.atr.value, now
        self.atr.handle_bar(bar)
        self.last_bar_ns = bar.ts_event
        return True

    def on_trade(self, tick: TradeTick, *, now: int) -> bool:
        self.void_reason = None
        if (
            self.failure
            or tick.instrument_id != self.instrument_id
            or not self.segment_start_ns <= tick.ts_event
            or tick.price.as_decimal() <= 0
            or tick.size.as_decimal() <= 0
        ):
            return False
        try:
            setup, last_ns = self.setup, -1 if self.last is None else self.last.ts_event
            identity = str(tick.trade_id), tick.ts_event, tick.price.raw, tick.size.raw
            if identity in setup.identities:  # a retained duplicate is already in VWAP
                return False
            # Behind the prints or two bar groups (no as-of ATR), skewed, future or stale: dropped.
            # Owner exception, refusal only: VWAP stays untrusted (N2 UNKNOWN) to segment end.
            late = tick.ts_event < last_ns or tick.ts_event <= self.atr_prev_at
            late = late or not tick.ts_event <= tick.ts_init <= now
            if late or now - tick.ts_event > self.params.ns("max_data_age_s"):
                restart = bool(self.run) and self.run[0].ts_event <= tick.ts_event
                if restart or setup.trusted:
                    self.void_reason = "late print: VWAP untrusted" + "; run restarted" * restart
                setup.trusted, setup.vwap, setup.extension = False, None, Extension.UNKNOWN
                setup.run = () if restart else setup.run
                setup.floor = max(setup.floor, tick.ts_event)  # #88: refuse moves the watermark
                return False
            while setup.seen and setup.seen[0][0] < tick.ts_event - setup.retain:
                setup.identities.discard(setup.seen.popleft()[1])
            # Strict as-of: one bar group known since the print's event time leaves atr_before.
            atr = self.atr.value if tick.ts_event > self.atr_at else self.atr_before
            setup.advance(tick, atr)
            setup.last = tick
            setup.identities.add(identity)
            setup.seen.append((tick.ts_event, identity))
            return True
        except Exception as exc:
            self.failure = f"{type(exc).__name__}: {exc}"
            return False

    def flow_dollars(self, now):
        return self.setup.window(self.params.ns("flow_share_window_s"), now)[1]

    def fresh(self, now: int) -> bool:
        return (
            not self.failure
            and self.last is not None
            and self.last.ts_init <= now
            and now - self.last.ts_event <= self.params.ns("max_data_age_s")
        )

    def first_leg_ready(self, ask: Decimal, now: int) -> bool:
        flag, p, run = self.surge, self.params, self.run
        return bool(
            flag
            and self.fresh(now)
            and ask.is_finite()
            and ask >= flag.quiet_price
            and flag.known_at <= now < flag.expires_ts
            and now - flag.flag_ts <= p.ns("first_leg_window_s")
            and len(run) >= p["first_leg_trades_count"]
            and run[-1].ts_event - run[0].ts_event >= p.ns("first_leg_span_s")
            and max(ask, run[-1].price.as_decimal())
            <= flag.quiet_price * (1 + p["first_leg_max_move"])
        )

    def reclaim_ready(self, ask: Decimal, buffer: Decimal, now: int, *, covered_flow=False) -> bool:
        return bool(
            covered_flow
            and self.fresh(now)
            and self.last is not None
            and self.reclaim_event_ns == self.last.ts_event
            and self.vwap is not None
            and ask.is_finite()
            and buffer.is_finite()
            and buffer > 0
            and self.vwap <= ask <= self.vwap + self.params["reclaim_max_above_buffers"] * buffer
        )

    def covered_flow(self, now: int, coverage: TradeCoverage | None) -> bool:
        if not (
            self.params.has("covered_flow_window_s")
            and self.params.has("covered_flow_baseline_min")
        ):  # a narrow mode never reads the flow rows
            return False
        baseline_ns = self.params.ns("covered_flow_baseline_min") * 60
        window_ns = self.params.ns("covered_flow_window_s")
        if (
            coverage is None
            or coverage.instrument_id != self.instrument_id
            or coverage.segment != self.segment
            or not coverage.source
            or not isinstance(coverage.counts, Mapping)
            or not 0 <= coverage.start_ns <= now - max(baseline_ns, window_ns)
            or not now <= coverage.end_ns <= coverage.known_at <= now
        ):
            return False
        baseline = coverage.counts.get(baseline_ns)
        recent = coverage.counts.get(window_ns)
        observed_baseline = self.setup.window(baseline_ns, now)[0]
        observed_recent = self.setup.window(window_ns, now)[0]
        return bool(
            type(baseline) is int
            and type(recent) is int
            and baseline >= observed_baseline > 0
            and recent >= observed_recent >= 0
            and (window_ns >= baseline_ns or recent <= baseline)
            and (window_ns <= baseline_ns or recent >= baseline)
            and recent * baseline_ns >= baseline * window_ns * self.params["covered_flow_ratio"]
            and observed_recent * baseline_ns
            >= observed_baseline * window_ns * self.params["covered_flow_ratio"]
        )

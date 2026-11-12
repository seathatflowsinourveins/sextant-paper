"""The exit engine's long-side policy, X2-X6 of ADR-0005, as pure Decimal functions.

Source: ADR-0005 Decision, harvested from
northstar-lanes@00203275944a:docs/adr/0006-exit-engine.md X2-X7. That record says no upstream
combines this policy; the owner approved it as house policy.
The Decimal-only, pure-function shape follows the nearest carried code,
northstar-trading@8256c16c31dc:src/northstar_trading/strategies/exit_manager/rules.py. The order
lifecycle stays in Nautilus (see engine.py).

Every parameter is an UNCALIBRATED registry row (REGISTRY below) with its recorded start and
source. ExitParams has no defaults: a caller takes `ExitParams.starts()` or names every value. A row
with no recorded start stays unavailable (AGENTS.md Rule 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal

ZERO = Decimal(0)
ONE = Decimal(1)
# The run's observations are evidence, not state: keep the completing tail of a fast tape.
MAX_BREACH_OBSERVATIONS = 64

ADR = "docs/adr/0005-exit-engine.md"


@dataclass(frozen=True)
class Row:
    name: str
    unit: str
    start: Decimal | None
    source: str
    state: str = "UNCALIBRATED"


T19 = (
    "northstar-trading@7c5f852a2816bbaa5896241a93bab9de7cf7bb4f:"
    "docs/adr/0019-managed-exits-and-ranked-reentry.md"
)
LANES = "northstar-lanes@00203275944a22655b3e14b221cfde9a265d549a:src/northstar_lanes/parameters.py"

# Paper starts as recorded at their sources; a start is never measured success (Rule 2).
REGISTRY: tuple[Row, ...] = (
    Row("prearm_buffer_fraction", "fraction of entry", Decimal("0.01"), f"{T19}:251"),
    Row("prearm_range_multiple", "multiple of R_w", Decimal(1), f"{T19}:252"),
    Row("prearm_range_window_s", "seconds", Decimal(10), f"{T19}:253"),
    Row("gain_lock_giveback_fraction", "fraction of H-entry", Decimal("0.1"), f"{T19}:140"),
    Row("breach_persist_count", "observations", Decimal(3), f"{T19}:100"),
    Row("breach_persist_span_s", "seconds", Decimal(1), f"{T19}:100"),
    Row(
        "chase_cap_fraction",
        "fraction of last",
        None,
        "ADR-0019:296 (trading@7c5f852a) implies 0.01, which is not a recorded start: no "
        "authority; X5 calibrates on tick replay after #130 (lead ruling, #98)",
    ),
    Row("chase_min_interval_s", "seconds", None, f"{ADR} X5; no value at either pin (#98)"),
    Row("take_profit_above_h_fraction", "fraction of H", Decimal("0.10"), f"{ADR} X5"),
    Row("take_profit_step_fraction", "fraction of H", Decimal("0.02"), f"{ADR} X5"),
    Row("backstop_min_separation_fraction", "fraction", Decimal("0.05"), f"{T19}:107"),
    Row("quote_max_age_s", "seconds (RTH; 2 outside)", Decimal(1), f"{LANES}:15"),
)


CHASE = ("chase_cap_fraction", "chase_min_interval_s")


@dataclass(frozen=True)
class ExitParams:
    """One value per REGISTRY row, all required. Test fixture values are not starts."""

    prearm_buffer_fraction: Decimal
    prearm_range_multiple: Decimal
    prearm_range_window_s: Decimal
    gain_lock_giveback_fraction: Decimal
    breach_persist_count: int
    breach_persist_span_s: Decimal
    chase_cap_fraction: Decimal | None  # None: unavailable, so the chase is off
    chase_min_interval_s: Decimal | None  # None: unavailable, so the chase is off
    take_profit_above_h_fraction: Decimal
    take_profit_step_fraction: Decimal
    backstop_min_separation_fraction: Decimal
    quote_max_age_s: Decimal

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in CHASE and value is None:
                continue
            if f.name == "breach_persist_count":
                if not isinstance(value, int) or value < 1:
                    raise ValueError("breach_persist_count must be an int >= 1")
            elif not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{f.name} must be a finite nonnegative Decimal")
        for name in ("gain_lock_giveback_fraction", "chase_cap_fraction"):
            if getattr(self, name) is not None and getattr(self, name) >= 1:
                raise ValueError(f"{name} must be below 1")
        if self.take_profit_above_h_fraction <= self.take_profit_step_fraction:
            raise ValueError("take_profit_above_h_fraction must exceed take_profit_step_fraction")

    @property
    def chase_on(self) -> bool:
        """X5's chase runs only when both its rows have a value (#98)."""
        return self.chase_cap_fraction is not None and self.chase_min_interval_s is not None

    @classmethod
    def starts(cls) -> ExitParams:
        """Every row at its recorded start. A row without one stays unavailable (None)."""
        values: dict[str, object] = {row.name: row.start for row in REGISTRY}
        values["breach_persist_count"] = int(Decimal(str(values["breach_persist_count"])))
        missing = [k for k, v in values.items() if v is None and k not in CHASE]
        if missing:
            raise ValueError(f"no recorded start: {missing}")
        return cls(**values)  # type: ignore[arg-type]


# X2: the trigger.
def noise_floor(one_tick: Decimal, spread: Decimal) -> Decimal:
    return max(one_tick, spread)


def buffer(
    one_tick: Decimal,
    spread: Decimal,
    entry: Decimal,
    range_w: Decimal | None,
    p: ExitParams,
    *,
    range_estimable: bool = True,
) -> Decimal | None:
    """max(one_tick, spread, f*entry, m*R_w). Unknown R_w makes it unknown unless m is zero.

    `range_estimable=False` drops the range term where the data cannot estimate it (minute bars,
    #98 B3); dropping a max term can only tighten the trigger.
    """
    if not range_estimable:
        range_w = None
    elif range_w is None and p.prearm_range_multiple != 0:
        return None
    range_term = ZERO if range_w is None else p.prearm_range_multiple * range_w
    return max(one_tick, spread, p.prearm_buffer_fraction * entry, range_term)


def round_trip_cost(
    entry: Decimal,
    qty: Decimal,
    one_tick: Decimal,
    sec31_rate: Decimal,
    taf_per_share: Decimal,
    taf_cap: Decimal,
) -> Decimal:
    """c_RT per share: entry*sec31 + min(qty*taf, cap)/qty + one tick (dated fee notices)."""
    return entry * sec31_rate + min(qty * taf_per_share, taf_cap) / qty + one_tick


def gain_lock(
    entry: Decimal, c_rt: Decimal, high: Decimal, floor: Decimal, p: ExitParams
) -> Decimal:
    return max(entry + c_rt, high - max(floor, p.gain_lock_giveback_fraction * (high - entry)))


def next_trigger(
    previous: Decimal, high: Decimal, trail: Decimal, armed_lock: Decimal | None
) -> Decimal:
    """max(previous, H - trail, gain_lock if armed). It never loosens."""
    candidates = [previous, high - trail]
    if armed_lock is not None:
        candidates.append(armed_lock)
    return max(candidates)


# X3: arming on completed one-minute bars (the default, `bar_1m`).
@dataclass
class Bar1mArming:
    """Confirmation holds from a completed close >= entry + c_RT + noise_floor until a later one
    closes below. Arm on the first event where it holds and the executable price is strictly above
    the armed trigger. Arming is one-way; it cannot itself breach."""

    confirmed: bool = False
    armed: bool = False

    def on_bar_close(self, close: Decimal, level: Decimal) -> None:
        self.confirmed = close >= level

    def try_arm(self, executable: Decimal, armed_trigger: Decimal) -> bool:
        if not self.armed and self.confirmed and executable > armed_trigger:
            self.armed = True
        return self.armed


# X4: a breach persists over breach_persist_count observations and breach_persist_span_s.
@dataclass
class BreachRun:
    count: int = 0
    first_ns: int | None = None
    # The completing run, so a durable release record can name the prints that fired it.
    seen: list[tuple[int, Decimal]] = field(default_factory=list)

    def observe(self, price: Decimal, trigger: Decimal, ts_ns: int, p: ExitParams) -> bool:
        if price > trigger:
            self.count, self.first_ns, self.seen = 0, None, []
            return False
        if self.first_ns is None:
            self.first_ns = ts_ns
        self.count += 1
        self.seen.append((ts_ns, price))
        if len(self.seen) > MAX_BREACH_OBSERVATIONS:
            del self.seen[0]
        span_ns = int(p.breach_persist_span_s * 1_000_000_000)
        return self.count >= p.breach_persist_count and ts_ns - self.first_ns >= span_ns


# X5: the fast sell's limit, and the take-profit that rolls with H before a breach.
def chase_limit(bid: Decimal, last: Decimal | None, p: ExitParams) -> Decimal:
    """max(bid, last*(1-cap)): never below the bid. With the chase off, the bid itself."""
    if last is None or not p.chase_on:
        return bid
    assert p.chase_cap_fraction is not None
    return max(bid, last * (ONE - p.chase_cap_fraction))


def take_profit(
    previous: Decimal | None, anchor: Decimal | None, high: Decimal, p: ExitParams
) -> tuple[Decimal, Decimal]:
    """Returns (limit, anchor H). It rolls up once H rises step above the anchor, never down."""
    if previous is None or anchor is None:
        return high * (ONE + p.take_profit_above_h_fraction), high
    if high >= anchor * (ONE + p.take_profit_step_fraction):
        return max(previous, high * (ONE + p.take_profit_above_h_fraction)), high
    return previous, anchor


# X6: the venue backstop is raised in place, never lowered.
def backstop(previous: Decimal | None, trigger: Decimal, bid: Decimal, p: ExitParams) -> Decimal:
    level = min(trigger, bid) * (ONE - p.backstop_min_separation_fraction)
    return level if previous is None else max(previous, level)

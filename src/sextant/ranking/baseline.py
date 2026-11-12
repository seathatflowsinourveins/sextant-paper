"""Pure time-matched historical dollar-volume and trade-count curves.

Minute bars approximate within-minute flow by linear interpolation, explicitly;
they do not contain seconds. No full-session total is used as a time-matched
median. Window queries take a window within EACH historical session, then its
median (a difference of medians is generally not the median of differences).

The builder turns vendor minute bars plus explicit completeness receipts into
those curves; a missing receipt is a refusal, never a zero.
"""

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from statistics import median
from types import MappingProxyType
from typing import TYPE_CHECKING

from exchange_calendars import ExchangeCalendar, get_calendar
from nautilus_trader.core.datetime import dt_to_unix_nanos

from sextant.paper.sessions import ET, Session, session_at

if TYPE_CHECKING:
    from sextant.ranking.engine import Segment

ZERO = Decimal(0)
NS = 1_000_000_000
MINUTE_NS = 60 * NS
_PROVENANCE_SUFFIX = (
    "; v*vw, n; explicit complete coverage; linear within minute; first POST minute omitted"
)
_BASELINE_KEYS = frozenset({"samples", "provenance", "known_at", "feed"})
_SAMPLE_KEYS = frozenset(
    {"symbol", "segment", "session", "offset_s", "dollar_volume", "trade_count"}
)


@dataclass(frozen=True, slots=True)
class MinuteBarRow:
    """One vendor minute bar, reduced to the fields a baseline needs."""

    symbol: str
    ts_event: int
    volume: int
    vwap: Decimal
    trade_count: int

    def __post_init__(self) -> None:
        if type(self.ts_event) is not int:
            raise TypeError("bar timestamps must be integer nanoseconds")
        if self.ts_event < 0:
            raise ValueError("negative bar timestamp")
        if self.ts_event % MINUTE_NS:
            raise ValueError("bar timestamps must be minute-aligned")
        if type(self.volume) is not int:
            raise TypeError("bar volume must be an integer")
        if self.volume < 0:
            raise ValueError("negative bar volume")
        if not isinstance(self.vwap, Decimal):
            raise TypeError("bar vwap must be Decimal")
        if not self.vwap.is_finite() or self.vwap < 0:
            raise ValueError("invalid bar vwap")
        if type(self.trade_count) is not int:
            raise TypeError("bar trade count must be an integer")
        if self.trade_count < 0:
            raise ValueError("negative bar trade count")


@dataclass(frozen=True, slots=True)
class BaselineCoverage:
    """A caller's receipt that every bar in ``[start_ts, end_ts)`` arrived."""

    symbol: str
    start_ts: int
    end_ts: int

    def __post_init__(self) -> None:
        if type(self.start_ts) is not int or type(self.end_ts) is not int:
            raise TypeError("coverage timestamps must be integer nanoseconds")
        if self.end_ts <= self.start_ts:
            raise ValueError("empty or reversed baseline coverage")


@dataclass(frozen=True, slots=True)
class BaselineSample:
    symbol: str
    segment: str
    session: str
    offset_s: int
    dollar_volume: Decimal
    trade_count: Decimal

    def __post_init__(self) -> None:
        if self.offset_s < 0:
            raise ValueError("negative baseline offset")
        for value in (self.dollar_volume, self.trade_count):
            if not isinstance(value, Decimal):
                raise TypeError("baseline values must be Decimal")
            if not value.is_finite() or value < 0:
                raise ValueError("invalid baseline value")


@dataclass(frozen=True, slots=True)
class BaselineValue:
    dollar_volume: Decimal
    trade_count: Decimal
    sessions: int
    known_at: int
    provenance: str


@dataclass(frozen=True, slots=True)
class BaselineTable:
    samples: tuple[BaselineSample, ...]
    provenance: str
    known_at: int
    feed: str
    latest_session: date | None = field(init=False)
    _identity: str = field(init=False, repr=False)
    _index: Mapping[tuple[str, str], tuple[tuple[BaselineSample, ...], ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.provenance or self.feed not in {"sip", "boats"}:
            raise ValueError("baseline source and feed required")
        ordered = tuple(
            sorted(self.samples, key=lambda s: (s.symbol, s.segment, s.session, s.offset_s))
        )
        previous: BaselineSample | None = None
        for sample in ordered:
            if previous and (sample.symbol, sample.segment, sample.session) == (
                previous.symbol,
                previous.segment,
                previous.session,
            ):
                if sample.offset_s == previous.offset_s:
                    raise ValueError("duplicate baseline offset")
                if (
                    sample.dollar_volume < previous.dollar_volume
                    or sample.trade_count < previous.trade_count
                ):
                    raise ValueError("cumulative baseline decreases")
            previous = sample
        object.__setattr__(self, "samples", ordered)
        object.__setattr__(
            self,
            "latest_session",
            max(
                (date.fromisoformat(sample.session) for sample in ordered),
                default=None,
            ),
        )
        payload = json.dumps(
            {
                "samples": [asdict(s) for s in ordered],
                "provenance": self.provenance,
                "known_at": self.known_at,
                "feed": self.feed,
            },
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "_identity", hashlib.sha256(payload.encode()).hexdigest())
        grouped: dict[tuple[str, str], dict[str, list[BaselineSample]]] = defaultdict(dict)
        for sample in ordered:
            grouped[sample.symbol, sample.segment].setdefault(sample.session, []).append(sample)
        object.__setattr__(
            self,
            "_index",
            MappingProxyType(
                {
                    key: tuple(tuple(curve) for curve in sessions.values())
                    for key, sessions in grouped.items()
                }
            ),
        )

    def _curves(self, symbol: str, segment: str) -> Iterable[tuple[BaselineSample, ...]]:
        return self._index.get((symbol, segment), ())

    @property
    def identity(self) -> str:
        return self._identity

    @staticmethod
    def _at(curve: tuple[BaselineSample, ...], offset: Decimal) -> tuple[Decimal, Decimal] | None:
        if offset < curve[0].offset_s or offset > curve[-1].offset_s:
            return None  # missing coverage is not a zero, or an extrapolation
        i = bisect_left(curve, offset, key=lambda sample: sample.offset_s)
        upper = curve[i]
        if upper.offset_s == offset:
            return upper.dollar_volume, upper.trade_count
        lower = curve[i - 1]
        fraction = (offset - lower.offset_s) / (upper.offset_s - lower.offset_s)
        return (
            lower.dollar_volume + fraction * (upper.dollar_volume - lower.dollar_volume),
            lower.trade_count + fraction * (upper.trade_count - lower.trade_count),
        )

    def cumulative(
        self, symbol: str, segment: str, offset_s: int | Decimal
    ) -> BaselineValue | None:
        values = [
            value
            for curve in self._curves(symbol, segment)
            if (value := self._at(curve, Decimal(offset_s)))
        ]
        return self._median(values)

    def window(
        self, symbol: str, segment: str, offset_s: int | Decimal, window_s: int
    ) -> BaselineValue | None:
        end = Decimal(offset_s)
        start = end - window_s
        values = []
        for curve in self._curves(symbol, segment):
            a, b = self._at(curve, start), self._at(curve, end)
            if a is not None and b is not None:
                values.append((b[0] - a[0], b[1] - a[1]))
        return self._median(values)

    def _median(self, values: list[tuple[Decimal, Decimal]]) -> BaselineValue | None:
        if not values:
            return None
        return BaselineValue(
            Decimal(median(value[0] for value in values)),
            Decimal(median(value[1] for value in values)),
            len(values),
            self.known_at,
            self.provenance,
        )

    def covers(self, symbol: str, segment: str) -> bool:
        """Whether a slice exists for ``symbol`` in ``segment``.

        A missing slice is not a zero: ``window`` returns ``None`` for it, so
        arming must ask this before spending a slot.
        """
        return bool(self._curves(symbol, segment))

    def sessions(self, symbol: str, segment: str) -> tuple[str, ...]:
        """The baseline's session dates for ``symbol`` and ``segment``, ascending."""
        return tuple(curve[0].session for curve in self._curves(symbol, segment))


def dump_baseline(table: BaselineTable) -> str:
    """Serialize one table to JSON; :func:`load_baseline` is its inverse."""
    return json.dumps(
        {
            "samples": [
                {
                    "symbol": sample.symbol,
                    "segment": sample.segment,
                    "session": sample.session,
                    "offset_s": sample.offset_s,
                    "dollar_volume": str(sample.dollar_volume),
                    "trade_count": str(sample.trade_count),
                }
                for sample in table.samples
            ],
            "provenance": table.provenance,
            "known_at": table.known_at,
            "feed": table.feed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"baseline JSON {field} must be a decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"baseline JSON {field} must be a decimal string") from exc


def _json_sample(entry: object) -> BaselineSample:
    if not isinstance(entry, Mapping) or set(entry) != _SAMPLE_KEYS:
        raise ValueError(f"baseline JSON samples must have {sorted(_SAMPLE_KEYS)}")
    symbol, segment, session = entry["symbol"], entry["segment"], entry["session"]
    if not all(isinstance(value, str) for value in (symbol, segment, session)):
        raise ValueError("baseline JSON symbol, segment and session must be strings")
    offset_s = entry["offset_s"]
    if type(offset_s) is not int:
        raise ValueError("baseline JSON offset must be an integer")
    return BaselineSample(
        symbol,
        segment,
        session,
        offset_s,
        _json_decimal(entry["dollar_volume"], "dollar_volume"),
        _json_decimal(entry["trade_count"], "trade_count"),
    )


def load_baseline(text: str) -> BaselineTable:
    """Read the JSON :func:`dump_baseline` writes; any other shape is refused."""
    payload = json.loads(text)
    if not isinstance(payload, Mapping) or set(payload) != _BASELINE_KEYS:
        raise ValueError(f"baseline JSON must have {sorted(_BASELINE_KEYS)}")
    samples = payload["samples"]
    provenance = payload["provenance"]
    known_at = payload["known_at"]
    feed = payload["feed"]
    if not isinstance(samples, list):
        raise ValueError("baseline JSON samples must be a list")
    if not isinstance(provenance, str):
        raise ValueError("baseline JSON provenance must be a string")
    if type(known_at) is not int:
        raise ValueError("baseline JSON known_at must be an integer")
    if not isinstance(feed, str):
        raise ValueError("baseline JSON feed must be a string")
    return BaselineTable(
        tuple(_json_sample(entry) for entry in samples),
        provenance,
        known_at,
        feed,
    )


def _nanos(value: datetime) -> int:
    return dt_to_unix_nanos(value)


@cache
def _calendar(year: int) -> ExchangeCalendar:
    # Mirrors paper.sessions._calendar so segment labels and closes agree.
    return get_calendar("XNYS", start=date(year - 1, 12, 1), end=date(year + 1, 1, 31), side="left")


def _open_ns(day: date) -> int:
    return int(_calendar(day.year).session_open(day).value)


def _close_ns(day: date) -> int:
    return int(_calendar(day.year).session_close(day).value)


def segment_start(ts_ns: int) -> tuple["Segment | None", date, int]:
    """Label the XNYS segment containing ``ts_ns`` and return its start.

    ``None`` labels every closed moment: ``Segment`` has no closed member, so a
    closed timestamp cannot be a segment. Weekends, holidays and the early-close
    17:00-20:00 gap are closed; the overnight segment belongs to the next
    session and starts at 20:00 ET.
    """
    from sextant.ranking.engine import Segment  # delayed: engine imports this module

    segment = session_at(ts_ns)
    local = datetime.fromtimestamp(ts_ns // NS, tz=ET)
    if segment is Session.CLOSED:
        return None, local.date(), ts_ns

    session_date = (local + timedelta(hours=4)).date()
    if segment is Session.RTH:
        start_ns, end_ns = _open_ns(session_date), _close_ns(session_date)
    elif segment is Session.POST:
        start_ns = _close_ns(session_date)
        end_ns = start_ns + 4 * 3_600 * NS  # 20:00 ET, or 17:00 ET on an early close
    else:
        day = local.date()
        hour = 4 if segment is Session.PRE else 20
        if segment is Session.OVERNIGHT and local.hour < 4:
            day -= timedelta(days=1)
        start_ns = _nanos(datetime.combine(day, time(hour), tzinfo=ET))
        end_ns = _open_ns(session_date) if segment is Session.PRE else start_ns + 8 * 3_600 * NS
    return Segment(segment.name, session_date, start_ns, end_ns, "xnys"), session_date, start_ns


def closed_until(ts_ns: int) -> int:
    """The end of the closed interval holding ``ts_ns``: 20:00 ET the day before the next
    XNYS session. ``session_at`` is CLOSED only on a non-session trading day or in the 17:00 to
    20:00 early-close gap, and its overnight starts at 20:00 the prior day, so every closed
    interval ends there (exchange_calendars ``date_to_session(..., direction="next")``)."""
    local = datetime.fromtimestamp(ts_ns // NS, tz=ET)
    trading_day = (local + timedelta(hours=4)).date()
    following = trading_day + timedelta(days=1)
    session = _calendar(following.year).date_to_session(following, direction="next").date()
    return _nanos(datetime.combine(session - timedelta(days=1), time(20), tzinfo=ET))


def build_baseline(
    bars: Iterable[MinuteBarRow],
    *,
    coverage: Iterable[BaselineCoverage],
    as_of: date,
    known_at: int,
    provenance: str,
    feed: str,
    prior_sessions: int = 20,
) -> BaselineTable:
    """Build the time-matched table from vendor bars plus coverage receipts.

    Receipts are authority: a bar without a receipt, a receipt not complete at
    ``known_at``, or a receipt that does not start at its segment's usable start
    is refused rather than zero-filled. ``feed`` is this repository's addition,
    because ``BaselineTable`` records the vendor feed that supplied it.
    """
    if prior_sessions <= 0:
        raise ValueError("prior_sessions must be positive")
    spans: dict[tuple[str, str, str], tuple[int, int]] = {}
    groups: dict[tuple[str, str, str], list[tuple[int, MinuteBarRow]]] = defaultdict(list)

    for receipt in coverage:
        segment, day, start_ns = segment_start(receipt.start_ts)
        if segment is None or day >= as_of:
            continue
        if receipt.end_ts > known_at:
            raise ValueError("baseline coverage not known at the supplied time")
        if receipt.start_ts % MINUTE_NS or receipt.end_ts % MINUTE_NS:
            raise ValueError("baseline coverage must be minute-aligned")
        if segment_start(receipt.end_ts - 1) != (segment, day, start_ns):
            raise ValueError("baseline coverage crosses a segment boundary")
        first = 60 if segment.name == "POST" else 0
        if receipt.start_ts not in (start_ns, start_ns + first * NS) or (
            receipt.end_ts <= start_ns + first * NS
        ):
            raise ValueError("baseline coverage must begin at the segment's usable start")
        key = (receipt.symbol, segment.name, day.isoformat())
        if key in spans:
            raise ValueError("duplicate baseline coverage")
        spans[key] = (first, (receipt.end_ts - start_ns) // NS)
        groups[key] = []

    for bar in bars:
        segment, day, start_ns = segment_start(bar.ts_event)
        if segment is None or day >= as_of:
            continue
        if bar.ts_event + MINUTE_NS > known_at:
            raise ValueError("baseline contains a bar not known at the supplied time")
        offset_ns = bar.ts_event - start_ns
        if offset_ns % MINUTE_NS:
            raise ValueError("baseline bars must be aligned to minute boundaries")
        offset_s = offset_ns // NS
        if segment.name == "POST" and offset_s == 0:
            continue
        key = (bar.symbol, segment.name, day.isoformat())
        span = spans.get(key)
        if span is None or not (span[0] <= offset_s and offset_s + 60 <= span[1]):
            raise ValueError("vendor minute bar outside declared coverage")
        if bar.volume < 0 or bar.vwap < 0 or bar.trade_count < 0:
            raise ValueError("negative vendor minute-bar value")
        groups[key].append((offset_s, bar))

    latest: dict[tuple[str, str], list[str]] = defaultdict(list)
    for symbol, segment_name, day_iso in groups:
        latest[(symbol, segment_name)].append(day_iso)

    samples: list[BaselineSample] = []
    for key in sorted(groups):
        symbol, segment_name, day_iso = key
        if day_iso not in sorted(latest[(symbol, segment_name)])[-prior_sessions:]:
            continue
        first, end_offset = spans[key]
        dollar_volume = ZERO
        trade_count = ZERO
        seen: set[int] = set()
        samples.append(
            BaselineSample(symbol, segment_name, day_iso, first, dollar_volume, trade_count)
        )
        for offset_s, bar in sorted(groups[key], key=lambda item: item[0]):
            if offset_s in seen:
                raise ValueError("duplicate vendor minute bar")
            seen.add(offset_s)
            samples.append(
                BaselineSample(symbol, segment_name, day_iso, offset_s, dollar_volume, trade_count)
            )
            dollar_volume += bar.volume * bar.vwap
            trade_count += bar.trade_count
            samples.append(
                BaselineSample(
                    symbol,
                    segment_name,
                    day_iso,
                    offset_s + 60,
                    dollar_volume,
                    trade_count,
                )
            )
        samples.append(
            BaselineSample(symbol, segment_name, day_iso, end_offset, dollar_volume, trade_count)
        )

    unique = {
        (sample.symbol, sample.segment, sample.session, sample.offset_s): sample
        for sample in samples
    }
    return BaselineTable(
        tuple(unique.values()),
        provenance + _PROVENANCE_SUFFIX,
        known_at,
        feed,
    )

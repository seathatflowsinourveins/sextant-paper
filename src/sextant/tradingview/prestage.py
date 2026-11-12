"""Keep the newest complete display unit fresh for the current display segment.

This is a REST-only, display-only producer: it replays the current PRE, RTH or
POST session's completed one-minute SIP bars through the bar-fidelity host, then
projects the ranked J5 document with the bridge into a research directory that
``python -m sextant.tradingview --loop`` consumes.  It opens no stream, calls no
TradingView endpoint, and places no order.

Source: the merged ``host.py``, ``bridge.py``, ``producer.py`` and
``baseline.py`` (alpaca-py 0.44.0 REST bars, ADR-0003, northstar-lanes
registry). The producer supplies the operating clock and the slot-2 lease; it
adds no ranking, projection or clock rule of its own.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import time
from array import array
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from exchange_calendars import get_calendar
from nautilus_trader.common import MessageBus
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model import TraderId

from sextant.data.alpaca_history import Pacer, decimal_value
from sextant.execution.state import write_atomic
from sextant.ranking.baseline import BaselineTable, segment_start
from sextant.ranking.engine import ReferencePrices, Segment
from sextant.ranking.publisher import AssetState, RankingPublisher
from sextant.ranking.registry import ROWS, Registry, RegistryRow
from sextant.tradingview.bars import (
    MAX_BATCH,
    AlpacaBarSource,
    Bar,
    BarPoll,
    PacedStockBars,
    build_universe,
)
from sextant.tradingview.baseline import SCALE, FiveSessionBaseline, _session_start
from sextant.tradingview.bridge import DisplayBounds, project_display_unit
from sextant.tradingview.cadence import Cadence
from sextant.tradingview.host import BarFidelityHost, load_symbol_map
from sextant.tradingview.inputs import read_research_inputs
from sextant.tradingview.listings import creation_time
from sextant.tradingview.producer import Clocks, reserve_stream
from sextant.tradingview.ranked_view import generation_view, view_document, write_view

NS = 1_000_000_000
MINUTE_NS = 60 * NS
# OVERNIGHT (20:00-04:00 ET) is the next PR: bridge.py must accept it and #138
# supplies the boats baseline for this same publish path.
SUPPORTED = frozenset({"PRE", "RTH", "POST"})
ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CADENCE_REGISTRY = ROOT / "lanes/tradingview/j5-cadence.params.json"
DEFAULT_K2_REGISTRY = ROOT / "lanes/tradingview/k2-ranking.params.json"
DEFAULT_PRESTAGE_PARAMETERS = ROOT / "lanes/tradingview/prestage-parameters.json"
DEFAULT_DISPLAY_BOUNDS = ROOT / "lanes/tradingview/display-bounds.params.json"
DEFAULT_STATE_ROOT = Path.home() / "sextant-data" / "tradingview-prestage"
STREAM_SOURCE = "REST-only bar-fidelity display host; zero streams opened on slot 2"
SIP_AUTHORITY = "slot-2 SIP market-data entitlement; REST-only display host"
MAPPING_SOURCE = "NASDAQ Trader nasdaqlisted.txt + otherlisted.txt read 2026-11-09"
REFERENCE_SOURCE = "alpaca_sip_split_1day official split-adjusted close"
ASSET_SOURCE = (
    "NASDAQ Trader directory operating-company superset; NOT PIT membership; "
    "halt status unknown (no status receipt)"
)
PROVENANCE = "alpaca_sip_raw_1min prior XNYS sessions; bar fidelity streaming one segment"
KEY_NAMES = ("ALPACA_PAPER_2_API_KEY", "ALPACA_PAPER_2_SECRET_KEY")
BUDGET_NAME = "prestage_request_budget"
BOUNDS = {"gap_bound": "fraction", "rvol_bound": "ratio", "max_age_s": "seconds"}


class PrestageError(ValueError):
    """A refused producer input or tick; the previous complete unit stays in place."""


class Source(Protocol):
    """The recorded or live bar history one producer tick may read."""

    def minute_bars(
        self, symbols: Sequence[str], *, start: datetime, end: datetime
    ) -> tuple[Bar, ...]: ...

    def daily_closes(self, symbols: Sequence[str], *, day: date) -> dict[str, Decimal]: ...


def load_registry(path: Path) -> Registry:
    """Read the operational K2 registry file into the validated registry."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != "k2-ranking.registry/1":
        raise PrestageError("not a K2 ranking registry")
    rows = []
    for item in raw.get("rows") or ():
        name, unit = item.get("name"), item.get("unit")
        kind = ROWS.get(name)
        if kind is None or kind[1] != unit:
            raise PrestageError(f"unknown registry row: {name}")
        raw_value = item["value"]
        if kind[0] is int:
            if type(raw_value) is not int:
                raise PrestageError(f"invalid registry value: {name}")
            value = raw_value
        else:
            if type(raw_value) not in (int, str):
                raise PrestageError(f"invalid registry value: {name}")
            value = Decimal(raw_value)
        rows.append(
            RegistryRow(
                name,
                unit,
                value,
                item["source"],
                item["calibration_state"],
                date.fromisoformat(item["last_change"]),
            )
        )
    return Registry(tuple(rows))


def load_maps(
    nasdaq_listed: Path, other_listed: Path, symbol_map: Path
) -> tuple[tuple[str, ...], dict[str, str], str]:
    """Public directory texts plus the exact TradingView map, and their provenance.

    A directory name with no exact mapping is omitted from the display universe and
    counted, never guessed (ADR-0021 Disagreements: the pinned consumer omits unmapped
    rows). Each file's File Creation Time is carried so a stale listing is visible.
    """
    texts = [Path(path).read_text(encoding="utf-8") for path in (nasdaq_listed, other_listed)]
    directory = build_universe(*texts)
    raw = load_symbol_map(Path(symbol_map))
    universe = tuple(name for name in directory if raw.get(name))
    if not universe:
        raise PrestageError(f"The symbol map maps none of {len(directory)} directory symbols")
    created = " / ".join(creation_time(text) for text in texts)
    provenance = (
        f"NASDAQ Trader nasdaqlisted.txt + otherlisted.txt, File Creation Time {created}; "
        f"{len(universe)} of {len(directory)} directory symbols mapped, "
        f"{len(directory) - len(universe)} unmapped omitted"
    )
    return universe, {name: str(raw[name]) for name in universe}, provenance


def cadence_floor(
    now: datetime,
    *,
    registry: Path = DEFAULT_CADENCE_REGISTRY,
    state_root: Path = DEFAULT_STATE_ROOT,
    weekly_usage_pct: Decimal = Decimal(0),
) -> int | None:
    """The J5 display floor from the shipped registry; never a timer or a literal."""
    cadence = Cadence(Path(state_root) / "cadence-unused", Path(registry), page=lambda _: None)
    return cadence.floor(now, weekly_usage_pct)


def prior_sessions(day: date, count: int = 5) -> tuple[date, ...]:
    """The most recent XNYS sessions strictly before ``day``, oldest first."""
    calendar = get_calendar("XNYS")
    sessions = calendar.sessions_in_range(day - timedelta(days=60), day - timedelta(days=1))
    if len(sessions) < count:
        raise PrestageError(f"fewer than {count} prior XNYS sessions")
    return tuple(item.date() for item in sessions[-count:])


def baseline_sessions(day: date, segment: Segment, count: int = 5) -> tuple[date, ...]:
    """Prior sessions whose start did not move and whose span covers this prefix."""
    candidates = prior_sessions(day, count + 10)
    expected_span = segment.end_ns - segment.start_ns
    chosen: list[date] = []
    for candidate in reversed(candidates):
        anchored = _session_start(candidate, segment)
        found, _, _ = segment_start(anchored + NS)
        if (
            found is None
            or found.name != segment.name
            or found.start_ns != anchored
            or found.end_ns - anchored < expected_span
        ):
            continue
        chosen.append(candidate)
        if len(chosen) == count:
            break
    if len(chosen) < count:
        raise PrestageError(f"fewer than {count} usable prior {segment.name} sessions")
    return tuple(sorted(chosen))


def build_segment_baseline(
    source: Source,
    *,
    segment: Segment,
    symbols: Sequence[str],
    sessions: Sequence[date],
    known_at: int,
    provenance: str = PROVENANCE,
) -> FiveSessionBaseline:
    """Stream one session at a time into the same curves/digest as build_baseline."""
    if segment.name not in SUPPORTED:
        raise PrestageError(f"no baseline for a {segment.name} segment")
    names = tuple(sorted(set(symbols)))
    if not names or len(names) != len(tuple(symbols)):
        raise PrestageError("A baseline needs distinct symbols")
    ordered = tuple(sorted(set(sessions)))
    if len(ordered) < 5 or len(ordered) != len(tuple(sessions)):
        raise PrestageError("A baseline needs five distinct prior sessions")
    span = segment.end_ns - segment.start_ns
    if span <= 0 or span % MINUTE_NS:
        raise PrestageError("A baseline segment spans whole minutes")
    points = span // MINUTE_NS + 1
    index = {name: position for position, name in enumerate(names)}
    dv_curves: list[array] = []
    count_curves: list[array] = []
    digest = hashlib.sha256()
    for day in ordered:
        session_start = _session_start(day, segment)
        start = datetime.fromtimestamp(session_start / NS, ET)
        bars = source.minute_bars(names, start=start, end=start + timedelta(seconds=span // NS))
        dv = array("q", [0]) * (len(names) * points)
        counts = array("i", [0]) * (len(names) * points)
        deltas: dict[int, list[int]] = {}
        trade_deltas: dict[int, list[int]] = {}
        for item in bars:
            row = index.get(item.symbol)
            if row is None:
                raise PrestageError(f"Bar outside the baseline universe: {item.symbol}")
            offset = (item.start_ns - session_start) // MINUTE_NS
            if offset < 0 or offset >= points - 1:
                continue
            delta = deltas.setdefault(row, [0] * points)
            count_delta = trade_deltas.setdefault(row, [0] * points)
            delta[offset] += int(item.close * item.volume * SCALE)
            count_delta[offset] += item.trades
        for row, delta in deltas.items():
            running = 0
            running_trades = 0
            base = row * points
            count_delta = trade_deltas[row]
            for point in range(1, points):
                running += delta[point - 1]
                running_trades += count_delta[point - 1]
                dv[base + point] = running
                counts[base + point] = running_trades
        dv_curves.append(dv)
        count_curves.append(counts)
        digest.update(day.isoformat().encode())
        digest.update(dv.tobytes())
        digest.update(counts.tobytes())
    if len(dv_curves) != 5:
        raise PrestageError("A baseline needs five sessions")
    return FiveSessionBaseline(
        segment=segment.name,
        end_s=span // NS,
        sessions=ordered,
        symbols=names,
        known_at=known_at,
        provenance=provenance,
        dv=tuple(dv_curves),
        counts=tuple(count_curves),
        data_digest=digest.hexdigest(),
    )


def collect_references(
    source: Source, *, day: date, symbols: Sequence[str]
) -> tuple[tuple[str, ...], dict[str, Decimal]]:
    """The official prior-session close, or a refusal; no symbol is invented."""
    closes = source.daily_closes(symbols, day=day)
    names = tuple(name for name in symbols if name in closes)
    if not names:
        raise PrestageError("no prior official close for the universe")
    return names, {name: closes[name] for name in names}


def _at(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(day, clock_time(hour, minute), tzinfo=ET).timestamp()) * NS


def _ns(at: datetime) -> int:
    return int(at.timestamp()) * NS


def _credentials() -> tuple[str, str]:
    """The same with-keys seam C uses: two environment names, never a file or a flag."""
    api_key = os.environ.get(KEY_NAMES[0])
    secret_key = os.environ.get(KEY_NAMES[1])
    if not api_key or not secret_key:
        raise PrestageError(f"{KEY_NAMES[0]} and {KEY_NAMES[1]} must be set by with-keys")
    return api_key, secret_key


def load_request_budget(path: Path = DEFAULT_PRESTAGE_PARAMETERS) -> int:
    """Refuse until a measured request budget and its authority are on record (Rule 2)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("rows") or ()
    matches = tuple(item for item in rows if item.get("name") == BUDGET_NAME)
    if not matches:
        raise PrestageError(f"the prestage request budget row is missing from {path}")
    item = matches[0]
    value = item.get("value")
    source = item.get("source")
    if (
        type(value) is not int
        or not 1 <= value <= 200
        or item.get("scope") != "REVIEWED"
        or item.get("calibration") not in {"UNCALIBRATED", "CALIBRATED"}
        or not isinstance(source, str)
        or not source.strip()
    ):
        raise PrestageError(
            "the prestage request budget is unavailable: no measured value and authority "
            "are on record; record both in lanes/tradingview/prestage-parameters.json (Rule 2)"
        )
    return value


def load_display_bounds(path: Path = DEFAULT_DISPLAY_BOUNDS) -> DisplayBounds:
    """The three display bounds from their sourced registry rows; never a code literal."""
    refusal = PrestageError(
        f"the display bounds in {path} need exactly one sourced row each for "
        f"{', '.join(BOUNDS)} (Rule 2)"
    )
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("rows") if raw.get("schema") == "tv-display-bounds.registry/1" else None
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in rows)
        or sorted(row["name"] for row in rows) != sorted(BOUNDS)
    ):
        raise refusal
    values: dict[str, Any] = {}
    for row in rows:
        name, value, source = row["name"], row.get("value"), row.get("source")
        try:
            date.fromisoformat(row.get("last_change"))
        except (TypeError, ValueError):
            raise refusal from None
        if (
            row.get("unit") != BOUNDS[name]
            or row.get("calibration_state") not in {"UNCALIBRATED", "CALIBRATED"}
            or not isinstance(source, str)
            or not source.strip()
            or type(value) is not (int if name == "max_age_s" else str)
        ):
            raise refusal
        values[name] = value
    try:
        return DisplayBounds(
            Decimal(values["gap_bound"]), Decimal(values["rvol_bound"]), values["max_age_s"]
        )
    except (ArithmeticError, ValueError):
        raise refusal from None


def operator_source(lease: Any, *, requests_per_minute: int) -> AlpacaSource:
    """The default source, built from the with-keys environment as C builds its data client."""
    api_key, secret_key = _credentials()
    pacer = Pacer(Decimal(requests_per_minute))
    lease.bind_pacer(pacer)
    client = PacedStockBars(pacer=pacer, api_key=api_key, secret_key=secret_key)
    return AlpacaSource(
        AlpacaBarSource(client, batch=MAX_BATCH),
        AlpacaDailyBars(client, batch=MAX_BATCH),
    )


class AlpacaDailyBars:
    """Daily official closes through the same paced transport as the minute bars."""

    def __init__(
        self, client: PacedStockBars, *, batch: int = MAX_BATCH, feed: DataFeed = DataFeed.SIP
    ) -> None:
        if type(batch) is not int or not 1 <= batch <= MAX_BATCH:
            raise ValueError(f"A daily close batch is 1..{MAX_BATCH}")
        self._client, self._batch, self._feed = client, batch, feed

    def daily_closes(self, symbols: Sequence[str], *, day: date) -> dict[str, Decimal]:
        if not isinstance(day, date):
            raise ValueError("A daily close request needs a date")
        names = tuple(sorted(set(symbols)))
        if not names:
            return {}
        start = datetime.combine(day, clock_time(0, 0), tzinfo=ET)
        end = start + timedelta(days=1)
        closes: dict[str, Decimal] = {}
        for chunk in _chunks(names, self._batch):
            request = StockBarsRequest(
                symbol_or_symbols=list(chunk),
                timeframe=cast(TimeFrame, TimeFrame.Day),
                start=start,
                end=end,
                feed=self._feed,
                adjustment=Adjustment.SPLIT,
                sort=Sort.ASC,
            )
            bars = cast(BarSet, self._client.get_stock_bars(request))
            for symbol, rows in bars.data.items():
                for row in rows:
                    stamp = row.timestamp
                    local = (
                        stamp.astimezone(ET)
                        if stamp.tzinfo is not None
                        else stamp.replace(tzinfo=UTC).astimezone(ET)
                    )
                    if local.date() != day:
                        continue
                    closes[str(symbol).upper()] = decimal_value(row.close)
        return closes


class AlpacaSource:
    """The live REST source one producer tick reads."""

    def __init__(self, bars: AlpacaBarSource, daily: AlpacaDailyBars) -> None:
        self._bars, self._daily = bars, daily

    def minute_bars(
        self, symbols: Sequence[str], *, start: datetime, end: datetime
    ) -> tuple[Bar, ...]:
        return self._bars.minute_bars(symbols, start=start, end=end)

    def daily_closes(self, symbols: Sequence[str], *, day: date) -> dict[str, Decimal]:
        return self._daily.daily_closes(symbols, day=day)


def _chunks(names: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(names), size):
        yield names[index : index + size]


@dataclass
class RankedView:
    """The opt-in local page: every directory name ranked with no TradingView map.

    The unit keeps its mapped universe; this second publisher shares the tick's
    bars, so an unmapped name shows by its plain ticker. Idle until ``baseline``.
    """

    directory: Path
    max_age_s: int
    cadence_registry: Path = DEFAULT_CADENCE_REGISTRY
    state_root: Path = DEFAULT_STATE_ROOT
    symbols: tuple[str, ...] = ()
    closes: dict[str, Decimal] = field(default_factory=dict)
    baseline: FiveSessionBaseline | None = None
    universe_source: str = ""
    written: bool = False
    previous: Any = None
    # (key, inputs kept alive for the key's ids, publisher, host, last fed close,
    # the (publisher, host) kept before that close or None).
    carried: Any = None

    def write(self, now_ns: int, reason: str | None = None, generation: Any = None) -> None:
        self.previous = generation or self.previous
        refresh = cadence_floor(
            datetime.fromtimestamp(now_ns / NS, UTC),
            registry=self.cadence_registry,
            state_root=self.state_root,
        )
        document = view_document(
            self.previous,
            reason=reason,
            max_age_s=self.max_age_s,
            refresh_s=refresh,
            written_at_ns=now_ns,
        )
        write_view(self.directory, document, now_ns=now_ns)
        self.written = True


def _completed(
    bars: Iterable[Bar], segment: Segment, completed_end_ns: int
) -> dict[int, list[Bar]]:
    by_minute: dict[int, list[Bar]] = {}
    for item in bars:
        if (
            item.start_ns < segment.start_ns
            or item.close_ns > segment.end_ns
            or item.close_ns > completed_end_ns
        ):
            continue
        by_minute.setdefault(item.close_ns, []).append(item)
    return by_minute


def _replay(
    *,
    lease: Any,
    trader: str,
    segment: Segment,
    baseline: FiveSessionBaseline,
    symbols: Sequence[str],
    prior_closes: Mapping[str, Decimal],
    prior_close_ns: int,
    symbol_map: Mapping[str, str | None],
    registry: Registry,
    epoch: str,
    universe_source: str,
    feed: str,
    by_minute: Mapping[int, list[Bar]],
) -> tuple[RankingPublisher, BarFidelityHost, int]:
    """One fresh publisher and host fed every completed minute in clock order."""
    publisher = RankingPublisher(
        bus=MessageBus(TraderId.from_str(trader)),
        epoch=epoch,
        feed=feed,
        segment=segment,
        registry=registry,
        universe=tuple(symbols),
        universe_source=universe_source,
        baseline=cast(BaselineTable, baseline),
    )
    for name in symbols:
        publisher.set_symbol(
            name,
            references=ReferencePrices(prior_closes[name], prior_close_ns, REFERENCE_SOURCE),
            asset=AssetState(prior_close_ns, segment.end_ns, None, "common", ASSET_SOURCE),
        )
    host = BarFidelityHost(
        publisher,
        lease=lease,
        sip_authority=SIP_AUTHORITY,
        symbol_map=dict(symbol_map),
    )
    return publisher, host, _feed_minutes(host, symbols, by_minute, since_ns=None)


def _feed_minutes(
    host: BarFidelityHost,
    symbols: Sequence[str],
    by_minute: Mapping[int, list[Bar]],
    *,
    since_ns: int | None,
    before_ns: int | None = None,
) -> int:
    ingested = 0
    for close_ns in sorted(by_minute):
        if (since_ns is not None and close_ns < since_ns) or (
            before_ns is not None and close_ns >= before_ns
        ):
            continue
        ingested += host.ingest(
            BarPoll(bars=tuple(by_minute[close_ns]), requested=tuple(symbols)),
            market_ns=close_ns,
            received_ns=close_ns,
        )
    return ingested


def _carry_key(
    view: RankedView, segment: Segment, *, lease: Any, epoch: str, prior_close_ns: int, params: str
) -> tuple:
    # Identity, not equality: run() rebuilds these objects for each segment.
    inputs = (lease, view.baseline, view.symbols, view.closes)
    return (segment, epoch, prior_close_ns, params, *map(id, inputs))


def _checkpoint(publisher: RankingPublisher, host: BarFidelityHost) -> tuple | None:
    """Copy a view's state; shared inputs and frozen per-symbol states are not copied.

    The stdlib's seeded memo (CPython 3.13 copy.py:119-131) maps each shared object
    to itself. A state the copy cannot take simply keeps no checkpoint.
    """
    shared = (publisher.bus, host.lease, publisher.registry, publisher.baseline, publisher.segment)
    memo: dict[int, Any] = {id(item): item for item in shared}
    memo.update({id(state): state for state in publisher.states.values()})
    try:
        return copy.deepcopy((publisher, host), memo)
    except (TypeError, copy.Error):
        return None


def _view_replay(
    view: RankedView, by_minute: Mapping[int, list[Bar]], key: tuple, **replay: Any
) -> tuple[RankingPublisher, BarFidelityHost, tuple | None]:
    """Feed a carried view only its new minutes, else replay the segment in full.

    Streaming batches keep state between batches (nautilus_trader d53849b2,
    docs/concepts/backtesting/apis-and-runs.md:74-91). The last fed minute is fed
    again, so the host's revised-bar rule (host.py:186-193) refuses a revision of it;
    the view then restarts from the state kept before that minute, which never saw
    the old bar, and only a refused kept state replays the segment in full. Returns
    the publisher, its host and the state kept before this tick's newest minute.
    """
    carried, view.carried = view.carried, None
    starts: list[tuple | None] = []
    last_ns = None
    if carried is not None and carried[0] == key and by_minute:
        _, _, publisher, host, last_ns, kept = carried
        starts = [(publisher, host), kept]
    # Release references so a refused start is freed before the next one.
    carried = publisher = host = kept = None
    newest = max(by_minute) if by_minute else None
    while starts:
        start = starts.pop(0)
        if start is None:
            continue
        try:
            _feed_minutes(start[1], view.symbols, by_minute, since_ns=last_ns, before_ns=newest)
            kept = _checkpoint(*start)
            _feed_minutes(start[1], view.symbols, by_minute, since_ns=newest)
            return start[0], start[1], kept
        except (ValueError, KeyError, TypeError, ArithmeticError):
            start = kept = None
    publisher, host, _ = _replay(
        **replay, by_minute={close: bars for close, bars in by_minute.items() if close != newest}
    )
    kept = _checkpoint(publisher, host) if newest is not None else None
    if newest is not None:
        _feed_minutes(host, view.symbols, by_minute, since_ns=newest)
    return publisher, host, kept


def _drop_stale(publisher: RankingPublisher, publish_clock_ns: int, max_age_ns: int) -> None:
    for name, state in list(publisher.states.items()):
        if state.latest is None:
            continue
        event_ns = state.latest.observations.get("ts_event")
        price_ns = state.latest.observations.get("last_price_ts")
        if (
            type(event_ns) is not int
            or publish_clock_ns - event_ns > max_age_ns
            or (type(price_ns) is int and publish_clock_ns - price_ns > max_age_ns)
        ):
            publisher.states[name] = replace(state, latest=None)


def publish_tick(
    *,
    lease: Any,
    source: Source,
    segment: Segment,
    baseline: FiveSessionBaseline,
    symbols: Sequence[str],
    prior_closes: Mapping[str, Decimal],
    prior_close_ns: int,
    symbol_map: Mapping[str, str],
    registry: Registry,
    epoch: str,
    universe_source: str,
    now_ns: int,
    unit_dir: Path,
    j5_path: Path,
    bounds: DisplayBounds,
    max_age: int = 1_200,
    clock: Callable[[], int] | None = None,
    feed: str = "sip",
    view: RankedView | None = None,
) -> dict[str, Any]:
    """Replay the current segment once and publish one complete unit.

    When ``clock`` is supplied it is read again after the replay, so the unit is
    stamped and validated with the time the fetch actually finished. A ``view``
    ranks its universe from the same fetch; the unit sees only its own bars.
    """
    if segment.name not in SUPPORTED:
        raise PrestageError(f"the producer does not write {segment.name} units")
    if not segment.start_ns <= now_ns < segment.end_ns:
        raise PrestageError("the publish clock is outside the supplied segment")
    start = datetime.fromtimestamp(segment.start_ns / NS, ET)
    completed_end_ns = now_ns - now_ns % MINUTE_NS
    if completed_end_ns <= segment.start_ns:
        raise PrestageError("no completed minute bars for this segment")
    end = datetime.fromtimestamp(completed_end_ns / NS, ET)
    if view is not None and view.baseline is None:
        view = None
    unit_names = frozenset(symbols)
    fetch = tuple(symbols)
    if view is not None:
        fetch += tuple(name for name in view.symbols if name not in unit_names)
    bars = source.minute_bars(fetch, start=start, end=end)
    by_minute = _completed(
        bars if view is None else (item for item in bars if item.symbol in unit_names),
        segment,
        completed_end_ns,
    )
    clocks = sorted(by_minute)
    if not clocks:
        raise PrestageError("no completed minute bars for this segment")
    latest_close_ns = clocks[-1]

    publisher, host, ingested = _replay(
        lease=lease,
        trader=f"TV-PRODUCER-{segment.name}-001",
        segment=segment,
        baseline=baseline,
        symbols=symbols,
        prior_closes=prior_closes,
        prior_close_ns=prior_close_ns,
        symbol_map=symbol_map,
        registry=registry,
        epoch=epoch,
        universe_source=universe_source,
        feed=feed,
        by_minute=by_minute,
    )

    publication_ns = now_ns if clock is None else int(clock())
    if not segment.start_ns <= publication_ns < segment.end_ns:
        raise PrestageError(f"the {segment.name} segment closed before the tick could publish")
    if publication_ns < latest_close_ns:
        raise PrestageError(
            "the publication clock moved behind the replayed bars: "
            f"{publication_ns} < {latest_close_ns}"
        )
    publish_clock_ns = publication_ns
    max_age_ns = registry.seconds("input_max_age_s")
    _drop_stale(publisher, publish_clock_ns, max_age_ns)
    if view is not None and view.baseline is not None:
        view_names = frozenset(view.symbols)
        view_minutes = _completed(
            (item for item in bars if item.symbol in view_names), segment, completed_end_ns
        )
        key = _carry_key(
            view,
            segment,
            lease=lease,
            epoch=epoch,
            prior_close_ns=prior_close_ns,
            params=registry.params_hash,
        )
        view_publisher, view_host, kept = _view_replay(
            view,
            view_minutes,
            key,
            lease=lease,
            trader=f"TV-VIEW-{segment.name}-001",
            segment=segment,
            baseline=view.baseline,
            symbols=view.symbols,
            prior_closes=view.closes,
            prior_close_ns=prior_close_ns,
            symbol_map=dict.fromkeys(view.symbols),
            registry=registry,
            epoch=f"{epoch}-view",
            universe_source=view.universe_source,
            feed=feed,
        )
        _drop_stale(view_publisher, publish_clock_ns, max_age_ns)
        generation = view_publisher.publish(now=publish_clock_ns)
        if generation is None:
            raise PrestageError("the ranked view publisher returned no generation")
        view.write(
            publish_clock_ns,
            generation=generation_view(
                generation,
                segment=segment.name,
                top_n=int(registry.top_n),
                universe_source=view.universe_source,
            ),
        )
        if view_minutes:
            inputs = (lease, view.baseline, view.symbols, view.closes)
            view.carried = (key, inputs, view_publisher, view_host, max(view_minutes), kept)

    if not host.heartbeat(at=latest_close_ns, received_ns=publish_clock_ns):
        raise PrestageError("the bar-fidelity heartbeat was refused")
    j5 = host.publish(
        Path(j5_path),
        now=publish_clock_ns,
        mapping_clocks=Clocks(latest_close_ns, publish_clock_ns, MAPPING_SOURCE),
    )
    if j5.get("status") != "fresh":
        raise PrestageError(f"the J5 unit is not fresh: {j5.get('reason')}")
    projected = project_display_unit(j5, bounds=bounds)
    unit_dir = Path(unit_dir)
    if unit_dir.exists() and not unit_dir.is_symlink():
        raise PrestageError("the display unit is not a generation pointer; move it aside once")
    generations = unit_dir.parent / f"{unit_dir.name}.generations"
    generation = generations / j5["generation_sha256"]
    for filename, document in projected.documents().items():
        write_atomic(generation / filename, document)
    validated = read_research_inputs(
        generation,
        # The exact clock that stamped generated_at; a float datetime drops its nanoseconds.
        now=unix_nanos_to_dt(publish_clock_ns),
        max_age=max_age,
    )
    pointer = unit_dir.parent / f".{unit_dir.name}.pointer"
    pointer.unlink(missing_ok=True)
    os.symlink(f"{generations.name}/{generation.name}", pointer, target_is_directory=True)
    os.replace(pointer, unit_dir)
    return {
        "schema": "sextant.tv-prestage-receipt/1",
        "segment": segment.name,
        "session_date": segment.session_date.isoformat(),
        "now_ns": now_ns,
        "publish_clock_ns": publish_clock_ns,
        "bars": sum(len(by_minute[clock]) for clock in clocks),
        "ingested": ingested,
        "expires_at_ns": _ns(validated.expires_at),
        "unit_dir": str(unit_dir),
        "generation_sha256": j5["generation_sha256"],
        "universe_source": universe_source,
    }


def _next_display_open(now_ns: int) -> int:
    local = datetime.fromtimestamp(now_ns / NS, ET)
    calendar = get_calendar("XNYS")
    for offset in range(0, 9):
        day = local.date() + timedelta(days=offset)
        candidate = datetime.combine(day, clock_time(4, 0), tzinfo=ET)
        if candidate <= local:
            continue
        if calendar.is_session(day.isoformat()):
            return _ns(candidate)
    raise PrestageError("no display session opens in the next eight days")


def run(
    *,
    research_dir: Path,
    nasdaq_listed: Path,
    other_listed: Path,
    symbol_map_path: Path,
    bounds: DisplayBounds,
    k2_registry: Path = DEFAULT_K2_REGISTRY,
    cadence_registry: Path = DEFAULT_CADENCE_REGISTRY,
    state_root: Path = DEFAULT_STATE_ROOT,
    max_age: int = 1_200,
    once: bool = False,
    clock: Callable[[], int] = time.time_ns,
    sleep: Callable[[float], None] = time.sleep,
    source_factory: Callable[[Any], Source] | None = None,
    page: Callable[[str], None] = print,
    ranked_view_dir: Path | None = None,
) -> int:
    """Refresh one segment's unit on the registry cadence until the display closes.

    ``ranked_view_dir`` opts in to the local ranked page (``RankedView``); without
    it nothing here reads or requests anything more than before.
    """
    if type(max_age) is not int or not 1 <= max_age <= 3_600:
        raise PrestageError("max_age is 1..3600 seconds")
    registry_path = Path(k2_registry)
    registry = load_registry(registry_path)
    universe, symbol_map, listing = load_maps(
        Path(nasdaq_listed), Path(other_listed), Path(symbol_map_path)
    )
    state_root = Path(state_root)
    Cadence(
        state_root / "cadence-unused",
        Path(cadence_registry),
        page=lambda _: None,
    )
    view: RankedView | None = None
    directory: tuple[str, ...] = ()
    if ranked_view_dir is not None:
        texts = (Path(path).read_text(encoding="utf-8") for path in (nasdaq_listed, other_listed))
        directory = build_universe(*texts)
        view = RankedView(
            Path(ranked_view_dir), bounds.max_age_s, Path(cadence_registry), state_root
        )
    refused: set[tuple[date, str]] = set()
    while True:
        now_ns = int(clock())
        segment, _, _ = segment_start(now_ns)
        if segment is None or segment.name not in SUPPORTED:
            reason = (
                "the exchange is closed"
                if segment is None
                else f"{segment.name} is not produced until #138 supplies a BOATS baseline"
            )
            page(f"TV producer idle: {reason}; no unit written.")
            if view is not None:
                view.write(now_ns, f"the producer is idle: {reason}")
            if once:
                return 0
            sleep(max(0.0, (_next_display_open(now_ns) - now_ns) / NS))
            continue
        segment_key = (segment.session_date, segment.name)
        if segment_key in refused:
            if once:
                return 1
            refused_ns = int(clock())
            sleep(max(0.0, (segment.end_ns - refused_ns) / NS))
            continue
        with reserve_stream(
            state_root / "leases",
            paper_slot=2,
            existing_streams=0,
            source=STREAM_SOURCE,
        ) as lease:
            if source_factory is None:
                rate = load_request_budget()
                source = operator_source(lease, requests_per_minute=rate)
            else:
                source = source_factory(lease)
            try:
                sessions = baseline_sessions(segment.session_date, segment)
                baseline = build_segment_baseline(
                    source,
                    segment=segment,
                    symbols=universe,
                    sessions=sessions,
                    known_at=_at(sessions[-1], 20),
                )
                reference_day = (
                    segment.session_date
                    if segment.name == "POST"
                    else prior_sessions(segment.session_date, 1)[-1]
                )
                available, closes = collect_references(source, day=reference_day, symbols=universe)
                reference_ns = (
                    segment.start_ns if segment.name == "POST" else _at(reference_day, 16)
                )
                universe_source = (
                    f"{listing}; {len(available)} of {len(universe)} mapped symbols with a "
                    f"{reference_day.isoformat()} SIP official close; NOT PIT membership"
                )
                epoch = f"tv-producer-{segment.name.lower()}-{segment.session_date:%Y%m%d}"
            except (PrestageError, ValueError, APIError) as error:
                refused.add(segment_key)
                page(
                    f"TV producer skipped the {segment.name} segment on "
                    f"{segment.session_date.isoformat()}: {error}; no unit written."
                )
                if view is not None:
                    view.write(now_ns, f"the producer skipped the {segment.name} segment: {error}")
                if once:
                    return 1
                refused_ns = int(clock())
                sleep(max(0.0, (segment.end_ns - refused_ns) / NS))
                continue
            if view is not None:
                view.baseline = None
                view.write(now_ns, f"building the {segment.name} directory baseline")
                try:
                    view.baseline = build_segment_baseline(
                        source,
                        segment=segment,
                        symbols=directory,
                        sessions=sessions,
                        known_at=_at(sessions[-1], 20),
                    )
                    view.symbols, view.closes = collect_references(
                        source, day=reference_day, symbols=directory
                    )
                    view.universe_source = (
                        f"{len(view.symbols)} of {len(directory)} directory symbols with a "
                        f"{reference_day.isoformat()} SIP official close; no TradingView map "
                        f"applied; NOT PIT membership; {listing}"
                    )
                except (PrestageError, ValueError, APIError) as error:
                    view.baseline = None
                    view.write(
                        now_ns, f"the ranked view skipped the {segment.name} segment: {error}"
                    )
            last_start = int(clock())
            while True:
                now_ns = int(clock())
                if now_ns >= segment.end_ns:
                    break
                if view is not None:
                    view.written = False
                try:
                    registry = load_registry(registry_path)
                    receipt = publish_tick(
                        lease=lease,
                        source=source,
                        segment=segment,
                        baseline=baseline,
                        symbols=available,
                        prior_closes=closes,
                        prior_close_ns=reference_ns,
                        symbol_map={name: symbol_map[name] for name in available},
                        registry=registry,
                        epoch=epoch,
                        universe_source=universe_source,
                        now_ns=now_ns,
                        unit_dir=Path(research_dir),
                        j5_path=state_root / "j5-unit.json",
                        bounds=bounds,
                        max_age=max_age,
                        clock=clock,
                        view=view,
                    )
                    page(json.dumps(receipt, sort_keys=True, default=str))
                except (PrestageError, ValueError, APIError) as error:
                    page(f"TV producer skipped a tick: {error}")
                    if view is not None and view.baseline is not None and not view.written:
                        view.write(now_ns, f"the producer refused the tick: {error}")
                    if once:
                        return 1
                if once:
                    return 0
                delay = cadence_floor(
                    datetime.fromtimestamp(int(clock()) / NS, UTC),
                    registry=cadence_registry,
                    state_root=state_root,
                )
                if delay is None:
                    break
                spent = max(0.0, (int(clock()) - last_start) / NS)
                sleep(max(0.0, delay - spent))
                last_start = int(clock())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sextant.tradingview.prestage",
        description="Keep the newest complete display unit fresh for PRE, RTH and POST.",
    )
    parser.add_argument("--research-dir", type=Path, required=True)
    parser.add_argument("--nasdaq-listed", type=Path, required=True)
    parser.add_argument("--other-listed", type=Path, required=True)
    parser.add_argument("--symbol-map", type=Path, required=True, dest="symbol_map_path")
    parser.add_argument("--display-bounds", type=Path, default=DEFAULT_DISPLAY_BOUNDS)
    parser.add_argument("--gap-bound", type=Decimal)
    parser.add_argument("--rvol-bound", type=Decimal)
    parser.add_argument("--bounds-max-age-s", type=int)
    parser.add_argument("--k2-registry", type=Path, default=DEFAULT_K2_REGISTRY)
    parser.add_argument("--cadence-registry", type=Path, default=DEFAULT_CADENCE_REGISTRY)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--max-age", type=int, default=1_200)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ranked-view-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        explicit = (args.gap_bound, args.rvol_bound, args.bounds_max_age_s)
        if all(value is None for value in explicit):
            bounds = load_display_bounds(args.display_bounds)
        elif any(value is None for value in explicit):
            raise PrestageError("explicit display bounds need all three bound flags")
        else:
            bounds = DisplayBounds(args.gap_bound, args.rvol_bound, args.bounds_max_age_s)
        return run(
            research_dir=args.research_dir,
            nasdaq_listed=args.nasdaq_listed,
            other_listed=args.other_listed,
            symbol_map_path=args.symbol_map_path,
            bounds=bounds,
            k2_registry=args.k2_registry,
            cadence_registry=args.cadence_registry,
            state_root=args.state_root,
            max_age=args.max_age,
            once=args.once,
            ranked_view_dir=args.ranked_view_dir,
        )
    except (PrestageError, ValueError) as error:
        print(f"TV producer stopped: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

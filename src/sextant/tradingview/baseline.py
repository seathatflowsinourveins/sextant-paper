"""Time-matched display baseline curves built from the named feed's minute bars.

Each prior session contributes its own cumulative dollar-volume and trade-count
curve at every minute offset of the segment; a query interpolates inside each
session curve and then takes the median across sessions.  Missing minutes carry
the previous cumulative value forward, exactly as the e2e bar-fidelity adapter
did, and no query extrapolates beyond a session's last completed minute.
"""

from __future__ import annotations

import json
from array import array
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from statistics import median
from zoneinfo import ZoneInfo

from sextant.ranking.baseline import BaselineValue
from sextant.ranking.engine import Segment
from sextant.tradingview.bars import Bar

MINUTE_NS = 60 * 1_000_000_000
NS = 1_000_000_000
SCALE = 10_000
NY = ZoneInfo("America/New_York")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _timestamp(value: datetime) -> int:
    delta = value.astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * NS + delta.microseconds * 1000


def _session_start(day: date, segment: Segment) -> int:
    """The same segment clock time on each prior session's own date.

    The pinned lanes baseline offsets every session from its own segment start
    (northstar-lanes ``session_offset.segment_start``); anchoring every session
    to the current segment would discard all prior-session bars.
    """

    local = (EPOCH + timedelta(microseconds=segment.start_ns // 1000)).astimezone(NY)
    if segment.name == "OVERNIGHT":
        day -= timedelta(days=1)
    return _timestamp(datetime.combine(day, local.timetz()))


class FiveSessionBaseline:
    """Per-session cumulative curves; the median is taken after interpolation."""

    def __init__(
        self,
        *,
        segment: str,
        end_s: int,
        sessions: tuple[date, ...],
        symbols: tuple[str, ...],
        known_at: int,
        provenance: str,
        dv: tuple[array, ...],
        counts: tuple[array, ...],
        data_digest: str,
        feed: str = "sip",
    ) -> None:
        if not provenance:
            raise ValueError("A baseline needs provenance")
        if not segment:
            raise ValueError("A baseline names its segment")
        if feed != ("boats" if segment == "OVERNIGHT" else "sip"):
            raise ValueError("display baseline feed/segment mismatch")
        if type(end_s) is not int or end_s <= 0 or end_s % 60:
            raise ValueError("A baseline ends on a whole minute")
        if not sessions or len(set(sessions)) != len(sessions):
            raise ValueError("A baseline stores distinct prior sessions")
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("A baseline stores distinct symbols")
        points = end_s // 60 + 1
        if len(dv) != len(sessions) or len(counts) != len(sessions):
            raise ValueError("A baseline stores one curve per session")
        if any(len(curve) != len(symbols) * points for curve in (*dv, *counts)):
            raise ValueError("A baseline curve covers every symbol and point")
        self.segment = segment
        self.end_s = end_s
        self.points = points
        self.sessions = sessions
        self.symbols = symbols
        self.known_at = known_at
        self.provenance = provenance
        self.dv = dv
        self.counts = counts
        self.feed = feed
        self.latest_session = max(sessions)
        self._index = {symbol: index for index, symbol in enumerate(symbols)}
        self.identity = sha256(
            json.dumps(
                {
                    "segment": segment,
                    "feed": feed,
                    "end_s": end_s,
                    "sessions": [session.isoformat() for session in sessions],
                    "symbols": list(symbols),
                    "known_at": known_at,
                    "provenance": provenance,
                    "data": data_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def cumulative(
        self, symbol: str, segment: str, offset_s: int | Decimal
    ) -> BaselineValue | None:
        if segment != self.segment:
            return None
        row = self._index.get(symbol)
        if row is None:
            return None
        offset = Decimal(offset_s)
        if offset < 0 or offset > self.end_s:
            return None
        lower = int(offset // 60)
        fraction = (offset - lower * 60) / 60
        upper = min(lower + 1, self.points - 1)
        base = row * self.points
        dollars: list[Decimal] = []
        trades: list[Decimal] = []
        for session in range(len(self.sessions)):
            low = Decimal(self.dv[session][base + lower])
            high = Decimal(self.dv[session][base + upper])
            dollars.append((low + (high - low) * fraction) / SCALE)
            low = Decimal(self.counts[session][base + lower])
            high = Decimal(self.counts[session][base + upper])
            trades.append(low + (high - low) * fraction)
        return BaselineValue(
            dollar_volume=median(dollars),
            trade_count=median(trades),
            sessions=len(self.sessions),
            known_at=self.known_at,
            provenance=self.provenance,
        )


def build_baseline(
    sessions: Sequence[tuple[date, Sequence[Bar]]],
    *,
    symbols: Sequence[str],
    segment: Segment,
    known_at: int,
    provenance: str,
    feed: str = "sip",
) -> FiveSessionBaseline:
    """Accumulate each prior session's minute bars from its own segment start."""
    if not provenance:
        raise ValueError("A baseline needs provenance")
    if type(known_at) is not int or known_at < 0:
        raise ValueError("A baseline needs a non-negative known-at clock")
    ordered = tuple(sorted(sessions, key=lambda pair: pair[0]))
    if not ordered or len({day for day, _ in ordered}) != len(ordered):
        raise ValueError("A baseline needs distinct prior sessions")
    names = tuple(sorted({symbol for symbol in symbols}))
    if not names or len(set(symbols)) != len(symbols):
        raise ValueError("A baseline needs distinct symbols")
    index = {symbol: position for position, symbol in enumerate(names)}
    span = segment.end_ns - segment.start_ns
    if span <= 0 or span % MINUTE_NS:
        raise ValueError("A baseline segment spans whole minutes")
    points = span // MINUTE_NS + 1
    if feed != ("boats" if segment.name == "OVERNIGHT" else "sip"):
        raise ValueError("display baseline feed/segment mismatch")
    dv_curves: list[array] = []
    count_curves: list[array] = []
    digest = sha256()
    for day, bars in ordered:
        session_start = _session_start(day, segment)
        if segment.name == "OVERNIGHT":
            if not session_start + span <= known_at <= segment.start_ns:
                raise ValueError(
                    "overnight history must be completed and known before this segment"
                )
            if not any(session_start <= bar.start_ns < session_start + span for bar in bars):
                raise ValueError("missing requested overnight session")
        dv = array("q", [0]) * (len(names) * points)
        counts = array("i", [0]) * (len(names) * points)
        deltas: dict[int, list[int]] = {}
        trade_deltas: dict[int, list[int]] = {}
        for bar in bars:
            row = index.get(bar.symbol)
            if row is None:
                raise ValueError(f"Bar outside the baseline universe: {bar.symbol}")
            offset = (bar.start_ns - session_start) // MINUTE_NS
            if offset < 0 or offset >= points - 1:
                continue
            delta = deltas.setdefault(row, [0] * points)
            counts_delta = trade_deltas.setdefault(row, [0] * points)
            delta[offset] += int(bar.close * bar.volume * SCALE)
            counts_delta[offset] += bar.trades
        for row, delta in deltas.items():
            running = 0
            running_trades = 0
            base = row * points
            counts_delta = trade_deltas[row]
            for point in range(1, points):
                running += delta[point - 1]
                running_trades += counts_delta[point - 1]
                dv[base + point] = running
                counts[base + point] = running_trades
        dv_curves.append(dv)
        count_curves.append(counts)
        digest.update(day.isoformat().encode())
        digest.update(dv.tobytes())
        digest.update(counts.tobytes())
    return FiveSessionBaseline(
        segment=segment.name,
        end_s=span // 1_000_000_000,
        sessions=tuple(day for day, _ in ordered),
        symbols=names,
        known_at=known_at,
        provenance=provenance,
        dv=tuple(dv_curves),
        counts=tuple(count_curves),
        data_digest=digest.hexdigest(),
        feed=feed,
    )

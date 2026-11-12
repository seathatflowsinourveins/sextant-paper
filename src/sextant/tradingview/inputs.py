"""Read one producer display unit; adapted from northstar-trading@8256c16c.

Source: tradingview/research.py:_envelope/_display_inputs/_candidates and
scanner/display_lists.py:validate_display_contract. No ranking policy is run here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from exchange_calendars import get_calendar
from nautilus_trader.core.datetime import _datetime_to_unix_nanos, dt_to_unix_nanos
from pandas import Timestamp

NAMES = ("NS movers", "NS focus")
# Display units carry slot 2's label; slot 3 was revoked 2026-11-10 (D-46), so 3 refuses.
DISPLAY_PAPER_SLOT = 2
QUALIFIED = re.compile(r"[A-Z0-9_]+:[A-Z0-9_.!\-]+\Z")
TICKER = re.compile(r"[A-Z0-9_.!\-]+\Z")
ET = ZoneInfo("America/New_York")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
RFC3339 = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?"
    r"(Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)"
)


class InputError(ValueError):
    """Unavailable input never means an empty desired list."""


@dataclass(frozen=True)
class ResearchInputs:
    memberships: dict[str, frozenset[str]]
    expires_at: datetime
    unmapped: frozenset[str]
    generation: dict[str, str]


def timestamp(value: Any) -> datetime:
    # Calendar view only; scanner/timestamps.py keeps visibility/order in ns.
    return EPOCH + timedelta(microseconds=_ns(value) // 1000)


def _ns(value: Any) -> int:
    """Wire validation plus Nautilus rc8's pinned, full-range integer bridge."""
    try:
        if isinstance(value, str) and (match := RFC3339.fullmatch(value)):
            whole, decimal, zone = match.groups()
            fraction = int((decimal or "0").ljust(9, "0"))
            result = datetime.fromisoformat(whole + zone)
        elif isinstance(value, Timestamp):
            # Nautilus's own exact pair (core/datetime.py unix_nanos_to_dt/dt_to_unix_nanos)
            # keeps the nanoseconds a datetime's microseconds would drop.
            if value.tzinfo is not None:
                return dt_to_unix_nanos(value)
            raise ValueError
        elif isinstance(value, datetime):
            result = value
            fraction = result.microsecond * 1000
            result = result.replace(microsecond=0)
        else:
            raise ValueError
        if result.utcoffset() is not None:
            result = result.astimezone(UTC).replace(microsecond=0)
            return _datetime_to_unix_nanos(result) + fraction
    except (TypeError, ValueError):
        pass
    raise InputError("Expected an explicit timezone-aware timestamp")


@cache
def _calendar(year: int):
    # Same explicit bounds as upstream sessions.py:_xnys; no wall-clock defaults.
    try:
        return get_calendar("XNYS", start=date(year - 1, 12, 1), end=date(year + 1, 1, 31))
    except ValueError:
        raise InputError("No supported XNYS trading date for this input") from None


def _session(at: datetime) -> tuple[str, bool]:
    # Upstream scanner/sessions.py:scanner_session; calendar only, no feed/client.
    local = at.astimezone(ET)
    overnight = local.time() >= time(20) or local.time() < time(4)
    day = local.date() + timedelta(days=1) if local.time() >= time(20) else local.date()
    if not _calendar(day.year).is_session(day):
        raise InputError("No supported XNYS trading date for this input")
    return day.isoformat(), overnight


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(), parse_float=Decimal)
        if isinstance(doc, dict):
            return doc
    except (OSError, ValueError):
        pass
    raise InputError("Missing or invalid research unit; lists NOT synced")


def _envelope(doc: dict[str, Any], schema: str, now: datetime, max_age: int):
    if (
        doc.get("schema") != schema
        or doc.get("status") != "fresh"
        or doc.get("snapshot_status") != "observed"
    ):
        raise InputError("Research generation is unavailable; lists NOT synced")
    snapshot, as_of, generated = (
        timestamp(doc.get(key)) for key in ("as_of_snapshot", "as_of", "generated_at")
    )
    clocks = tuple(_ns(doc.get(key)) for key in ("as_of_snapshot", "as_of", "generated_at"))
    if not clocks[0] <= clocks[1] <= clocks[2] <= _ns(now):
        raise InputError("Inconsistent or future research clocks")
    if _ns(now) - clocks[0] >= max_age * 1_000_000_000 or clocks[1] - clocks[0] > 600_000_000_000:
        raise InputError("Research generation expired; lists NOT synced")
    session, overnight = _session(now)
    if doc.get("session_date") != session or any(
        _session(at)[0] != session for at in (snapshot, as_of, generated)
    ):
        raise InputError("Research belongs to a different Eastern trading session")
    if overnight:
        expected = {
            "session": "overnight",
            "session_date": session,
            "intraday_feed": "boats",
            "daily_history_feed": "sip",
            "daily_history_scope": "prior completed XNYS sessions; not BOATS intraday evidence",
        }
        if doc.get("ranking_context") != expected or not isinstance(
            doc.get("research_inputs"), dict
        ):
            raise InputError("Overnight input requires explicit BOATS provenance")
    return clocks


def _members(rows: Any, identities: dict[str, Any], reverse: dict[str, str], unmapped: set[str]):
    if not isinstance(rows, list):
        raise InputError("Research members must be an array")
    result: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise InputError("Invalid research member")
        symbol, qualified = row.get("symbol"), row.get("tradingview_symbol")
        if not isinstance(symbol, str) or not TICKER.fullmatch(symbol) or symbol in seen:
            raise InputError("Research tickers must be explicit and unique")
        seen.add(symbol)
        if symbol in identities and identities[symbol] != qualified:
            raise InputError("Cross-list TradingView mappings disagree")
        identities[symbol] = qualified
        if qualified is None:
            unmapped.add(symbol)
            continue
        if (
            not isinstance(qualified, str)
            or not QUALIFIED.fullmatch(qualified)
            or qualified.split(":", 1)[0] in {"SMART", "IB", "IBKR", "ALPACA"}
        ):
            raise InputError("Research needs exact TradingView exchange mappings")
        if qualified in result or (qualified in reverse and reverse[qualified] != symbol):
            raise InputError("Research mappings alias different tickers")
        reverse[qualified] = symbol
        result.add(qualified)
    return frozenset(result)


def read_research_inputs(unit: Path, *, now: datetime, max_age: int = 1200) -> ResearchInputs:
    """Resolve latest once and bind all three documents before OAuth or any write."""
    if now.utcoffset() is None or type(max_age) is not int or not 1 <= max_age <= 3600:
        raise InputError("Need an aware clock and max age 1..3600 seconds")
    unit = unit.resolve()
    top, focus, display = (
        _read(unit / name) for name in ("top100.json", "focus.json", "display-lists.json")
    )
    clocks = _envelope(top, "morning-pack.candidates/1", now, max_age)
    for doc, schema in ((focus, "top100.focus/1"), (display, "ns-display-lists/1")):
        if _envelope(doc, schema, now, max_age) != clocks or any(
            doc.get(key) != top.get(key)
            for key in ("generation", "ranking_context", "research_inputs")
        ):
            raise InputError("Research files must share one exact producer generation")
    generation = display.get("generation")
    if (
        not isinstance(generation, dict)
        or set(generation) != {"as_of", "sha256"}
        or not isinstance(generation["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", generation["sha256"])
        or _ns(generation["as_of"]) != clocks[0]
    ):
        raise InputError("Invalid snapshot generation identity")
    try:
        snapshot = display["sources"]["snapshot"]
        bounds = display["bounds"]
        values = [Decimal(str(bounds[key])) for key in ("gap_bound", "rvol_bound")]
        valid = (
            all(value.is_finite() and value > 0 for value in values)
            and type(bounds["max_age_s"]) is int
            and bounds["max_age_s"] > 0
            and all(snapshot.get(key) == value for key, value in generation.items())
            and display["armed"] in {"observed", "UNVERIFIED"}
            and type(display["excluded_unknown"]) is int
            and display["excluded_unknown"] >= 0
        )
        for name in ("ns_movers", "ns_focus"):
            rows = display[name]
            valid = valid and isinstance(rows, list) and type(display["counts"][name]) is int
            valid = valid and display["counts"][name] == len(rows)
            valid = valid and all(
                isinstance(row.get("labels"), list)
                and row["labels"]
                and all(isinstance(label, str) and label for label in row["labels"])
                for row in rows
            )
        if not valid or len(display["ns_movers"]) > 100:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        raise InputError("Invalid producer display contract") from None
    effective = min(max_age, bounds["max_age_s"])
    if _ns(now) - clocks[0] >= effective * 1_000_000_000:
        raise InputError("Research generation expired; lists NOT synced")
    parent_unmapped: set[str] = set()
    parent = top.get("candidates")
    if (
        not isinstance(parent, list)
        or len(parent) > 100
        or not _members(parent, {}, {}, parent_unmapped)
        or len(parent_unmapped) > 5
    ):
        raise InputError("Parent ranking empty or more than five unmapped names")
    identities: dict[str, Any] = {}
    reverse: dict[str, str] = {}
    unmapped: set[str] = set()
    memberships = {
        name: _members(display[key], identities, reverse, unmapped)
        for name, key in zip(NAMES, ("ns_movers", "ns_focus"), strict=True)
    }
    if not memberships["NS movers"] or len(unmapped) > 5:
        raise InputError("Display movers empty or more than five unmapped names")
    return ResearchInputs(
        memberships,
        EPOCH + timedelta(microseconds=clocks[0] // 1000, seconds=effective),
        frozenset(unmapped),
        generation,
    )

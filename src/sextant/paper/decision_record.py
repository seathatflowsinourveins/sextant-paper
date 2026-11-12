"""B's durable exit-decision record: one append-only JSON line per exit decision.

B-EXIT-1959 left "which rule fired" unverifiable: the post-mortem had the state lines but no
trigger level, no source high and no completing observations. This module is the receipt B
appends for every exit decision it takes: the trigger level at the decision, the quote or tape
row it came from, the high-water mark that produced it, the observation run that completed the
breach, the order submitted and the position bookkeeping around it.

The line is built on the calling thread and written by one daemon writer thread. Appending never
touches the disk on the exit path, never raises and never alters a decision; a failed write pages
and is dropped. Durability is ``sextant.persist.full_fsync`` (carried from
northstar-trading@8256c16c31dc:persist.py); the no-float guard mirrors ``trials._no_floats``.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from sextant.persist import full_fsync

RECORD_VERSION = 1
NS_PER_SECOND = 1_000_000_000
DEFAULT_QUEUE_SIZE = 256
PAGE_QUEUE_SIZE = 64

PageSink = Callable[[str, str | None, str], None]
LineWriter = Callable[[str], None]


def stamp(ns: int) -> str:
    """The exact UTC instant of ``ns`` with nanoseconds, e.g. 2026-11-09T21:59:15.543085000Z."""

    if isinstance(ns, bool) or not isinstance(ns, int):
        raise TypeError("a timestamp must be integer nanoseconds")
    seconds, fraction = divmod(ns, NS_PER_SECOND)
    moment = datetime.fromtimestamp(seconds, tz=UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{fraction:09d}Z"


def number(value: Decimal | int | None) -> str | None:
    """An exact decimal string; a binary float is never money or a quantity."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError("a money or quantity value must be Decimal or int, never float")
    return format(Decimal(value), "f")


def _plain(value: object, path: str = "record") -> Any:
    """The JSON-safe copy of a record body; any float is refused with the path that holds it."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not be a float")
    if isinstance(value, Mapping):
        return {str(key): _plain(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_plain(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} is not a JSON value: {type(value).__name__}")


def _compact(body: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in body.items()
        if value is not None and value != [] and value != {}
    }


@dataclass(frozen=True)
class TapeSpan:
    """The start read's span and budget that produced a tape level."""

    start_ns: int
    end_ns: int
    feed: str
    pages: int
    status: str

    def as_record(self) -> dict[str, object]:
        return {
            "window_start": stamp(self.start_ns),
            "window_end": stamp(self.end_ns),
            "feed": self.feed,
            "pages": self.pages,
            "status": self.status,
        }


@dataclass(frozen=True)
class TriggerSource:
    """Where the exit trigger came from: the native stop, the broker's hwm or a quote row."""

    kind: str
    level: Decimal
    at_ns: int | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    stop_price: Decimal | None = None
    hwm: Decimal | None = None
    trail_percent: Decimal | None = None
    tape: TapeSpan | None = None

    def as_record(self) -> dict[str, object]:
        quote: dict[str, object] = {}
        if self.at_ns is not None:
            quote["at"] = stamp(self.at_ns)
        if self.bid is not None:
            quote["bid"] = number(self.bid)
        if self.ask is not None:
            quote["ask"] = number(self.ask)
        return _compact(
            {
                "kind": self.kind,
                "level": number(self.level),
                "quote": quote,
                "stop_price": number(self.stop_price),
                "hwm": number(self.hwm),
                "trail_percent": number(self.trail_percent),
                "tape": None if self.tape is None else self.tape.as_record(),
            }
        )


@dataclass(frozen=True)
class HighWater:
    """The high the trigger was computed from, with the quote or broker record behind it."""

    value: Decimal
    kind: str
    at_ns: int | None = None
    detail: str = ""

    def as_record(self) -> dict[str, object]:
        return _compact(
            {
                "value": number(self.value),
                "kind": self.kind,
                "at": None if self.at_ns is None else stamp(self.at_ns),
                "detail": self.detail or None,
            }
        )


@dataclass(frozen=True)
class Observation:
    """One trade in the run that completed the breach, with its persistence count."""

    at_ns: int
    price: Decimal
    kind: str
    count: int
    noise: Decimal | None = None
    spread: Decimal | None = None

    def as_record(self) -> dict[str, object]:
        return _compact(
            {
                "at": stamp(self.at_ns),
                "price": number(self.price),
                "kind": self.kind,
                "count": self.count,
                "noise": number(self.noise),
                "spread": number(self.spread),
            }
        )


@dataclass(frozen=True)
class Book:
    """The position, the owner's reservation and the quantity the decision acts on."""

    position: Decimal | None = None
    reserved: Decimal | None = None
    available: Decimal | None = None
    quantity: Decimal | None = None

    def as_record(self) -> dict[str, object]:
        return _compact(
            {
                "position": number(self.position),
                "reserved": number(self.reserved),
                "available": number(self.available),
                "quantity": number(self.quantity),
            }
        )


@dataclass(frozen=True)
class ExitRecord:
    """One exit decision, as it will be appended to the run's receipt."""

    action: str
    at_ns: int
    symbol: str
    trigger: Decimal | None = None
    source: TriggerSource | None = None
    high: HighWater | None = None
    observations: tuple[Observation, ...] = ()
    order: Mapping[str, object] | None = None
    book: Book | None = None
    noise: Decimal | None = None
    noise_detail: Mapping[str, str] | None = None
    note: str = ""
    outcome: str = "decided"

    def as_record(self) -> dict[str, Any]:
        body: dict[str, object] = {
            "record": RECORD_VERSION,
            "action": self.action,
            "at": stamp(self.at_ns),
            "symbol": self.symbol,
            "trigger": number(self.trigger),
            "trigger_source": None if self.source is None else self.source.as_record(),
            "high_water": None if self.high is None else self.high.as_record(),
            "observations": [item.as_record() for item in self.observations],
            "order": None if self.order is None else dict(self.order),
            "book": None if self.book is None else self.book.as_record(),
            "noise": number(self.noise),
            "noise_detail": None if self.noise_detail is None else dict(self.noise_detail),
            "outcome": self.outcome,
            "note": self.note,
        }
        return _plain(_compact(body))


@dataclass(frozen=True)
class Row:
    """A plain record body, such as one row of B's lifecycle Strategy journal; checked like an
    exit record, so a float or a non-JSON value is refused and paged, never written."""

    body: Mapping[str, object]

    def as_record(self) -> dict[str, Any]:
        return _plain(dict(self.body))


class DecisionLog:
    """One append-only JSONL receipt, written by one daemon thread. ``append`` only serializes and
    enqueues: it never writes, starts a thread, blocks or raises on the caller's thread; the pager
    starts with the log, off the protective path, its backlog bounded and its drops counted (the
    logging cookbook's non-blocking ``QueueHandler`` shape).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        on_page: PageSink | None = None,
        writer: LineWriter | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        if queue_size < 1:
            raise ValueError("the decision-record queue needs room for one line")
        self.path = Path(path)
        self._on_page = on_page
        self._writer = writer
        self._closed, self._stopped = False, False
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=queue_size)
        self._thread = threading.Thread(target=self._drain, name="b-decision-record", daemon=True)
        self._pages: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=PAGE_QUEUE_SIZE)
        self._pager: threading.Thread | None = threading.Thread(
            target=self._drain_pages, name="b-decision-record-pages", daemon=True
        )
        self._dropped, self._reported = 0, False
        try:
            self._pager.start()
        except RuntimeError:
            self._pager = None
        try:
            self._thread.start()
        except RuntimeError:
            self._stopped = True

    @property
    def stopped(self) -> bool:
        """True when the writer could not open the receipt and now drops every line."""

        return self._stopped

    @property
    def page_counts(self) -> tuple[int, int]:
        """The pager's waiting pages (at most ``PAGE_QUEUE_SIZE``) and every page dropped."""

        return self._pages.qsize(), self._dropped

    def append(self, record: ExitRecord | Row) -> None:
        """Serialize one decision and hand it to the writer; never blocks the caller."""

        if self._closed:
            return
        if self._stopped:
            self._dispatch_page("decision_record_write_failed", "writer stopped")
            return
        try:
            body = record.as_record()
            line = json.dumps(body, sort_keys=True, allow_nan=False, separators=(",", ":"))
        except Exception as exc:
            self._dispatch_page("decision_record_invalid", type(exc).__name__)
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self._dispatch_page("decision_record_write_failed", "queue full")

    def close(self, timeout: float = 5.0) -> None:
        """Ask the writer to finish and stop the pager; never joins a thread that did not start."""

        if self._closed:
            return
        self._closed = True
        if not self._stopped:
            try:
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                self._dispatch_page("decision_record_write_failed", "queue full")
            self._thread.join(timeout)
        self._pages.shutdown()
        if self._pager is not None:
            self._pager.join(timeout)

    def _dispatch_page(self, reason: str, detail: str) -> None:
        """Hand a page to the started pager; a missing or full backlog only counts a drop."""

        if self._on_page is None:
            return
        if self._pager is None:
            self._dropped += 1
            return
        try:
            self._pages.put_nowait((reason, detail))
        except (queue.Full, queue.ShutDown):
            self._dropped += 1

    def _drain_pages(self) -> None:
        while True:
            try:
                item = self._pages.get()
            except queue.ShutDown:
                return
            self._page(*item)
            if self._dropped and not self._reported:
                self._reported = True
                self._page("decision_record_write_failed", f"{self._dropped} page(s) dropped")

    def _page(self, reason: str, detail: str) -> None:
        if self._on_page is None:
            return
        with contextlib.suppress(Exception):
            self._on_page(reason, None, detail)

    def _drain(self) -> None:
        if self._writer is not None:
            self._run(self._writer)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            self._dispatch_page("decision_record_write_failed", type(exc).__name__)
            self._stopped = True
            return
        try:
            self._run(lambda line: self._write(handle, line))
        finally:
            handle.close()

    def _run(self, write: LineWriter) -> None:
        while True:
            line = self._queue.get()
            if line is None:
                return
            try:
                write(line)
            except Exception as exc:  # a receipt failure must never escape the writer
                self._dispatch_page("decision_record_write_failed", type(exc).__name__)

    @staticmethod
    def _write(handle: TextIO, line: str) -> None:
        handle.write(line + "\n")
        handle.flush()
        full_fsync(handle.fileno())

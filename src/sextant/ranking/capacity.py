"""The capacity-counting Actor for the ranking node's measurement (LEAD-2325 2e, part N1b).

A NautilusTrader ``DataActor`` (docs/concepts/actors.md:1-13) subscribes trade ticks, and
quotes when configured, for the universe, and records per one-second bucket of the node
clock: ticks per channel, the lag ``ts_init - ts_event`` as nearest-rank p50/p95/p99 and
max (the rank rule of paper/feed_tap.py's ``_percentiles``, exact here because every lag is
kept for the open second), the largest gap between consecutive ticks on a channel, and the
Actor's own handler time per tick (``time.perf_counter_ns``). The engine ships no per-tick
data latency statistic at the pin; it ships runner queue pressure (``QueueStateChanged``
with ``queue_depth`` and ``mean_dispatch_ns``, actors.md:196-250), which is recorded as
delivered. A node-clock timer (``set_timer``) writes the receipt atomically every
``receipt_interval_s`` seconds, which the caller passes, and once more on stop. Only the last
``bucket_window_s`` closed seconds and queue events, also the caller's, are kept and written;
older ones are counted as dropped. The node entry passes the run's duration plus one, so a run
keeps every second (lead ruling 04:2x: not a tunable). No socket, no order, and no handler
raises: failures are counted.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from nautilus_trader.common import DataActor
from nautilus_trader.config import DataActorConfig
from nautilus_trader.model import InstrumentId

from sextant.execution.state import write_atomic

NS = 1_000_000_000
QUANTILES = {"p50": 0.50, "p95": 0.95, "p99": 0.99}
CHANNELS = ("trades", "quotes")
# The engine's own DataActorConfig fields, the only keys the catch-all may carry: the
# documented config pattern passes them through to DataActorConfig.__new__
# (docs/concepts/actors.md:15-21; common stub :132-151). Any other key is refused by name.
ENGINE_FIELDS = frozenset({"actor_id", "log_events", "log_commands"})


class CapacityActorConfig(DataActorConfig):
    def __init__(
        self,
        *,
        instrument_ids: Sequence[InstrumentId],
        receipt_path: Path,
        receipt_interval_s: int,
        bucket_window_s: int,
        quotes: bool = False,
        **kwargs: object,
    ) -> None:
        unknown = sorted(set(kwargs) - ENGINE_FIELDS)
        if unknown:
            raise TypeError(f"{type(self).__name__}() got unexpected keyword arguments {unknown}")
        super().__init__()
        ids = tuple(instrument_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("the measured universe needs unique instrument ids")
        if type(receipt_interval_s) is not int or receipt_interval_s <= 0:
            raise ValueError("receipt_interval_s must be a positive int the caller supplies")
        if type(bucket_window_s) is not int or bucket_window_s <= 0:
            raise ValueError("bucket_window_s must be a positive int the caller supplies")
        self.instrument_ids, self.receipt_path = ids, Path(receipt_path)
        self.receipt_interval_s, self.quotes = receipt_interval_s, quotes is True
        self.bucket_window_s = bucket_window_s


def nearest_rank(values: list[int]) -> dict[str, int | None]:
    """feed_tap.py's rule: rank max(1, ceil(q * n)) of the sorted values, and the max."""
    if not values:
        return {**{name: None for name in QUANTILES}, "max": None}
    ordered = sorted(values)
    found = {
        name: ordered[max(1, math.ceil(q * len(ordered))) - 1] for name, q in QUANTILES.items()
    }
    return {**found, "max": ordered[-1]}


class CapacityActor(DataActor):
    """Counts what the node's data client delivers; never sends, holds or opens anything."""

    def __init__(self, config: CapacityActorConfig) -> None:
        super().__init__(config)
        self._ids, self._quotes = config.instrument_ids, config.quotes
        self._path, self._interval = config.receipt_path, config.receipt_interval_s
        self._second: int | None = None
        self._open: dict[str, Any] = _bucket()
        # Review F5: only the last bucket_window_s closed seconds and as many queue events are
        # kept and written; older ones are dropped and counted, never grown without bound.
        window = config.bucket_window_s
        self._closed: deque[dict[str, Any]] = deque(maxlen=window)
        self._last_init: dict[str, int] = {}
        self.queue_states: deque[dict[str, Any]] = deque(maxlen=window)
        self.dropped_buckets = self.dropped_queue_states = 0
        self.ticks = self.errors = self.writes = self.write_errors = 0
        self.faults: dict[str, int] = {}
        self.stopped = False

    def on_start(self) -> None:
        # Review F4: a registration that raises is counted per stage; the shared node runs on.
        ids = list(self._ids)
        self._guard("subscribe", lambda: self.subscribe_trades(ids))
        if self._quotes:
            self._guard("subscribe", lambda: self.subscribe_quotes(ids))
        self._guard("queue_state", self.subscribe_queue_state)
        every = timedelta(seconds=self._interval)
        self._guard(
            "timer",
            lambda: self.clock.set_timer("capacity-receipt", every, callback=self._on_timer),
        )

    def _guard(self, stage: str, register: Any) -> None:
        try:
            register()
        except Exception:
            self.errors += 1
            self.faults[stage] = self.faults.get(stage, 0) + 1

    def on_trade(self, trade: Any) -> None:
        self._count("trades", trade)

    def on_quote(self, quote: Any) -> None:
        self._count("quotes", quote)

    def on_queue_state(self, event: Any) -> None:
        try:
            if len(self.queue_states) == self.queue_states.maxlen:
                self.dropped_queue_states += 1
            self.queue_states.append(
                {
                    "ts": event.ts_event,
                    "channel": str(event.channel),
                    "condition": str(event.condition),
                    "state": str(event.state),
                    "queue_depth": event.queue_depth,
                    "mean_dispatch_ns": event.mean_dispatch_ns,
                }
            )
        except Exception:
            self.errors += 1

    def on_stop(self) -> None:
        try:
            self.clock.cancel_timer("capacity-receipt")
        except Exception:
            self.errors += 1
        self.stopped = True
        self._write(final=True)

    def _count(self, channel: str, tick: Any) -> None:
        began = time.perf_counter_ns()
        try:
            second = self.clock.timestamp_ns() // NS
            if second != self._second:
                if self._second is not None:
                    if len(self._closed) == self._closed.maxlen:
                        self.dropped_buckets += 1
                    self._closed.append(_summary(self._second, self._open))
                self._second, self._open = second, _bucket()
            row = self._open[channel]
            row["lags"].append(tick.ts_init - tick.ts_event)
            previous = self._last_init.get(channel)
            if previous is not None:
                gap = tick.ts_init - previous
                row["max_gap_ns"] = (
                    gap if row["max_gap_ns"] is None else max(row["max_gap_ns"], gap)
                )
            self._last_init[channel] = tick.ts_init
            self.ticks += 1
        except Exception:
            self.errors += 1
        finally:
            spent = time.perf_counter_ns() - began
            handler = self._open["handler_ns"]
            handler["count"] += 1
            handler["sum"] += spent
            handler["max"] = spent if handler["max"] is None else max(handler["max"], spent)

    def _on_timer(self, _event: Any) -> None:
        if not self.stopped:  # a timer event queued before the stop never overwrites it
            self._write(final=False)

    def _write(self, *, final: bool) -> None:
        try:
            buckets = list(self._closed)
            if self._second is not None:
                buckets.append(_summary(self._second, self._open))
            receipt = {
                "final": final,
                "written_ns": self.clock.timestamp_ns(),
                "instrument_ids": [str(iid) for iid in self._ids],
                "quotes": self._quotes,
                "ticks": self.ticks,
                "errors": self.errors,
                "write_errors": self.write_errors,
                "buckets": buckets,
                "dropped_buckets": self.dropped_buckets,
                "queue_states": list(self.queue_states),
                "dropped_queue_states": self.dropped_queue_states,
            }
            write_atomic(self._path, receipt)
            self.writes += 1
        except Exception:
            self.write_errors += 1


def _bucket() -> dict[str, Any]:
    rows: dict[str, Any] = {name: {"lags": [], "max_gap_ns": None} for name in CHANNELS}
    rows["handler_ns"] = {"count": 0, "sum": 0, "max": None}
    return rows


def _summary(second: int, bucket: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"second": second, "handler_ns": dict(bucket["handler_ns"])}
    for name in CHANNELS:
        lags = bucket[name]["lags"]
        out[name] = {
            "count": len(lags),
            "lag_ns": nearest_rank(lags),
            "max_gap_ns": bucket[name]["max_gap_ns"],
        }
    return out

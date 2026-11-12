"""ADR-0003 entry consumer: ranked names and vetoes over the unchanged reader.

The same ``handle`` runs behind a raw ``MessageBus`` subscription (C's process) or a
native ``DataActor`` (sim/backtest). No order, execution, broker or account surface is
imported or exposed, and held names and exits stay with their existing owners.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nautilus_trader.common import DataActor, MessageBus
from nautilus_trader.config import DataActorConfig
from nautilus_trader.model import CustomData

from sextant.ranking.engine import Segment
from sextant.ranking.generation import RankingRow
from sextant.ranking.heartbeat import heartbeat_signal
from sextant.ranking.publisher import ranking_data_type
from sextant.ranking.reader import GenerationReader
from sextant.ranking.registry import Registry

NS = 1_000_000_000
_ENTRY_FEATURES = frozenset({"price", "move", "dvol_ratio", "trades_accel"})


def _positive_age(name: str, value: object) -> int:
    # The entry deployment's ages stay UNAVAILABLE until sourced; a bool is not an int here.
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive int; the consumer refuses to start without it")
    return value


class RankingConsumer:
    """One feed's entry view: fresh names, or a refusal reason and no names."""

    def __init__(
        self,
        *,
        registry: Registry,
        feed: str,
        segment: Segment,
        universe_hash: str,
        baseline_hash: str,
        entry_heartbeat_max_age_s: int,
        entry_generation_max_age_s: int,
        entry_input_max_age_s: int,
        alarm: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self.feed = feed
        self._alarm = alarm
        self.alarm_errors = 0
        self.last_alarm_error: str | None = None
        self._active_alarms: set[tuple[str, str | None, str | None]] = set()
        self._entry_heartbeat_ns = (
            _positive_age("entry_heartbeat_max_age_s", entry_heartbeat_max_age_s) * NS
        )
        self._entry_generation_ns = (
            _positive_age("entry_generation_max_age_s", entry_generation_max_age_s) * NS
        )
        self._entry_input_ns = _positive_age("entry_input_max_age_s", entry_input_max_age_s) * NS
        self.reader = GenerationReader(
            registry,
            feed=feed,
            segment=segment,
            universe_hash=universe_hash,
            baseline_hash=baseline_hash,
            alarm=None if alarm is None else lambda reason: self._emit_alarm(reason, None),
        )
        self._heartbeat: tuple[int, int] | None = None
        self.reason = self.reader.reason

    def handle(self, envelope: CustomData, *, now: int) -> bool:
        """Accept a full generation through the reader; any refusal forces a new heartbeat."""
        accepted = self.reader.accept_generation(envelope, now=now)
        if not accepted:
            self._heartbeat = None
        else:
            # A new generation ends every refusal tied to an older one; current keys stay.
            identity = self.reader.generation_key
            self._active_alarms = {
                key for key in self._active_alarms if key[2] is None or key[2] == identity
            }
        self.reason = self.reader.reason
        return accepted

    def accept_heartbeat(self, *, at: int, received_ns: int) -> bool:
        accepted = self.reader.accept_heartbeat(at=at, received_ns=received_ns)
        self._heartbeat = (at, received_ns) if accepted else None
        if accepted:
            self._clear("entry_heartbeat_stale")
        self.reason = self.reader.reason
        return accepted

    def entry_rank(self, symbol: str, *, now: int) -> int | None:
        if not self._entry_fresh(now):
            return None
        generation = self.reader.generation
        row = (
            None
            if generation is None
            else next((row for row in generation.rows if row.symbol == symbol), None)
        )
        if row is not None and not self._candidate_fresh(row, now):
            return None
        rank = self.reader.entry_rank(symbol, now=now)
        self.reason = self.reader.reason
        return rank

    def ranked_names(self, *, now: int) -> tuple[str, ...]:
        """The full fresh focus list in rank order; never the top projection or a prior list."""
        if not self._entry_fresh(now):
            return ()
        generation = self.reader.generation
        if generation is None:
            return ()
        focused = sorted(
            (row for row in generation.rows if row.rank is not None and row.focus_eligible is True),
            key=lambda row: row.rank if row.rank is not None else 0,
        )
        names: list[str] = []
        filtered = False
        for row in focused:
            if not self._candidate_fresh(row, now):
                filtered = True
                continue
            if self.reader.entry_rank(row.symbol, now=now) is not None:
                names.append(row.symbol)
        if names:
            self.reason = "fresh"
        elif filtered:
            self.reason = "symbol_input_stale"
        else:
            self.reason = self.reader.reason
        return tuple(names)

    def _candidate_fresh(self, row: RankingRow, now: int) -> bool:
        # The generation-level clock is the latest event across all symbols; each candidate's
        # own required features must also fit the entry input age before it gets a rank or name.
        stale = any(
            feature.ts_event is None or not 0 <= now - feature.ts_event <= self._entry_input_ns
            for feature in row.features
            if feature.name in _ENTRY_FEATURES
        )
        if stale:
            self._refuse("symbol_input_stale", row.symbol)
            return False
        self._clear("symbol_input_stale", symbol=row.symbol)
        return True

    def _refuse(self, reason: str, symbol: str | None = None) -> None:
        self.reason = reason
        key = (reason, symbol, self.reader.generation_key)
        if key in self._active_alarms:
            return
        self._active_alarms.add(key)
        self._emit_alarm(reason, symbol)

    def _clear(self, *reasons: str, symbol: str | None = None) -> None:
        identity = self.reader.generation_key
        self._active_alarms = {
            key
            for key in self._active_alarms
            if not (key[0] in reasons and key[1] == symbol and key[2] == identity)
        }

    def _emit_alarm(self, reason: str, symbol: str | None) -> None:
        if self._alarm is None:
            return
        try:
            self._alarm(reason, symbol)
        except Exception as exc:  # containment: a broken transport never un-vetoes a name
            self.alarm_errors += 1
            try:
                self.last_alarm_error = f"{type(exc).__name__}: {exc}"
            except Exception:  # even an unprintable exception stays inside the boundary
                self.last_alarm_error = type(exc).__name__

    def _entry_fresh(self, now: int) -> bool:
        if not self.reader.fresh(now):
            self.reason = self.reader.reason
            return False
        generation = self.reader.generation
        heartbeat = self._heartbeat
        if heartbeat is None or not 0 <= now - heartbeat[0] <= self._entry_heartbeat_ns:
            self._refuse("entry_heartbeat_stale")
            return False
        if generation is None:
            self.reason = "generation_missing"
            return False
        if not 0 <= now - generation.known_at <= self._entry_generation_ns:
            self._refuse("entry_generation_stale")
            return False
        if not 0 <= now - generation.ts_event <= self._entry_input_ns:
            self._refuse("entry_input_stale")
            return False
        self._clear("entry_heartbeat_stale", "entry_generation_stale", "entry_input_stale")
        self.reason = "fresh"
        return True


class RawBusHost:
    """Subscribes the consumer to a raw ``MessageBus``; the caller supplies the clock."""

    def __init__(self, consumer: RankingConsumer, *, clock: Callable[[], int]) -> None:
        self.consumer = consumer
        self._clock = clock

    def subscribe(self, bus: MessageBus) -> None:
        bus.subscribe(ranking_data_type(self.consumer.feed).topic, self._on_generation)

    def _on_generation(self, envelope: CustomData) -> None:
        self.consumer.handle(envelope, now=self._clock())


# DataActorConfig's own fields (common stub :132-151): the documented pattern takes **kwargs
# only so these reach DataActorConfig.__new__ (docs/concepts/actors.md:15-21).
ENGINE_FIELDS = frozenset({"actor_id", "log_events", "log_commands"})


class RankingDataActorConfig(DataActorConfig):
    def __init__(self, *, consumer: RankingConsumer, **kwargs: object) -> None:
        unknown = sorted(set(kwargs) - ENGINE_FIELDS)
        if unknown:  # an unknown or misspelled key is refused by name, never swallowed
            raise TypeError(f"{type(self).__name__}() got unexpected keyword arguments {unknown}")
        super().__init__()
        self.consumer = consumer


class RankingDataActor(DataActor):
    """Native sim/backtest host: the same reader behind ``subscribe_data``."""

    def __init__(self, config: RankingDataActorConfig) -> None:
        super().__init__(config)
        self.consumer = config.consumer

    def on_start(self) -> None:
        self.subscribe_data(ranking_data_type(self.consumer.feed))
        self.subscribe_signal(heartbeat_signal(self.consumer.feed))

    def on_data(self, data: CustomData) -> None:
        self.consumer.handle(data, now=self.clock.timestamp_ns())

    def on_signal(self, signal: Any) -> None:
        """The node's heartbeat: ``at`` is the beaten generation's ``known_at`` (N2b-2a)."""
        if signal.name == heartbeat_signal(self.consumer.feed):
            self.consumer.accept_heartbeat(
                at=signal.ts_event, received_ns=self.clock.timestamp_ns()
            )

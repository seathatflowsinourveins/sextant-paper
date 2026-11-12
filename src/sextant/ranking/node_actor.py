"""The ranking host inside the account's engine node (LEAD-2325 2c, part N1).

NautilusTrader's shape at the manifest pin: a live node hosts the data client, and a
``DataActor`` carries non-trading logic (docs/concepts/actors.md:1-13). ``on_start`` builds
the host on the node's thread, which the host binds (8850b79 ranking/host.py:132, 169-171),
primes coverage and every universe name, and only then subscribes trade ticks
(``subscribe_trades``). Each tick goes to ``host.on_trade``; each generation it returns is
published with ``publish_data`` as ``ranking_data_type(feed)``, the type a Strategy in the
node takes by ``subscribe_data`` (docs/concepts/message_bus.md:55-65, 180-232). The Actor
opens no socket: the node's data client owns the one market-data socket. An exception from
the host, the priming source or a publish is counted, never leaves a handler and pages at
most once per stage and type in each caller-set window, so neither the Actor nor the data
client stops. The Actor holds no switch or registry row; the priming source is read at
start, never at construction.

N1c: the universe's native instrument status (``subscribe_instrument_status``,
``InstrumentStatus.action``/``is_trading``) reaches the host through ``set_symbol`` with the
primed references kept; only a halting action or TRADING is classified, everything else is
an unknown halt state, so the ranking refuses the name (``AssetState.eligible`` needs
``halted is False``). A generation a setter returns is published at once, as for a trade.

N1d (LEAD-0020 2b): halt state starts unknown; a halt stops trading on all Trading Centers
(UTP Plan X.A.10, X.G; CTA XI(a)(vii); FINRA 6120(a)(1), 6121.01), so the first print that
counts for price after the status watch, with no halt since, clears it (N1D-SOURCES.md).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from nautilus_trader.common import DataActor, SocketState, SocketStateChanged
from nautilus_trader.config import DataActorConfig
from nautilus_trader.model import (
    CustomData,
    DataType,
    InstrumentId,
    InstrumentStatus,
    MarketStatusAction,
    TradeTick,
)

from sextant.alpaca.stream_facts import (
    AMENDMENTS,
    AlpacaTradeFacts,
    StreamFacts,
    vendor_trade_id,
)
from sextant.ranking.engine import ReferencePrices, price_counts
from sextant.ranking.generation import Coverage, CoverageState, RankingGeneration
from sextant.ranking.heartbeat import HeartbeatEmitter, heartbeat_signal
from sextant.ranking.publisher import AssetState, ranking_data_type
from sextant.ranking.registry import Registry

Page = Callable[[str, str], None]
# A print's (price_eligible, conditions) from the data client, or None when they are unknown.
TradeFacts = Callable[[TradeTick], tuple[bool, tuple[str, ...]] | None]
# The engine's own DataActorConfig fields, the only keys the catch-all may carry: the
# documented config pattern passes them through to DataActorConfig.__new__
# (docs/concepts/actors.md:15-21; common stub :132-151). Any other key is refused by name.
ENGINE_FIELDS = frozenset({"actor_id", "log_events", "log_commands"})


def refuse_unknown(kwargs: Mapping[str, object], owner: str) -> None:
    unknown = sorted(set(kwargs) - ENGINE_FIELDS)
    if unknown:
        raise TypeError(f"{owner}() got unexpected keyword arguments {unknown}")


NS = 1_000_000_000
# rc8 crates/model/src/enums.rs:1034-1068: halted, paused, suspended, or not available for trading.
HALTED = frozenset(
    {
        MarketStatusAction.HALT,
        MarketStatusAction.PAUSE,
        MarketStatusAction.SUSPEND,
        MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
    }
)


def classify(status: InstrumentStatus) -> bool | None:
    """The asset's halt state: True, False only for TRADING not contradicted, else unknown."""
    if status.action in HALTED:
        return True
    if status.action == MarketStatusAction.TRADING and status.is_trading is not False:
        return False
    return None


class RankingHostProtocol(Protocol):
    """RankingHost's surface at 8850b79 (host.py:173-205), injected so N1 runs on main."""

    def on_trade(
        self, tick: TradeTick, *, facts: tuple[bool, tuple[str, ...]] | None
    ) -> RankingGeneration | None: ...

    def set_symbol(
        self, name: str, *, references: ReferencePrices, asset: AssetState | None
    ) -> RankingGeneration | None: ...

    def set_coverage(self, coverage: Coverage) -> RankingGeneration | None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Priming:
    """What the host needs before its first tick (HOST-FACTS 2.6): coverage and every name."""

    coverage: Coverage
    symbols: Mapping[str, tuple[ReferencePrices, AssetState | None]]


class RankingActorConfig(DataActorConfig):
    def __init__(
        self,
        *,
        instrument_ids: Sequence[InstrumentId],
        feed: str,
        host_factory: Callable[[], RankingHostProtocol],
        priming: Callable[[], Priming],
        page_window_s: int,
        trade_facts: TradeFacts | None,
        page: Page | None = None,
        status_snapshot_window_s: int | None = None,
        heartbeat_registry: Registry | None = None,
        **kwargs: object,
    ) -> None:
        refuse_unknown(kwargs, type(self).__name__)
        super().__init__()
        ids = tuple(instrument_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("the ranking universe needs unique instrument ids")
        if not callable(host_factory) or not callable(priming):
            raise ValueError("the host factory and the priming source must be callables")
        self.instrument_ids, self.feed = ids, feed
        if type(page_window_s) is not int or page_window_s <= 0:
            raise ValueError("page_window_s must be a positive int the caller supplies")
        if trade_facts is not None and not callable(trade_facts):
            raise ValueError("trade_facts must be a callable or None (no print clears)")
        window = status_snapshot_window_s
        if window is not None and (type(window) is not int or window <= 0):
            raise ValueError("status_snapshot_window_s must be None (off) or a positive int")
        self.host_factory, self.priming, self.page = host_factory, priming, page
        self.page_window_s, self.trade_facts = page_window_s, trade_facts
        self.status_snapshot_window_s = window
        if heartbeat_registry is not None and not isinstance(heartbeat_registry, Registry):
            raise ValueError("heartbeat_registry must be the ranking Registry or None (no beat)")
        self.heartbeat_registry = heartbeat_registry


def withheld(generation: RankingGeneration) -> RankingGeneration:
    """DEGRADED and unranked, as the publisher writes a not-ready generation (publisher.py)."""
    rows = tuple(
        replace(row, score=None, rank=None, completed=False, partial=True, focus_eligible=False)
        for row in generation.rows
    )
    coverage = replace(generation.coverage, state=CoverageState.DEGRADED, complete=False)
    return replace(generation, coverage=coverage, rows=rows)


def advance(current: bool | None, status: InstrumentStatus) -> bool | None:
    """One status on a name's halt state: only a resume clears a halt (LEAD-0020 2b)."""
    halted = classify(status)
    return True if halted is None and current is True else halted


class StaleGeneration(Exception):
    """A host returned a generation that does not advance the published lineage."""


class RankingActor(DataActor):
    """Feeds one host from the node's trade ticks and publishes its generations."""

    def __init__(self, config: RankingActorConfig) -> None:
        super().__init__(config)
        self._ids, self._feed = config.instrument_ids, config.feed
        self._factory, self._priming, self._page = config.host_factory, config.priming, config.page
        self._window_ns = config.page_window_s * NS
        self._facts = config.trade_facts
        self._beat_registry = config.heartbeat_registry
        self.heartbeat: HeartbeatEmitter | None = None
        self._primed: dict[str, tuple[ReferencePrices, AssetState | None]] = {}
        self._halted: dict[str, bool | None] = {str(iid.symbol): None for iid in self._ids}
        self._watch_from: int | None = None  # node time the status subscription went active
        self._subscribed_at: int | None = None  # ... first, never moved by a reset (2e)
        self.cleared_by_print = self.cleared_by_status = self.resets = self.stream_gaps = 0
        self.ignored_amendments = 0
        snapshot = config.status_snapshot_window_s
        self._snapshot_ns = None if snapshot is None else snapshot * NS
        self.snapshot_statuses: dict[str, int] | None = None if snapshot is None else {}
        self._status_seen: set[str] = set()
        self.ignored_statuses = 0
        self.host: RankingHostProtocol | None = None
        self.primed = False
        self.published = 0
        self.errors: dict[str, int] = {}
        self.last_error: str | None = None
        self._paged: dict[str, int] = {}  # last page time per stage: a bounded key set (F6)
        self._lineage: tuple[str, int] | None = None  # the last published (epoch, sequence)
        self._retired: set[str] = set()
        self._last: RankingGeneration | None = None  # the last generation on the bus
        # Review F1: statuses before priming, folded per name (state, time, reason).
        self._early: dict[str, tuple[bool | None, int, str]] = {}
        # Review F2: names whose halt state the host refused, retried before each tick.
        self._veto: dict[str, tuple[bool | None, int, str]] = {}

    def on_start(self) -> None:
        """Build, then prime, then subscribe: never a tick to an unprimed host."""
        self._reset_run()
        if self._beat_registry is not None:  # main's emitter, beating after each publish
            self.heartbeat = HeartbeatEmitter(
                registry=self._beat_registry, clock=self.clock.timestamp_ns, sink=self._beat
            )
        try:
            self.host = self._factory()
        except Exception as exc:
            return self._fault("host_factory", exc)
        try:
            priming = self._priming()
            names = {str(iid.symbol) for iid in self._ids}
            if set(priming.symbols) != names:
                raise ValueError("priming must name exactly the universe")
            self._publish(self.host.set_coverage(priming.coverage))
            self._primed = dict(priming.symbols)
            for name in sorted(names):  # unknown whatever priming says (LEAD-0020 2b)
                references, asset = priming.symbols[name]
                unknown = None if asset is None else replace(asset, halted=None)
                self._publish(self.host.set_symbol(name, references=references, asset=unknown))
        except Exception as exc:
            return self._fault("priming", exc)
        self.primed = True
        for name in sorted(self._early):  # kept, never dropped (review F1)
            self._move(name, *self._early.pop(name), stage="instrument_status")
        try:  # a registration failure faults and pages; the shared node runs on (review F4)
            if isinstance(self._facts, StreamFacts):  # the client's facts and gaps (N1e)
                self.subscribe_data(self._facts.facts_type)
                self.subscribe_data(self._facts.gap_type)
                for kind in AMENDMENTS:  # corrections and cancels (N1f)
                    self.subscribe_data(DataType(kind, metadata={"feed": self._facts.feed}))
            self.subscribe_socket_state()  # a reported transport change is a stream gap
            self.watch_statuses()
            self.subscribe_trades(list(self._ids))
        except Exception as exc:
            self._fault("subscribe", exc)
        self._on_clock(None)

    def _on_clock(self, _event: object) -> None:
        """A host with ``on_clock`` (the one optional protocol addition, N2b-4) is called now
        and again at the boundary it names, by a node-clock alert (``Clock.set_time_alert_ns``,
        common/__init__.pyi:647-653; the alert pattern of docs/concepts/actors.md:131-151)."""
        turn = getattr(self.host, "on_clock", None)
        if turn is None or not self.primed:
            return
        now = self.clock.timestamp_ns()
        try:
            generation, boundary = turn(now)
        except Exception as exc:
            return self._fault("clock", exc)
        self._publish(generation)
        if boundary is not None and boundary > now:
            try:
                self.clock.set_time_alert_ns(
                    f"ranking-boundary-{boundary}", boundary, callback=self._on_clock
                )
            except Exception as exc:
                self._fault("clock", exc)

    def _reset_run(self) -> None:
        """A (re)start builds a new host: no evidence, lineage or pending state from an earlier
        run carries over (review G1)."""
        self.host, self.primed, self._primed = None, False, {}
        self._halted = dict.fromkeys(self._halted)  # every name unknown again
        self._watch_from = self._subscribed_at = self._lineage = self._last = None
        self._received = False  # no print, status or fact yet: a connect then is no gap
        if isinstance(self._facts, StreamFacts):  # no earlier run's conditions (P2)
            self._facts.clear()
        if self.snapshot_statuses is not None:  # the 2e recorder counts this run only (P2)
            self.snapshot_statuses = {}
        for pending in (self._status_seen, self._early, self._veto, self._retired):
            pending.clear()

    def watch_statuses(self) -> None:
        """Subscribe the universe's statuses; prints count as evidence only after this."""
        self.subscribe_instrument_status(list(self._ids))
        self._watch_from = self._subscribed_at = self.clock.timestamp_ns()

    def on_instrument_status(self, status: InstrumentStatus) -> None:
        """A halt sets halted, a resume clears it, anything else from halted stays halted."""
        self._received = True
        name = str(status.instrument_id.symbol)
        why = f"instrument status {status.action.name}"
        if not self.primed:
            if status.instrument_id in self._ids:  # kept and applied once primed (review F1)
                current = self._early.get(name, (None, 0, ""))[0]
                self._early[name] = (advance(current, status), status.ts_init, why)
            return
        if self.host is None or status.instrument_id not in self._ids or name not in self._primed:
            self.ignored_statuses += 1
            return
        self._record_first(name, status)
        halted = advance(self._halted[name], status)
        self._move(name, halted, status.ts_init, why, stage="instrument_status")

    def _record_first(self, name: str, status: InstrumentStatus) -> None:
        """2e: a name's first status soon after subscribing may be the vendor's current state."""
        first, start = name not in self._status_seen, self._subscribed_at
        self._status_seen.add(name)
        if self.snapshot_statuses is None or self._snapshot_ns is None or start is None:
            return
        if first and status.ts_init - start <= self._snapshot_ns:
            action = status.action.name
            self.snapshot_statuses[action] = self.snapshot_statuses.get(action, 0) + 1

    def on_socket_state(self, event: SocketStateChanged) -> None:
        """A LiveNode relays a network client's CONNECTED or DISCONNECTED (crates/live node
        mod.rs publish_socket_state_change; crates/network sink.rs). Any event after which
        prints may have been missed follows the gap rule. Only a connect while this run has
        received nothing (no print, status or fact) is exempt (lead ruling 04:3x; #537 P1)."""
        if event.state != SocketState.CONNECTED or self._received:
            self.reset_evidence(f"socket {event.state}")

    def reset_evidence(
        self, reason: str = "stream gap reported by the node entry", *, since: int | None = None
    ) -> None:
        """A gap: DEGRADED first, then every name unknown; prints count only after ``since``
        (default: now). The node entry's own call takes the same path (#537 P2)."""
        if self.host is not None and self.primed:
            self._gap(reason, since=since)

    def _reset_names(self, reason: str, since: int | None) -> None:
        self.resets += 1
        now = self.clock.timestamp_ns()
        if self._watch_from is not None:
            self._watch_from = now if since is None else since
        for name in sorted(self._halted):
            self._move(name, None, now, reason)

    def on_data(self, data) -> None:
        """The data client's per-trade facts and stream gaps (N1e, DESIGN-N1E)."""
        self._received = True
        source = self._facts
        if not isinstance(source, StreamFacts) or self.host is None or not self.primed:
            return
        kind = data.data_type.type_name
        if kind == source.facts_type.type_name and isinstance(data.data, AlpacaTradeFacts):
            if data.data.instrument_id in self._ids:
                source.record(data.data)
        elif kind in AMENDMENTS:
            self._amend(data.data)
        elif kind == source.gap_type.type_name:
            self.stream_gaps += 1
            self.reset_evidence("stream reconnect", since=data.data.ts_init)

    def _gap(self, reason: str, *, since: int | None) -> None:
        """A stream gap: DEGRADED for the rest of the segment (LEAD-0033 2e) is published
        first, so no per-name reset below can publish a READY generation that still ranks a
        name not yet reset (gate review of #531). Then every name is unknown and the watch
        restarts at the new connection's receipt time."""
        assert self.host is not None
        try:
            generation = self.host.set_coverage(Coverage(CoverageState.DEGRADED, False))
        except Exception as exc:
            self._fault("set_coverage", exc)
        else:
            self._publish(generation)
        self._reset_names(reason, since)

    def _amend(self, event: Any) -> None:
        """A correction or cancel names its original print; the host withholds that name's rank
        while its live windows contain the print (LEAD-2357 1b, N1f)."""
        turn = getattr(self.host, "on_amendment", None)
        if event.instrument_id not in self._ids:
            self.ignored_amendments += 1
            return
        if turn is None:
            return
        key = vendor_trade_id(event.exchange, event.original_id)
        price, size = event.original_price, event.original_size  # the print it names, in full
        try:
            notional = price * size  # the record's own fields
            name = str(event.instrument_id.symbol)
            generation = turn(name, key, price=price, size=size, notional=notional)
        except Exception as exc:
            return self._fault("amendment", exc)
        self._publish(generation)

    def _clear_on_print(self, trade: TradeTick, facts: tuple[bool, tuple[str, ...]] | None) -> None:
        name, start = str(trade.instrument_id.symbol), self._watch_from
        if trade.instrument_id not in self._ids or self._halted.get(name, True) is not None:
            return  # outside the universe, halted (a print never clears), or already clear
        if start is None or min(trade.ts_event, trade.ts_init) <= start:
            return
        if facts is None or not price_counts(*facts):
            return
        self.cleared_by_print += 1
        self._move(name, False, trade.ts_init, "first eligible print after the status watch")

    def _move(
        self, name: str, halted: bool | None, at: int, why: str, *, stage: str = "set_symbol"
    ) -> None:
        if self.host is None or (self._halted[name] is halted and name not in self._veto):
            return
        if stage == "instrument_status" and self._halted[name] is None and halted is False:
            self.cleared_by_status += 1  # unknown left by a TRADING status, not by a print
        self._halted[name] = halted
        self._apply(name, halted, at, why, stage)

    @property
    def vetoed(self) -> set[str]:
        return set(self._veto)

    def _apply(self, name: str, halted: bool | None, at: int, why: str, stage: str) -> None:
        """One halt state into the host. If the host refuses it, the name is unknown (or halted)
        until a retry succeeds, and every generation is withheld meanwhile (review F2)."""
        references, asset = self._primed[name]
        updated = (
            None  # no primed asset: the name stays ineligible (publisher.py AssetState)
            if asset is None
            else replace(asset, known_at=at, halted=halted, source=f"{asset.source}; {why}")
        )
        try:
            assert self.host is not None
            generation = self.host.set_symbol(name, references=references, asset=updated)
        except Exception as exc:
            fallback = True if halted is True else None
            self._veto[name] = (fallback, at, why)
            self._halted[name] = fallback
            self._fault(stage, exc)
            return self._withdraw()
        self._veto.pop(name, None)
        self._publish(generation)

    def evidence(self) -> dict[str, int]:
        states = list(self._halted.values())
        return dict(
            unknown=states.count(None),
            not_halted=sum(state is False for state in states),
            halted=sum(state is True for state in states),
            cleared_by_print=self.cleared_by_print,
            cleared_by_status=self.cleared_by_status,
            resets=self.resets,
        )

    def on_trade(self, trade: TradeTick) -> None:
        self._received = True
        host = self.host
        if host is None or not self.primed:
            return
        for name in sorted(self._veto):  # retry a refused halt state before the next tick
            self._apply(name, *self._veto[name], "instrument_status")
        facts = None  # read once: the evidence and the host see the same facts (N1e)
        try:
            facts = None if self._facts is None else self._facts(trade)
        except Exception as exc:
            self._fault("trade_facts", exc)
        try:  # before the host, so this print's own generation already sees the evidence
            self._clear_on_print(trade, facts)
        except Exception as exc:
            self._fault("evidence", exc)
        try:
            generation = host.on_trade(trade, facts=facts)
        except Exception as exc:
            return self._fault("on_trade", exc)
        self._vendor(trade)
        self._publish(generation)

    def _vendor(self, trade: TradeTick) -> None:
        """Hand the host the raw price and size of the facts record paired with this tick, so
        an amendment is compared raw to raw (N1f). A tick without one stays unprimed."""
        turn, source = getattr(self.host, "on_vendor_print", None), self._facts
        if turn is None or not isinstance(source, StreamFacts):
            return
        try:
            sent = source.vendor(trade)
            if sent is not None:
                turn(trade, price=sent[0], size=sent[1])
        except Exception as exc:
            self._fault("vendor_print", exc)

    def _withdraw(self) -> None:
        """Republish the last generation in its withheld form at once, so a consumer drops the
        rank now rather than at its age bound (review P2). A DEGRADED copy is refused before
        the reader's sequence check (reader.py:110-116), so it never moves the lineage."""
        if self._last is None:
            return
        data_type = ranking_data_type(self._feed)
        try:
            self.publish_data(data_type, CustomData(data_type, withheld(self._last)))
        except Exception as exc:
            self._fault("publish", exc)

    def _publish(self, generation: RankingGeneration | None) -> None:
        """A returned generation reaches the node bus at once; None publishes nothing."""
        if generation is None or not self._advances(generation):
            return
        if self._veto:  # a refused halt state: the publisher's own not-ready form, every row
            generation = withheld(generation)
        data_type = ranking_data_type(self._feed)
        try:  # a DEGRADED veto is published too, so a consumer drops its names at once
            self.publish_data(data_type, CustomData(data_type, generation))
        except Exception as exc:
            return self._fault("publish", exc)
        identity = generation.generation_id
        self._lineage = (identity.producer_epoch, identity.sequence)
        self._last = generation
        self.published += 1
        if self.heartbeat is not None:  # only after the generation is on the bus
            self.heartbeat.emit(generation)

    def _beat(self, *, at: int, received_ns: int) -> bool:
        self.publish_signal(heartbeat_signal(self._feed), str(at), ts_event=at)
        return True

    def _advances(self, generation: RankingGeneration) -> bool:
        """Only a generation past the last published one leaves (review F3; reader.py:220-235)."""
        identity, last = generation.generation_id, self._lineage
        epoch, sequence = identity.producer_epoch, identity.sequence
        if epoch in self._retired or (
            last is not None and epoch == last[0] and sequence <= last[1]
        ):
            self._fault("lineage", StaleGeneration(f"{epoch}/{sequence}"))
            return False
        if last is not None and epoch != last[0]:
            self._retired.add(last[0])
        return True

    def on_stop(self) -> None:
        if self.host is None:
            return
        try:
            self.host.close()
        except Exception as exc:
            self._fault("close", exc)

    def _fault(self, stage: str, exc: Exception) -> None:
        self.errors[stage] = self.errors.get(stage, 0) + 1
        kind = type(exc).__name__
        self.last_error = f"{stage}: {kind}"
        now, last = self.clock.timestamp_ns(), self._paged.get(stage)
        if self._page is None or (last is not None and now - last < self._window_ns):
            return  # at most one page per stage in each window, whatever the error type
        self._paged[stage] = now
        try:
            self._page(stage, kind)
        except Exception:  # a broken pager never stops the Actor either
            self.errors["page"] = self.errors.get("page", 0) + 1

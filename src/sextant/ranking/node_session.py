"""The ranking node's segment session: one publisher per segment (LEAD-0115 6, N2b-2a).

``SegmentedHost`` stands behind the RankingActor's host protocol. A publisher is valid for one
segment (engine.py: "source/session changed", "trade outside supplied session"), so each tick
is routed by its event time's segment, and the first tick of a later segment rolls over to a
fresh ``RankingPublisher`` and ``NodeRankingHost`` primed from that segment's inputs.

A segment's prefix counts as covered by observation only when this host, built in the same
Actor ``on_start`` callback that subscribes statuses and trades, existed before the segment's
start and no stream gap arrived since (a gap is timed by the node clock and belongs to the
segment that time falls in); then the new publisher's states are marked covered
(the ``replace`` precedent of tradingview/host.py) and its coverage is READY over the
universe. Otherwise, or after a gap inside the segment, the segment is DEGRADED for the rest
of it and withholds every rank. No replay is attempted. The Actor's halt evidence crosses a
rollover; references, ``known_at`` and expiry come from each segment's own inputs. A receipt
per segment is written at rollover and on close (DESIGN-N2B2-BUILD).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from nautilus_trader.common import MessageBus
from nautilus_trader.model import TradeTick

from sextant.execution.state import write_atomic
from sextant.ranking.baseline import BaselineTable, closed_until
from sextant.ranking.engine import ReferencePrices, Segment
from sextant.ranking.generation import Coverage, CoverageState, RankingGeneration
from sextant.ranking.node_actor import Priming
from sextant.ranking.node_host import NodeRankingHost
from sextant.ranking.publisher import AssetState, RankingPublisher
from sextant.ranking.registry import Registry


class _Discard:
    """The publisher's private bus is a sink only (DESIGN-N2B Q4): node consumers read the
    Actor's ``publish_data``. Constructing an engine ``MessageBus`` inside a running node's
    handler stopped all later delivery in a BacktestNode, so none is built here."""

    def publish(self, topic: str, message: Any, external_pub: bool = True) -> None:
        return None


_DISCARD = _Discard()


@dataclass(frozen=True)
class SegmentInputs:
    baseline: BaselineTable
    references: Mapping[str, ReferencePrices]
    assets: Mapping[str, AssetState]


class SegmentedHost:
    def __init__(
        self,
        *,
        feed: str,
        universe: tuple[str, ...],
        universe_source: str,
        registry: Registry,
        inputs: Callable[[Segment], SegmentInputs | None],
        segment_of: Callable[[int], Segment | None],
        clock: Callable[[], int],
        receipt_dir: Path,
        evidence: Callable[[], Mapping[str, int]] | None = None,
    ) -> None:
        self.feed, self.universe, self._source = feed, tuple(sorted(universe)), universe_source
        self._registry, self._inputs, self._segment_of = registry, inputs, segment_of
        self._clock, self._receipt_dir = clock, Path(receipt_dir)
        self._evidence = evidence  # the Actor's run counts, read when a receipt is written
        self.observing_since = clock()  # the Actor subscribes in this same on_start callback
        self._halted: dict[str, bool | None] = dict.fromkeys(self.universe)
        self._known: dict[str, int] = {}
        self.segment: Segment | None = None
        self.host: NodeRankingHost | None = None
        self._current: SegmentInputs | None = None
        self.unrouted = 0
        self._pending_gaps: list[int] = []  # node-clock times of gaps no entered segment holds
        self._retired_end: int | None = None  # the end of a segment retired into closed time
        self._reset_counts()

    def _reset_counts(self) -> None:
        self.observed = False
        self.gaps = self.generations = self.late = self.unprimed = 0
        self.amendments = {"inside": 0, "outside": 0, "unprimed": 0}
        self.amendment_notional = {"inside": Decimal(0), "outside": Decimal(0)}
        self.mismatched, self.mismatched_notional = 0, Decimal(0)  # an id match, not the print
        self._amended_names: set[str] = set()
        self._last: RankingGeneration | None = None

    def actor_priming(self) -> Priming:
        """The Actor's per-name state; this host substitutes each segment's own inputs."""
        size = len(self.universe)
        placeholder = AssetState(0, 1, None, "common", "per-segment asset from the segment host")
        refs = ReferencePrices(None, 0, "per-segment references from the segment host")
        return Priming(
            coverage=Coverage(CoverageState.READY, True, size, size, 0, 0, 0),
            symbols={name: (refs, placeholder) for name in self.universe},
        )

    def on_trade(
        self, tick: TradeTick, *, facts: tuple[bool, tuple[str, ...]] | None
    ) -> RankingGeneration | None:
        segment = self._segment_of(tick.ts_event)
        if segment is None or (segment.name == "OVERNIGHT") != (self.feed == "boats"):
            self.unrouted += 1  # closed, or a segment this feed's publisher refuses
            return None
        entered = None
        retired = self._retired_end
        if self.segment is None and retired is not None and segment.start_ns < retired:
            self.late += 1  # a print of a retired segment: its publisher and receipt are closed
            return None
        if self.segment is None or segment.start_ns >= self.segment.end_ns:
            entered = self._enter(segment)
        elif segment.start_ns < self.segment.start_ns:
            self.late += 1  # a print of an earlier segment: its publisher is gone
            return None
        if self.host is None:
            self.unprimed += 1
            return entered
        return self._count(self.host.on_trade(tick, facts=facts)) or entered

    def on_clock(self, now: int) -> tuple[RankingGeneration | None, int | None]:
        """The node clock's turn (N2b-4): roll at a boundary even with no tick, and name the
        next boundary, so a consumer never waits on a tick for the next segment's generation."""
        segment = self._segment_of(now)
        rankable = segment is not None and (segment.name == "OVERNIGHT") == (self.feed == "boats")
        generation = None
        if segment is not None and rankable:
            if self.segment is None or segment.start_ns >= self.segment.end_ns:
                generation = self._enter(segment)
        elif self.segment is not None and now >= self.segment.end_ns:
            self._write()  # the segment ended into one this feed does not rank
            self._retired_end = self.segment.end_ns  # a later print of it stays late (Codex r1)
            self.segment, self.host, self._current = None, None, None
        if segment is not None:
            return generation, segment.end_ns
        until = closed_until(now)  # closed time keeps the alert chain (Codex r1)
        following = self._segment_of(until)
        early = following is not None and now < following.start_ns <= until
        return generation, following.start_ns if following is not None and early else until

    def set_symbol(
        self, name: str, *, references: ReferencePrices, asset: AssetState | None
    ) -> RankingGeneration | None:
        """Keep the Actor's halt evidence; the references are this segment's, never the Actor's."""
        self._halted[name] = None if asset is None else asset.halted
        if asset is not None:
            self._known[name] = asset.known_at
        if self.host is None or self._current is None:
            return None
        return self._count(
            self.host.set_symbol(
                name, references=self._current.references[name], asset=self._asset(name)
            )
        )

    def on_amendment(
        self,
        name: str,
        key: str,
        *,
        price: Decimal,
        size: Decimal,
        notional: Decimal | None = None,
    ) -> RankingGeneration | None:
        """A correction or cancel, for the current segment's adapter (N1f). The records' own
        notional is summed for the receipt only: the segment's cumulative dollar volume keeps a
        corrected print until the segment ends (a known limit, lead ruling 03:35)."""
        self._amended_names.add(name)
        if self.host is None:
            self.amendments["unprimed"] += 1
            return None
        host = self.host
        before = host.amendments_inside, host.amendments_mismatched, host.amendments_unprimed
        generation = self._count(host.on_amendment(name, key, price=price, size=size))
        if host.amendments_unprimed > before[2]:
            self.amendments["unprimed"] += 1  # the named print has no raw vendor record
            return generation
        if host.amendments_mismatched > before[1]:
            self.mismatched += 1  # its own class: the id names a print of another price or size
            self.mismatched_notional += notional or Decimal(0)
            return generation
        side = "inside" if host.amendments_inside > before[0] else "outside"
        self.amendments[side] += 1
        if notional is not None:
            self.amendment_notional[side] += notional
        return generation

    def on_vendor_print(self, tick: TradeTick, *, price: Decimal, size: Decimal) -> None:
        """The raw vendor values of a print, for the current segment's adapter (N1f)."""
        if self.host is not None:
            self.host.on_vendor_print(tick, price=price, size=size)

    def set_coverage(self, coverage: Coverage) -> RankingGeneration | None:
        """Coverage is this host's observation; a non-READY word from the Actor is a gap.

        A gap belongs to the segment its node-clock time falls in, whatever the callback order:
        one at or after the current segment's end waits for the next segment's entry, which
        counts it if it is at or after that segment's start (Codex r1 P1)."""
        if coverage.state is CoverageState.READY:
            return None
        at = self._clock()
        if self.segment is None or at >= self.segment.end_ns:
            self._pending_gaps.append(at)
            return None
        self.gaps += 1
        self.observed = False
        if self.host is None:
            return None
        return self._count(self.host.set_coverage(Coverage(CoverageState.DEGRADED, False)))

    def close(self) -> None:
        self._write()

    def _enter(self, segment: Segment) -> RankingGeneration | None:
        if self.segment is not None:
            self._write()
        closed_late = self.late if self.segment is None else 0  # retired-segment prints
        self.segment, self.host, self._current = segment, None, None
        self._reset_counts()
        self.late = closed_late
        self.gaps = sum(at >= segment.start_ns for at in self._pending_gaps)
        self._pending_gaps.clear()  # an earlier one fell before this start: resubscribed since
        inputs = self._inputs(segment)
        if inputs is None:
            return None
        publisher = RankingPublisher(
            bus=cast(MessageBus, _DISCARD),  # never a second engine bus inside a running node
            epoch=f"{segment.session_date.isoformat()}-{segment.name}",
            feed=self.feed,
            segment=segment,
            registry=self._registry,
            universe=self.universe,
            universe_source=self._source,
            baseline=inputs.baseline,
        )
        self.observed = self.observing_since < segment.start_ns and self.gaps == 0
        if self.observed:  # covered by observation (LEAD-0115 6a)
            for name, state in publisher.states.items():
                publisher.states[name] = replace(state, segment_covered=True)
        size = len(self.universe)
        coverage = (
            Coverage(CoverageState.READY, True, size, size, 0, 0, 0)
            if self.observed
            else Coverage(CoverageState.DEGRADED, False)
        )
        self.host, self._current = NodeRankingHost(publisher, clock=self._clock), inputs
        last = self._count(self.host.set_coverage(coverage))
        for name in self.universe:
            asset = self._asset(name)
            last = (
                self._count(
                    self.host.set_symbol(name, references=inputs.references[name], asset=asset)
                )
                or last
            )
        return last

    def _asset(self, name: str) -> AssetState:
        assert self._current is not None
        base = self._current.assets[name]
        known = max(base.known_at, self._known.get(name, base.known_at))
        return replace(base, halted=self._halted[name], known_at=known)

    def _count(self, generation: RankingGeneration | None) -> RankingGeneration | None:
        if generation is not None:
            self.generations += 1
            self._last = generation
        return generation

    def _write(self) -> None:
        segment = self.segment
        if segment is None:
            return
        host, last = self.host, self._last
        states = list(self._halted.values())
        counts = self._evidence() if self._evidence is not None else {}
        body: dict[str, Any] = {
            "segment": segment.name,
            "session_date": segment.session_date.isoformat(),
            "start_ns": segment.start_ns,
            "observing_since_ns": self.observing_since,
            "primed": host is not None,
            "observed": self.observed,
            "gaps": self.gaps,
            "generations": self.generations,
            "names_ranked": 0 if last is None else sum(r.rank is not None for r in last.rows),
            "names_unknown": states.count(None),
            "names_halted": sum(state is True for state in states),
            # how names left unknown, the run's totals when this receipt is written (lead 04:4x)
            "cleared": {key: counts.get(key) for key in ("cleared_by_print", "cleared_by_status")},
            "late": self.late,
            "unrouted": self.unrouted,
            "unprimed": self.unprimed,
            "amendments": dict(self.amendments),
            "amendment_notional": {k: format(v, "f") for k, v in self.amendment_notional.items()},
            "amended_names": len(self._amended_names),
            "amendments_mismatched": {
                "count": self.mismatched,
                "notional": format(self.mismatched_notional, "f"),
            },
            "faults": {}
            if host is None
            else {
                "ingress_errors": host.ingress_errors,
                "publish_errors": host.publish_errors,
                "malformed_assets": host.malformed_assets,
                "ignored_trades": host.ignored_trades,
            },
        }
        name = f"ranking-{segment.session_date.isoformat()}-{segment.name}.json"
        write_atomic(self._receipt_dir / name, body)

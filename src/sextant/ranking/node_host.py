"""The node's ranking host: a thin adapter over ``RankingPublisher`` (LEAD-0033 2b, N2b-1).

It stands behind ``RankingHostProtocol`` (node_actor.py) and keeps exactly ruling 2b's
fail-closed rules; every row-level fact stays per row, as the publisher and reader already
enforce (ADR-0003:24; DESIGN-N2B1 sections 4-5):

1. A tick that raises taints only that name for the rest of the segment: its latest row is
   cleared and its prefix marked uncovered (the ``replace`` precedent of
   tradingview/host.py:218-220, 237-238), so its score stays None (engine.py segment rule).
2. A setter call that withdraws authority (coverage no longer READY-shaped, an eligible asset
   no longer eligible, a reference change) publishes at once, unthrottled.
3. A publish that raises sends one veto under a fresh DEGRADED coverage, unthrottled; if
   that raises too the error leaves here and the Actor faults and pages.
4. The caller's coverage lives only in ``supplied``, written only by ``set_coverage`` and
   assigned to the publisher before every publish, so the publisher's own DEGRADED write on
   a raising tick (publisher.py:132-134) never sticks.
5. A reference change clears that name's latest state.

An asset whose fields would make ``AssetState.eligible`` raise is stored as None (unknown,
ineligible); otherwise every later publish, the veto included, would raise and leave an old
rank readable (review witness b). The heartbeat is the node's (DESIGN-N2B1 section 1), so a
publisher carrying its own emitter is refused. No socket, thread, file or bus of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal

from nautilus_trader.model import TradeTick

from sextant.ranking.engine import MoverState, ReferencePrices, price_counts
from sextant.ranking.generation import Coverage, CoverageState, RankingGeneration
from sextant.ranking.publisher import AssetState, RankingPublisher


def _ready_shaped(coverage: Coverage, size: int) -> bool:
    """The publisher's own coverage test (publisher.py:142-150), without the clocks."""
    return bool(
        coverage.state is CoverageState.READY
        and coverage.complete is True
        and coverage.expected_symbols == coverage.observed_symbols == size
        and coverage.missing_symbols == coverage.invalid_symbols == coverage.late_symbols == 0
    )


def _well_formed(asset: AssetState) -> bool:
    ints = (asset.known_at, asset.expires_at)
    return (
        all(type(value) is int for value in ints)
        and type(asset.halted) in (bool, type(None))
        and isinstance(asset.security_type, str)
        and isinstance(asset.source, str)
    )


Identity = tuple[str, Decimal, Decimal]  # trade id (exchange + original id), raw price, size
Sent = Mapping[str, tuple[int, Decimal, Decimal]]  # trade id -> (ts_event, raw price, raw size)


def _ticks(state: MoverState) -> list[TradeTick]:
    """Every print that still feeds a feature: a window bucket or its anchor."""
    ticks = []
    for window in (state.history, state.volume_history):
        ticks += [tick for bucket in window.buckets for tick in bucket.ticks]
        ticks += [] if window.anchor is None else [window.anchor]
    return ticks


def _find(state: MoverState, identity: Identity, sent: Sent) -> str:
    """Where a print is while it still feeds a feature. The tick holds its price only at
    instrument precision, so the vendor's raw price and size travel beside it in the facts
    record (stream_facts.py, N1e) and are compared raw to raw: ``inside`` on an exact match of
    id, raw price and raw size; ``mismatched`` when only the id matches; ``unprimed`` when the
    stored print has no raw record, never matched otherwise; ``outside`` when no stored print
    carries the id (lead 04:2x and 07:1x)."""
    key, price, size = identity
    found = "outside"
    for tick in _ticks(state):
        if tick.trade_id.value != key:
            continue
        raw = sent.get(key)
        if raw is None:
            found = "unprimed"
        elif raw[1:] == (price, size):
            return "inside"
        else:
            found = "mismatched"
    return found


class NodeRankingHost:
    def __init__(self, publisher: RankingPublisher, *, clock: Callable[[], int]) -> None:
        if publisher.heartbeat is not None:
            raise ValueError("the node sends the heartbeat after the Actor publishes")
        self.publisher, self._clock = publisher, clock
        self.supplied = Coverage(CoverageState.UNKNOWN, False)
        self.tainted: set[str] = set()
        self.ingress_errors = self.ignored_trades = self.malformed_assets = 0
        self.publish_errors = 0
        self.amended: dict[str, set[Identity]] = {}  # pending corrected prints per name (N1f)
        self.amendments_inside = self.amendments_outside = self.amendments_mismatched = 0
        self.amendments_unprimed = 0  # the named print has no raw vendor record
        self.sent: dict[str, dict[str, tuple[int, Decimal, Decimal]]] = {}  # per name, in windows
        self.last_error: str | None = None

    def on_trade(
        self, tick: TradeTick, *, facts: tuple[bool, tuple[str, ...]] | None
    ) -> RankingGeneration | None:
        """``facts`` is the Actor's one read of the print's (vendor flag, conditions); unknown
        facts move neither price nor volume (engine.py advance_mover's masks; N1e)."""
        publisher, name = self.publisher, str(tick.instrument_id.symbol)
        if name not in publisher.states:
            self.ignored_trades += 1  # publisher.py:119 would raise; nothing new to publish
            return None
        eligible, conditions = (False, ()) if facts is None else (price_counts(*facts), facts[1])
        try:
            publisher.on_trade(
                tick, price_eligible=eligible, volume_eligible=eligible, conditions=conditions
            )
        except Exception as exc:
            self.ingress_errors += 1
            self.last_error = f"on_trade: {type(exc).__name__}"
            self.tainted.add(name)
            state = publisher.states[name]
            publisher.states[name] = replace(state, latest=None, segment_covered=False)
            return self._publish(force=True)  # rule 1 withdraws that name's rank
        self._hold(name)
        state = publisher.states[name]
        if name in self.tainted and state.segment_covered:  # a late print at the segment start
            publisher.states[name] = replace(state, latest=None, segment_covered=False)  # (G2)
        return self._publish(force=False)

    def on_amendment(
        self,
        name: str,
        key: str,
        *,
        price: Decimal,
        size: Decimal,
        notional: Decimal | None = None,
    ) -> RankingGeneration | None:
        """A correction or cancel: withhold the name while a live window holds the print it
        names in full (exchange and original id in ``key``, original price and size)."""
        identity = (key, price, size)
        where = "outside"
        if name in self.publisher.states:
            where = _find(self.publisher.states[name], identity, self.sent.get(name, {}))
        if where == "unprimed":
            self.amendments_unprimed += 1  # no raw record to compare: nothing is withdrawn
            return None
        if where == "mismatched":
            self.amendments_mismatched += 1  # not the stored print: nothing is withdrawn
            return None
        if where == "outside":
            self.amendments_outside += 1
            return None
        self.amendments_inside += 1
        self.amended.setdefault(name, set()).add(identity)
        self._hold(name)
        return self._publish(force=True)  # a withdrawal publishes at once

    def on_vendor_print(self, tick: TradeTick, *, price: Decimal, size: Decimal) -> None:
        """The vendor's raw price and size of a print the windows just took, from its facts
        record; kept only while a window can still hold that print (bounded by the windows)."""
        name = str(tick.instrument_id.symbol)
        if name not in self.publisher.states:
            return
        sent = self.sent.setdefault(name, {})
        sent[tick.trade_id.value] = (tick.ts_event, price, size)
        oldest = min((t.ts_event for t in _ticks(self.publisher.states[name])), default=0)
        while sent and next(iter(sent.values()))[0] < oldest:  # arrival order is time order
            del sent[next(iter(sent))]

    def _hold(self, name: str) -> None:
        """Keep the row unranked while any pending print is contained; then let it stand."""
        pending = self.amended.get(name)
        if not pending:
            return
        state = self.publisher.states[name]
        sent = self.sent.get(name, {})
        pending = {identity for identity in pending if _find(state, identity, sent) == "inside"}
        if pending:
            self.amended[name] = pending
            self.publisher.states[name] = replace(state, latest=None)
        else:
            del self.amended[name]

    def set_symbol(
        self, name: str, *, references: ReferencePrices, asset: AssetState | None
    ) -> RankingGeneration | None:
        publisher, now = self.publisher, self._clock()
        if asset is not None and not _well_formed(asset):
            self.malformed_assets += 1
            self.last_error = "set_symbol: malformed asset"
            asset = None
        before = publisher.assets.get(name)
        was_eligible = before is not None and before.eligible(now)
        previous = publisher.references.get(name)
        publisher.set_symbol(name, references=references, asset=asset)
        changed = previous is not None and previous != references
        if changed:
            publisher.states[name] = replace(publisher.states[name], latest=None)  # rule 5
        withdrawn = was_eligible and not (asset is not None and asset.eligible(now))
        return self._publish(force=changed or withdrawn)

    def set_coverage(self, coverage: Coverage) -> RankingGeneration | None:
        size = len(self.publisher.universe)
        was_ready = _ready_shaped(self.supplied, size)
        self.supplied = coverage
        return self._publish(force=was_ready and not _ready_shaped(coverage, size))

    def _publish(self, *, force: bool) -> RankingGeneration | None:
        publisher, now = self.publisher, self._clock()
        if force:
            publisher.published_at = None  # unthrottled
        publisher.coverage = self.supplied
        try:
            return publisher.publish(now=now)
        except Exception as exc:
            self.publish_errors += 1
            self.last_error = f"publish: {type(exc).__name__}"
            publisher.coverage = Coverage(CoverageState.DEGRADED, False)
            publisher.published_at = None
            return publisher.publish(now=now)  # a second failure is the Actor's to page

    def close(self) -> None:
        """Nothing to release: the publisher's bus is a private in-process sink."""

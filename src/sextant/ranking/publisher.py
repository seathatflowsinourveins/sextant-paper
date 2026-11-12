"""Fixture feed publisher; no socket or enable path.

Carry: northstar-trading@8256c16c31dc:scanner/s2_k3.py:445-504,
s2_ranking.py:159-239 and data/ranking.py; rc7 MessageBus.publish/CustomData.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Any

from nautilus_trader.common import MessageBus
from nautilus_trader.model import CustomData, DataType, TradeTick

from sextant.ranking.baseline import BaselineTable
from sextant.ranking.engine import (
    MoverState,
    ReferencePrices,
    Segment,
    advance_mover,
    qualifies,
    rank_movers,
)
from sextant.ranking.generation import (
    Coverage,
    CoverageState,
    GenerationId,
    GenerationStatus,
    RankingFeature,
    RankingGeneration,
    RankingReason,
    RankingRow,
    register_ranking_generation,
)
from sextant.ranking.heartbeat import HeartbeatEmitter
from sextant.ranking.registry import Registry

LANES_PIN = "00203275944a22655b3e14b221cfde9a265d549a"
METHOD = "MOVER-v2@" + LANES_PIN
RANKER = "sextant/fixture-ranking/1"


def universe_digest(symbols: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(sorted(symbols), separators=(",", ":")).encode()).hexdigest()


def ranking_data_type(feed: str, view: str = "full") -> DataType:
    return DataType("RankingGeneration", metadata={"feed": feed, "view": view, "ranker": RANKER})


@dataclass(frozen=True, slots=True)
class AssetState:
    known_at: int
    expires_at: int
    halted: bool | None
    security_type: str
    source: str

    def eligible(self, now: int) -> bool:
        return bool(
            self.source
            and 0 <= self.known_at <= now < self.expires_at
            and self.halted is False
            and self.security_type in {"common", "adr"}
        )


class RankingPublisher:
    def __init__(
        self,
        *,
        bus: MessageBus,
        epoch: str,
        feed: str,
        segment: Segment,
        registry: Registry,
        universe: tuple[str, ...],
        universe_source: str,
        baseline: BaselineTable,
        heartbeat: HeartbeatEmitter | None = None,
    ) -> None:
        if not epoch or not universe_source or not universe or len(set(universe)) != len(universe):
            raise ValueError("explicit epoch, unique universe and source required")
        if baseline.feed != feed or (feed == "boats") != (segment.name == "OVERNIGHT"):
            raise ValueError("feed/segment/baseline mismatch")
        register_ranking_generation()
        self.bus, self.epoch, self.feed, self.segment = bus, epoch, feed, segment
        self.registry, self.universe, self.universe_source = (
            registry,
            tuple(sorted(universe)),
            universe_source,
        )
        self.baseline = baseline
        self.heartbeat = heartbeat
        self.universe_hash = universe_digest(self.universe)
        self.states = {name: MoverState() for name in universe}
        self.references: dict[str, ReferencePrices] = {}
        self.assets: dict[str, AssetState | None] = {}
        self.coverage = Coverage(CoverageState.UNKNOWN, False)
        self.published_at: int | None = None
        self.sequence = 0
        self.last_event = segment.start_ns
        self.input_cutoff = segment.start_ns

    def set_symbol(
        self, name: str, *, references: ReferencePrices, asset: AssetState | None
    ) -> None:
        if name not in self.states:
            raise ValueError("symbol outside supplied universe")
        self.references[name], self.assets[name] = references, asset

    def set_coverage(self, coverage: Coverage) -> None:
        self.coverage = coverage

    def on_trade(self, tick: TradeTick, **facts: Any) -> None:
        name = str(tick.instrument_id.symbol)
        try:
            state, row = advance_mover(
                self.states[name],
                tick,
                self.references[name],
                self.baseline,
                segment=self.segment,
                feed=self.feed,
                registry=self.registry,
                **facts,
            )
            self.states[name] = state
            if row is not None:
                self.last_event = max(self.last_event, tick.ts_event)
                self.input_cutoff = max(self.input_cutoff, row.known_at)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.coverage = Coverage(CoverageState.DEGRADED, False)
            raise

    def publish(self, *, now: int) -> RankingGeneration | None:
        if self.published_at is not None:
            if now < self.published_at:
                raise ValueError("publication clock regressed")
            if now - self.published_at < self.registry.seconds("publish_min_interval_s"):
                return None
        ready = (
            self.coverage.state is CoverageState.READY
            and self.coverage.complete
            and self.coverage.expected_symbols == len(self.universe)
            and self.coverage.observed_symbols == len(self.universe)
            and self.coverage.missing_symbols
            == self.coverage.invalid_symbols
            == self.coverage.late_symbols
            == 0
            and self.segment.start_ns
            <= self.last_event
            <= self.input_cutoff
            <= now
            < self.segment.end_ns
            and now - self.last_event <= self.registry.seconds("input_max_age_s")
        )
        ranked = (
            rank_movers(s.latest for s in self.states.values() if s.latest is not None)
            if ready
            else ()
        )
        by_symbol = {row.symbol: row for row in ranked}
        source = json.dumps(
            {
                "feed": self.feed,
                "segment": self.segment.name,
                "start_ns": self.segment.start_ns,
                "end_ns": self.segment.end_ns,
                "session_source": self.segment.source,
                "universe_source": self.universe_source,
                "baseline_source": self.baseline.provenance,
                "params_hash": self.registry.params_hash,
                "universe_hash": self.universe_hash,
                "baseline_hash": self.baseline.identity,
            },
            sort_keys=True,
        )
        rows = []
        for name in self.universe:
            mover = by_symbol.get(name, self.states[name].latest) if ready else None
            asset = self.assets.get(name)
            asset_ok = asset is not None and asset.eligible(now)
            reasons = [RankingReason("SOURCE", source)]
            if not asset_ok:
                reasons.append(RankingReason("asset_or_halt_unknown"))
            if asset is not None:
                reasons.append(RankingReason("ASSET", json.dumps(asdict(asset), sort_keys=True)))
            if mover is not None:
                reasons.extend(
                    RankingReason(reason) for reason in mover.observations["unknown_reasons"]
                )
                reasons.extend(
                    RankingReason("gate_failed", gate)
                    for gate in (
                        "mover_min_move",
                        "move_1m_positive",
                        "mover_min_trades_accel",
                        "mover_min_dvol_ratio",
                    )
                    if gate not in mover.gates_passed
                )
                reasons.append(
                    RankingReason("REFERENCE", str(mover.observations["reference_source"]))
                )
                price = mover.observations["price"]
                reasons.append(
                    RankingReason(
                        "LABELS",
                        json.dumps(
                            {
                                "price_band": None
                                if price is None
                                else ">=5"
                                if price >= 5
                                else "1-5"
                                if price >= 1
                                else "<1",
                                "security_type": asset.security_type if asset else None,
                                "cap_bucket": None,
                            },
                            sort_keys=True,
                        ),
                    )
                )
                reasons.append(
                    RankingReason(
                        "CLOCKS",
                        json.dumps(
                            {
                                "event_ns": mover.observations["ts_event"],
                                "vendor_receipt_ns": mover.observations["ts_init"],
                                "local_receipt_ns": mover.known_at,
                                "generation_ns": now,
                                "publication_ns": now,
                                "entry_eligible_ns": None,
                                "submission_ns": None,
                            },
                            sort_keys=True,
                        ),
                    )
                )
            features = []
            if mover is not None:
                event = mover.observations["ts_event"]
                features = [
                    RankingFeature(key, getattr(mover, key), event, mover.known_at)
                    for key in ("move", "move_1m", "move_5m", "dvol_ratio", "trades_accel")
                ]
                features.extend(
                    RankingFeature(
                        key,
                        mover.observations[key],
                        mover.observations["last_price_ts"] if key == "price" else event,
                        mover.known_at,
                    )
                    for key in ("price", "dollar_volume", "spread", "move_from_open")
                )
            rows.append(
                RankingRow(
                    name,
                    score=mover.score if mover else None,
                    rank=mover.rank if mover else None,
                    features=tuple(features),
                    completed=ready,
                    partial=not ready,
                    focus_eligible=bool(mover and asset_ok and qualifies(mover)),
                    reasons=tuple(reasons),
                )
            )
        generation = RankingGeneration(
            generation_id=GenerationId(
                self.epoch, self.sequence, self.sequence - 1 if self.sequence else None
            ),
            session_date=self.segment.session_date,
            input_cutoff=self.input_cutoff,
            ts_event=self.last_event,
            ts_init=now,
            known_at=now,
            method_version=METHOD,
            profile_version="registry/" + self.registry.params_hash,
            ranker_version=RANKER,
            coverage=self.coverage
            if ready
            else replace(self.coverage, state=CoverageState.DEGRADED, complete=False),
            status=GenerationStatus.PROVISIONAL,
            rows=tuple(rows),
        )
        self.bus.publish(
            ranking_data_type(self.feed).topic, CustomData(ranking_data_type(self.feed), generation)
        )
        top = replace(
            generation,
            rows=tuple(r for r in generation.rows if r.rank is not None)[
                : int(self.registry.top_n)
            ],
            coverage=replace(
                generation.coverage, complete=False, reasons=(RankingReason("TOP_N_PROJECTION"),)
            ),
        )
        self.bus.publish(
            ranking_data_type(self.feed, "top").topic,
            CustomData(ranking_data_type(self.feed, "top"), top),
        )
        self.published_at = now
        self.sequence += 1
        if self.heartbeat is not None:
            self.heartbeat.emit(generation)
        return generation

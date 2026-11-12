"""No-fallback consumer, adapted from northstar-trading@8256c16c31dc:k3_reader.py.

Native heartbeat ingress supplies event and local receipt nanoseconds explicitly;
this fixture slice starts no external stream and manufactures no heartbeat.
"""

import json
import logging
from collections.abc import Callable

from nautilus_trader.model import CustomData

from sextant.ranking.engine import Segment
from sextant.ranking.generation import CoverageState, RankingGeneration, generation_digest
from sextant.ranking.publisher import METHOD, RANKER, ranking_data_type, universe_digest
from sextant.ranking.registry import Registry


class GenerationReader:
    def __init__(
        self,
        registry: Registry,
        *,
        feed: str,
        segment: Segment,
        universe_hash: str,
        baseline_hash: str,
        alarm: Callable[[str], None] | None = None,
    ) -> None:
        self.registry, self.feed, self.segment = registry, feed, segment
        self.universe_hash, self.baseline_hash = universe_hash, baseline_hash
        self._alarm = alarm or logging.getLogger(__name__).warning
        self._last_alarm: str | None = None
        self.generation: RankingGeneration | None = None
        self.reason = "generation_missing"
        self.generation_key: str | None = None
        self._heartbeat: tuple[int, int] | None = None
        self._epoch: str | None = None
        self._sequence: int | None = None
        self._retired_epochs: set[str] = set()

    def invalidate(self, reason: str) -> None:
        self.generation, self.generation_key = None, None
        self._refuse(reason)

    def _refuse(self, reason: str) -> None:
        self.reason = reason
        if self._last_alarm != reason:
            self._alarm(reason)
            self._last_alarm = reason

    def accept_heartbeat(self, *, at: int, received_ns: int) -> bool:
        if (
            type(at) is not int
            or type(received_ns) is not int
            or not 0 <= at <= received_ns
            or received_ns - at > self.registry.seconds("heartbeat_max_age_s")
            or (self._heartbeat is not None and at < self._heartbeat[0])
        ):
            self._heartbeat = None
            self._refuse("heartbeat_invalid")
            return False
        self._heartbeat = (at, received_ns)
        return True

    def fresh(self, now: int) -> bool:
        if self._heartbeat is None:
            self._refuse("heartbeat_missing")
            return False
        if any(
            not 0 <= now - at <= self.registry.seconds("heartbeat_max_age_s")
            for at in self._heartbeat
        ):
            self._refuse("heartbeat_stale")
            return False
        gen = self.generation
        if gen is None:
            self._refuse(self.reason)
            return False
        if not self.segment.start_ns <= now < self.segment.end_ns:
            self._refuse("session_mismatch")
            return False
        for reason, at, limit in (
            ("generation_stale", gen.known_at, "generation_max_age_s"),
            ("input_stale", gen.ts_event, "input_max_age_s"),
        ):
            if not 0 <= now - at <= self.registry.seconds(limit):
                self._refuse(reason)
                return False
        self.reason = "fresh"
        self._last_alarm = None
        return True

    def accept_generation(self, envelope: CustomData | None, *, now: int) -> bool:
        try:
            if not isinstance(envelope, CustomData) or envelope.data_type != ranking_data_type(
                self.feed
            ):
                raise ValueError("wrong generation topic")
            gen = envelope.data
            if not isinstance(gen, RankingGeneration):
                gen = RankingGeneration.from_json(gen.to_json())
            if (
                gen.ranker_version != RANKER
                or gen.method_version != METHOD
                or gen.profile_version != "registry/" + self.registry.params_hash
            ):
                raise ValueError("generation identity mismatch")
            cov = gen.coverage
            if not (
                cov.state is CoverageState.READY
                and cov.complete
                and cov.expected_symbols == cov.observed_symbols == len(gen.rows)
                and cov.missing_symbols == cov.invalid_symbols == cov.late_symbols == 0
            ):
                raise ValueError("generation incomplete")
            if not (
                self.segment.start_ns
                <= gen.ts_event
                <= gen.input_cutoff
                <= gen.known_at
                <= gen.ts_init
                <= now
                < self.segment.end_ns
                and gen.session_date == self.segment.session_date
            ):
                raise ValueError("generation session/future clocks")
            if now - gen.known_at > self.registry.seconds(
                "generation_max_age_s"
            ) or now - gen.ts_event > self.registry.seconds("input_max_age_s"):
                raise ValueError("generation/input stale")
            if universe_digest(tuple(row.symbol for row in gen.rows)) != self.universe_hash:
                raise ValueError("universe identity mismatch")
            first_source = None
            scored = []
            for row in gen.rows:
                sources = [r.detail for r in row.reasons if r.code == "SOURCE"]
                if len(sources) != 1 or sources[0] is None:
                    raise ValueError("source missing")
                source = json.loads(sources[0])
                expected = {
                    "feed": self.feed,
                    "segment": self.segment.name,
                    "start_ns": self.segment.start_ns,
                    "end_ns": self.segment.end_ns,
                    "session_source": self.segment.source,
                    "params_hash": self.registry.params_hash,
                    "universe_hash": self.universe_hash,
                    "baseline_hash": self.baseline_hash,
                }
                if (
                    any(source.get(k) != value for k, value in expected.items())
                    or not source.get("universe_source")
                    or not source.get("baseline_source")
                ):
                    raise ValueError("source/feed/session mismatch")
                if first_source is not None and source != first_source:
                    raise ValueError("mixed generation sources")
                first_source = source
                if any(
                    (f.value is not None and (f.known_at is None or f.ts_event is None))
                    or (
                        f.known_at is not None
                        and (
                            f.ts_event is None
                            or not self.segment.start_ns <= f.ts_event <= f.known_at <= gen.known_at
                        )
                    )
                    for f in row.features
                ):
                    raise ValueError("feature future or outside session")
                values = {f.name: f.value for f in row.features}
                if row.score is not None:
                    move, ratio, accel = (
                        values.get(k) for k in ("move", "dvol_ratio", "trades_accel")
                    )
                    if move is None or ratio is None or row.score != move * ratio:
                        raise ValueError("score/feature mismatch")
                    if accel is not None:
                        scored.append(row)
                elif row.rank is not None:
                    raise ValueError("rank without score")
                if row.focus_eligible is True:
                    price, move_1m, move, ratio, accel = (
                        values.get(k)
                        for k in ("price", "move_1m", "move", "dvol_ratio", "trades_accel")
                    )
                    if (
                        row.partial
                        or not row.completed
                        or row.rank is None
                        or price is None
                        or price <= 0
                        or move_1m is None
                        or move_1m <= 0
                        or move is None
                        or move < self.registry.mover_min_move
                        or ratio is None
                        or ratio < self.registry.mover_min_dvol_ratio
                        or accel is None
                        or accel < self.registry.mover_min_trades_accel
                    ):
                        raise ValueError("entry qualification incomplete")
            expected_ranks = {
                row.symbol: i
                for i, row in enumerate(
                    sorted(
                        scored,
                        key=lambda r: (
                            -r.score,
                            -next(f.value for f in r.features if f.name == "trades_accel"),
                            r.symbol,
                        ),
                    ),
                    1,
                )
            }
            if any(row.rank != expected_ranks.get(row.symbol) for row in gen.rows):
                raise ValueError("rank order mismatch")
            identity = gen.generation_id
            if identity.producer_epoch in self._retired_epochs:
                raise ValueError("retired producer epoch replay")
            if self._epoch is not None and identity.producer_epoch != self._epoch:
                self._retired_epochs.add(self._epoch)
                self._epoch, self._sequence = identity.producer_epoch, identity.sequence
                self._heartbeat = None
                raise ValueError(
                    "producer epoch changed; require coherent generation and new heartbeat"
                )
            if self._sequence is not None and (
                identity.sequence != self._sequence + 1
                or identity.previous_sequence != self._sequence
            ):
                self._sequence = max(self._sequence, identity.sequence)
                raise ValueError("generation sequence discontinuity")
            self._epoch, self._sequence = identity.producer_epoch, identity.sequence
            self.generation, self.generation_key = gen, generation_digest(gen)
            self.reason = "accepted"
            return True
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            self.invalidate(str(exc))
            return False

    def entry_rank(self, symbol: str, *, now: int) -> int | None:
        if not self.fresh(now) or self.generation is None:
            return None
        row = next((row for row in self.generation.rows if row.symbol == symbol), None)
        if row is None or row.partial or not row.completed or row.focus_eligible is not True:
            return None
        assets = [reason.detail for reason in row.reasons if reason.code == "ASSET"]
        try:
            asset = json.loads(assets[0]) if len(assets) == 1 and assets[0] is not None else {}
            if not (
                asset.get("source")
                and asset.get("halted") is False
                and asset.get("security_type") in {"common", "adr"}
                and 0 <= asset["known_at"] <= now < asset["expires_at"]
            ):
                return None
        except (ValueError, KeyError, TypeError):
            return None
        if any(
            f.ts_event is None
            or not 0 <= now - f.ts_event <= self.registry.seconds("input_max_age_s")
            for f in row.features
            if f.name in {"price", "move", "dvol_ratio", "trades_accel"}
        ):
            self._refuse("symbol_input_stale")
            return None
        return row.rank

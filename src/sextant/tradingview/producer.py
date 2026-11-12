"""Slot-2 offline live seam, adapted from tv-display-producer@ea9507545045.

Main owns ranking/validation; M2@abe8432 owns the two-clock interpretation.
ADR-0021 owns J5. No transport, keys, orders, controller or schedule exists here.
Sources, registry and carry/consolidation receipts: handbook/rank-live.md.
"""

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockSnapshotRequest
from nautilus_trader.model import CustomData, TradeTick

from sextant.data.alpaca_history import Pacer, symbol_name
from sextant.execution.custody import AccountScope, WriterLease
from sextant.execution.state import write_atomic
from sextant.ranking.generation import Coverage, CoverageState
from sextant.ranking.publisher import RankingPublisher, ranking_data_type
from sextant.ranking.reader import GenerationReader
from sextant.ranking.registry import RegistryRow
from sextant.tradingview.inputs import DISPLAY_PAPER_SLOT, QUALIFIED

SCOPE = AccountScope("alpaca-market-data", "paper-slot-2")
REST_DISPLAY_SCOPE = AccountScope("alpaca-rest-display", "paper-slot-2")


@contextmanager
def reserve_stream(
    root: Path, *, paper_slot: int, existing_streams: int, source: str
) -> Iterator[WriterLease]:
    """Reserve, never open; every host must use the same canonical lease root."""
    if type(paper_slot) is not int or paper_slot != 2:
        raise ValueError("only paper slot 2; slot 3 is revoked")
    if type(existing_streams) is not int or existing_streams != 0 or not source.strip():
        raise ValueError("sourced zero existing streams required; never a second stream")
    lease = WriterLease(SCOPE, root, "rank-live")
    lease.acquire()
    try:
        yield lease
    finally:
        lease.close()


@contextmanager
def reserve_rest_display(root: Path, *, paper_slot: int) -> Iterator[WriterLease]:
    """Serialize REST display producers under the canonical market-data lease root.

    This grants no stream admission; a stream opener must take reserve_stream.
    Build on custody.WriterLease's exclusive flock, with a distinct fixed scope.
    """
    if type(paper_slot) is not int or paper_slot != 2:
        raise ValueError("only paper slot 2; slot 3 is revoked")
    lease = WriterLease(REST_DISPLAY_SCOPE, root, "rest-display")
    lease.acquire()
    try:
        yield lease
    finally:
        lease.close()


@dataclass(frozen=True, slots=True)
class Clocks:
    MARKET_AVAILABLE_AT: int
    ACQUIRED_AT: int
    source: str

    def __post_init__(self) -> None:
        if (
            any(
                type(at) is not int or at < 0 for at in (self.MARKET_AVAILABLE_AT, self.ACQUIRED_AT)
            )
            or not self.source.strip()
        ):
            raise ValueError("explicit integer clocks and source required")


class LiveProducer:
    """Offline display seam; either reservation owns publication, neither opens a stream.

    Actual stream hosts must independently enter reserve_stream before client construction.
    """

    def __init__(
        self,
        publisher: RankingPublisher,
        lease: WriterLease,
        *,
        sip_authority: str = "",
        feed_authority: str = "",
    ) -> None:
        if (
            lease.scope not in {SCOPE, REST_DISPLAY_SCOPE}
            or not lease.held
            or not (feed_authority or sip_authority).strip()
            or publisher.feed != ("boats" if publisher.segment.name == "OVERNIGHT" else "sip")
            or (
                publisher.feed == "boats"
                and (lease.scope != REST_DISPLAY_SCOPE or not feed_authority.strip())
            )
            or publisher.sequence != 0
            or publisher.registry.top_n > 1000
        ):
            raise ValueError(
                "fresh display publisher, feed authority/entitlement source and held lease required"
            )
        self.publisher, self.lease, self.sip_authority = publisher, lease, sip_authority
        self.feed_authority = feed_authority or sip_authority
        self.reader = GenerationReader(
            publisher.registry,
            feed=publisher.feed,
            segment=publisher.segment,
            universe_hash=publisher.universe_hash,
            baseline_hash=publisher.baseline.identity,
        )
        self.provenance: dict[str, dict] = {}
        self.snapshot: dict | None = None
        self.error: str | None = None
        self.last_market = self.heartbeat_deadline = 0

    def invalidate(self, reason: str) -> None:
        """Gaps, disconnects, revisions and invalid data require a recovered epoch."""
        self.error = reason
        self.publisher.set_coverage(Coverage(CoverageState.DEGRADED, False))
        self.reader.invalidate(reason)

    def _admit(self, clocks: Clocks, now: int) -> bool:
        if self.error or not self.lease.held:
            raise ValueError("publisher invalid or display lease released")
        if type(now) is not int or not (
            self.publisher.segment.start_ns <= now < self.publisher.segment.end_ns
        ):
            raise ValueError("explicit clock inside supplied session required")
        return clocks.MARKET_AVAILABLE_AT <= now

    def on_trade(
        self,
        tick: TradeTick,
        *,
        clocks: Clocks,
        now: int,
        price_eligible: bool,
        volume_eligible: bool,
        conditions: tuple[str, ...],
        mask_source: str,
    ) -> bool:
        try:
            if not self._admit(clocks, now):
                return False
            if (
                not mask_source.strip()
                or type(price_eligible) is not bool
                or type(volume_eligible) is not bool
                or not isinstance(conditions, tuple)
                or any(not isinstance(c, str) or not c for c in conditions)
                or not tick.ts_event <= clocks.MARKET_AVAILABLE_AT
                or clocks.MARKET_AVAILABLE_AT < self.last_market
            ):
                raise ValueError("invalid market clocks or condition authority")
            view = TradeTick(
                tick.instrument_id,
                tick.price,
                tick.size,
                tick.aggressor_side,
                tick.trade_id,
                tick.ts_event,
                clocks.MARKET_AVAILABLE_AT,
            )
            excluded = bool({"G", "U", "Z", "6", "M", "9"}.intersection(conditions))
            self.publisher.on_trade(
                view,
                known_at=clocks.MARKET_AVAILABLE_AT,
                conditions=conditions,
                price_eligible=price_eligible and not excluded,
                volume_eligible=volume_eligible and not excluded,
            )
            self.last_market = clocks.MARKET_AVAILABLE_AT
            self.provenance[str(tick.instrument_id.symbol)] = {
                **asdict(clocks),
                "native_ts_init": tick.ts_init,
                "mask_source": mask_source,
            }
            return True
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.invalidate("invalid_trade; recover a new epoch")
            raise

    def on_snapshot(self, response_json: str, *, clocks: Clocks, now: int) -> bool:
        """Raw diagnostics only: aggregate/latest trades cannot seed tick history."""
        try:
            if self.publisher.feed != "sip":
                raise ValueError("snapshot diagnostics are SIP-only")
            if not self._admit(clocks, now):
                return False
            payload = json.loads(response_json, parse_float=Decimal, parse_constant=_invalid_number)
            if (
                not isinstance(payload, dict)
                or not payload
                or not payload.keys() <= set(self.publisher.universe)
                or not all(isinstance(row, dict) for row in payload.values())
                or (
                    self.snapshot
                    and clocks.MARKET_AVAILABLE_AT < self.snapshot["MARKET_AVAILABLE_AT"]
                )
            ):
                raise ValueError("invalid or regressed snapshot")
            self.snapshot = {**asdict(clocks), "response_json": response_json}
            return True
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.invalidate("invalid_snapshot; recover a new epoch")
            raise

    def heartbeat(self, *, at: int, received_ns: int) -> bool:
        accepted = self.lease.held and self.reader.accept_heartbeat(at=at, received_ns=received_ns)
        self.heartbeat_deadline = (
            at + self.publisher.registry.seconds("heartbeat_max_age_s") if accepted else 0
        )
        return accepted

    def build_unit(
        self, *, now: int, symbol_map: Mapping[str, str | None], mapping_clocks: Clocks
    ) -> dict:
        common = {
            "schema": "sextant.j5-ranked/1",
            "display_only": True,
            "paper_slot": DISPLAY_PAPER_SLOT,
        }
        try:
            if not self._admit(mapping_clocks, now):
                raise ValueError("mapping unavailable")
            record = self.publisher.publish(now=now)
            if record is not None:
                self.reader.accept_generation(
                    CustomData(ranking_data_type(self.publisher.feed), record), now=now
                )
            record = self.reader.generation
            if not self.reader.fresh(now) or record is None:
                raise ValueError(self.reader.reason)
            expires = min(
                record.known_at + self.publisher.registry.seconds("generation_max_age_s"),
                record.ts_event + self.publisher.registry.seconds("input_max_age_s"),
                self.heartbeat_deadline,
                self.publisher.segment.end_ns,
            )
            if now >= expires or mapping_clocks.MARKET_AVAILABLE_AT > record.known_at:
                raise ValueError("expired generation or future mapping")
            selected = [r for r in record.rows if r.rank is not None][
                : int(self.publisher.registry.top_n)
            ]
            if any(row.partial or not row.completed for row in selected):
                raise ValueError("partial display row")
            if any(
                f.ts_event is None
                or f.value is None
                or not 0 <= now - f.ts_event <= self.publisher.registry.seconds("input_max_age_s")
                for row in selected
                for f in row.features
                if f.name in {"price", "move", "dvol_ratio", "trades_accel"}
            ):
                raise ValueError("projected symbol input stale")
            members = [symbol_map.get(row.symbol) for row in selected]
            if (
                not members
                or any(
                    not isinstance(member, str)
                    or not QUALIFIED.fullmatch(member)
                    or member.split(":", 1)[0] in {"SMART", "IB", "IBKR", "ALPACA"}
                    for member in members
                )
                or len(set(members)) != len(members)
            ):
                raise ValueError("empty ranking or missing/ambiguous exact mapping")
            return {
                **common,
                "status": "fresh",
                "generation_sha256": self.reader.generation_key,
                "params_hash": self.publisher.registry.params_hash,
                "generated_at_ns": record.known_at,
                "expires_at_ns": expires,
                "ranking_generation": record.to_dict(),
                "watchlists": {"NS Ranked": members},
                "provenance": {
                    "sip_authority": self.sip_authority,
                    "feed_authority": self.feed_authority,
                    "trades": self.provenance.copy(),
                    "snapshot": self.snapshot,
                    "mapping": asdict(mapping_clocks),
                },
            }
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            return {**common, "status": "unavailable", "watchlists": None, "reason": str(exc)}

    def publish_unit(self, path: Path, **inputs) -> dict:
        """One durable complete document replaces the carried multi-file assembler."""
        unit = self.build_unit(**inputs)
        write_atomic(path, unit)
        return unit


def _invalid_number(value: str) -> None:
    raise ValueError("nonfinite snapshot number")


def snapshot_pacer(
    lease: WriterLease,
    row: RegistryRow,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Pacer:
    if (
        row.name != "snapshot_requests_per_minute"
        or row.unit != "requests/minute"
        or type(row.value) is not int
        or not 0 < row.value <= 200
        or not row.source.strip()
        or row.calibration_state != "UNCALIBRATED"
        or type(row.last_change) is not date
        or lease.scope != SCOPE
    ):
        raise ValueError("sourced UNCALIBRATED shared snapshot allocation <=200 required")
    pacer = Pacer(Decimal(row.value), clock=clock, sleep=sleep)
    lease.bind_pacer(pacer)
    return pacer


def snapshot_request(lease: WriterLease, symbols: tuple[str, ...]) -> StockSnapshotRequest:
    """Describe one paced request, never send it; retries must use this path too."""
    if not lease.held or not isinstance(lease.pacer, Pacer):
        raise ValueError("held stream lease and shared pacer required")
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("explicit unique snapshot group required")
    request = StockSnapshotRequest(
        symbol_or_symbols=[symbol_name(symbol) for symbol in symbols], feed=DataFeed.SIP
    )
    lease.pacer.wait()
    return request

"""N2b-2a (LEAD-0115 6): per-segment publishers, coverage by observation, the heartbeat.

A real BacktestNode replays fixture PENNY prints at 0, 60, ... 360 s into a RankingActor whose
host is the SegmentedHost; a RankingDataActor reads the node bus and the heartbeat signal.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from nautilus_trader.model import CustomData, MarketStatusAction
from test_ranking import NS, SCOPE, START, baseline, registry
from test_ranking_actor import IDS, NAMES, Logged, run
from test_ranking_actor_status import status

from sextant.alpaca.stream_facts import AlpacaStreamGap, StreamFacts, gap_type
from sextant.ranking.engine import ReferencePrices, Segment
from sextant.ranking.generation import CoverageState
from sextant.ranking.node_session import SegmentedHost, SegmentInputs
from sextant.ranking.publisher import AssetState

AT = START + 360 * NS
REFS = ReferencePrices(D("0.50"), START - NS, "fixture official close")


class Regular(StreamFacts):
    """Fixture: every catalog print is a regular print (the catalog carries no conditions)."""

    def __call__(self, tick) -> tuple[bool, tuple[str, ...]]:
        return True, ("@",)

    def vendor(self, tick):  # the catalog prints are the vendor's own values (N1f)
        return tick.price.as_decimal(), tick.size.as_decimal()


def inputs(segment: Segment) -> SegmentInputs:
    asset = AssetState(START - NS, segment.end_ns, None, "common", "fixture vendor")
    return SegmentInputs(
        baseline=baseline(),
        references=dict.fromkeys(NAMES, REFS),
        assets=dict.fromkeys(NAMES, asset),
    )


def session_run(tmp_path: Path, *, segments=lambda ts: SCOPE, source=inputs, **kwargs: Any):
    clocks: list[Any] = []
    actors: list[Any] = []
    sessions: list[SegmentedHost] = []

    class Clocked(kwargs.pop("actor_cls", Logged)):  # type: ignore[misc]
        def on_start(self) -> None:
            clocks.append(self.clock)
            actors.append(self)
            super().on_start()

    def build() -> SegmentedHost:
        sessions.append(
            SegmentedHost(
                feed="sip",
                universe=NAMES,
                universe_source="fixture selected symbols; NOT PIT universe",
                registry=registry(),
                inputs=source,
                segment_of=segments,
                clock=clocks[0].timestamp_ns,
                receipt_dir=tmp_path / "receipts",
                evidence=lambda: actors[0].evidence(),
            )
        )
        return sessions[-1]

    out = run(
        tmp_path / "node",
        factory=build,
        prime=lambda: sessions[-1].actor_priming(),
        actor_cls=Clocked,
        facts=kwargs.pop("facts", Regular("sip")),
        heartbeat=kwargs.pop("heartbeat", registry()),
        **kwargs,
    )
    out["session"] = sessions[0]
    return out


def receipt(tmp_path: Path, name: str = "PRE") -> dict[str, Any]:
    return json.loads((tmp_path / "receipts" / f"ranking-2026-10-27-{name}.json").read_text())


def test_a_segment_held_from_before_its_start_ranks_and_beats(tmp_path) -> None:
    out = session_run(tmp_path)
    last = out["receiver"].received[-1]
    assert last.coverage.state is CoverageState.READY
    consumer = out["consumer"]
    assert consumer.entry_rank("PENNY", now=AT) == 1  # a regular print cleared PENNY
    assert consumer.entry_rank("ADR", now=AT) is None  # untraded: unknown halt state
    body = receipt(tmp_path)
    assert body["observed"] is True and body["gaps"] == 0 and body["names_ranked"] == 1
    assert body["names_unknown"] == 2 and body["names_halted"] == 0
    assert body["generations"] == out["actor"].published


def test_the_receipt_says_how_names_left_unknown(tmp_path) -> None:
    trading = status(30, MarketStatusAction.TRADING, iid=IDS[0], is_trading=True)
    session_run(tmp_path, statuses=[trading])
    assert receipt(tmp_path)["cleared"] == {"cleared_by_print": 1, "cleared_by_status": 1}


def test_a_halted_name_has_no_entry_rank(tmp_path) -> None:
    out = session_run(tmp_path, statuses=[status(355, MarketStatusAction.HALT)])
    assert out["consumer"].entry_rank("PENNY", now=AT) is None
    assert receipt(tmp_path)["names_halted"] == 1


def test_a_mid_segment_attach_covers_nothing_and_says_so(tmp_path) -> None:
    started = replace(SCOPE, start_ns=START - 600 * NS)  # the node started inside it
    out = session_run(tmp_path, segments=lambda ts: started)
    assert all(g.coverage.state is CoverageState.DEGRADED for g in out["receiver"].received)
    assert all(row.rank is None for g in out["receiver"].received for row in g.rows)
    assert out["consumer"].entry_rank("PENNY", now=AT) is None
    body = receipt(tmp_path)
    assert body["observed"] is False and body["names_ranked"] == 0


def test_the_session_rolls_over_to_a_fresh_publisher_at_the_boundary(tmp_path) -> None:
    pre = replace(SCOPE, end_ns=START + 305 * NS)
    rth = Segment("RTH", SCOPE.session_date, START + 305 * NS, SCOPE.end_ns, "fixture")
    out = session_run(tmp_path, segments=lambda ts: pre if ts < pre.end_ns else rth)
    epochs = [g.generation_id.producer_epoch for g in out["receiver"].received]
    assert epochs[0] == "2026-10-27-PRE" and epochs[-1] == "2026-10-27-RTH"
    assert epochs == sorted(epochs, key=lambda e: e.endswith("RTH"))  # never back to PRE
    assert receipt(tmp_path, "PRE")["generations"] >= 1
    later = receipt(tmp_path, "RTH")
    assert later["observed"] is True and later["late"] == 0
    assert out["actor"].errors == {}  # the lineage guard accepted the new epoch
    assert out["counter"].seen == 12  # building a publisher mid-run stops no delivery


def test_a_gap_degrades_the_segment_for_the_rest_of_it(tmp_path) -> None:
    class Gapped(Logged):
        def watch_statuses(self) -> None:
            super().watch_statuses()

            def gap(_event) -> None:
                now = self.clock.timestamp_ns()
                self.on_data(CustomData(gap_type("sip"), AlpacaStreamGap("sip", 2, now, now)))

            self.clock.set_time_alert_ns("gap", START + 200 * NS, gap)

    out = session_run(tmp_path, actor_cls=Gapped)
    after = [g for g in out["receiver"].received if g.known_at >= START + 200 * NS]
    assert after and all(g.coverage.state is CoverageState.DEGRADED for g in after)
    assert out["consumer"].entry_rank("PENNY", now=AT) is None
    body = receipt(tmp_path)
    assert body["gaps"] == 1 and body["names_ranked"] == 0


def test_a_segment_without_inputs_publishes_nothing_and_counts_its_ticks(tmp_path) -> None:
    out = session_run(tmp_path, source=lambda segment: None)
    assert out["receiver"].received == [] and out["actor"].errors == {}
    body = receipt(tmp_path)
    assert body["primed"] is False and body["unprimed"] == 12


def test_ticks_outside_any_rankable_segment_are_counted_not_ranked(tmp_path) -> None:
    out = session_run(tmp_path, segments=lambda ts: None)
    assert out["receiver"].received == [] and out["session"].unrouted == 12

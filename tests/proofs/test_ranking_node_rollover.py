"""N2b-4: the segment rollover runs on the node clock, not only on the next tick.

Fixture PENNY prints arrive at 0, 60, ... 360 s on a real BacktestNode; the node's alert at
each segment's end rolls the SegmentedHost to the next segment's publisher.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
from nautilus_trader.model import CustomData
from test_ranking import NS, SCOPE, START, baseline, registry
from test_ranking_actor import NAMES
from test_ranking_node_session import session_run

from sextant.paper.sessions import ET
from sextant.ranking.baseline import closed_until, segment_start
from sextant.ranking.consumer import RankingConsumer
from sextant.ranking.engine import Segment
from sextant.ranking.heartbeat import HeartbeatEmitter
from sextant.ranking.node_session import SegmentedHost
from sextant.ranking.publisher import ranking_data_type, universe_digest


def two_segments(boundary_s: int) -> tuple[Segment, Segment]:
    first = replace(SCOPE, end_ns=START + boundary_s * NS)
    second = Segment("RTH", SCOPE.session_date, first.end_ns, SCOPE.end_ns, "fixture")
    return first, second


def epochs(out: dict[str, Any]) -> list[str]:
    return [g.generation_id.producer_epoch for g in out["receiver"].received]


def test_a_boundary_with_no_tick_still_rolls_over(tmp_path) -> None:
    first, second = two_segments(361)  # a second after the last print at 360 s
    out = session_run(
        tmp_path,
        segments=lambda ts: first if ts < first.end_ns else second,
        end=START + 400 * NS,
    )
    session: SegmentedHost = out["session"]
    assert session.segment == second  # rolled by the clock: no tick arrived after 360 s
    assert epochs(out)[-1] == "2026-10-27-RTH"
    rolled = [g for g in out["receiver"].received if g.generation_id.producer_epoch.endswith("RTH")]
    assert rolled[0].known_at == second.start_ns  # published at the boundary itself
    receipt = tmp_path / "receipts" / "ranking-2026-10-27-PRE.json"
    assert json.loads(receipt.read_text())["generations"] >= 1


def test_no_generation_of_the_old_segment_is_readable_after_the_boundary(tmp_path) -> None:
    first, second = two_segments(361)
    out = session_run(
        tmp_path,
        segments=lambda ts: first if ts < first.end_ns else second,
        end=START + 400 * NS,
    )
    consumer = RankingConsumer(  # B's consumer for the first segment, fed the node's bus
        registry=registry(),
        feed="sip",
        segment=first,
        universe_hash=universe_digest(NAMES),
        baseline_hash=baseline().identity,
        entry_heartbeat_max_age_s=10,
        entry_generation_max_age_s=10,
        entry_input_max_age_s=10,
    )
    for generation in out["receiver"].received:
        if not generation.generation_id.producer_epoch.endswith("PRE"):
            continue
        now = generation.known_at
        consumer.handle(CustomData(ranking_data_type("sip"), generation), now=now)
        HeartbeatEmitter(
            registry=registry(), clock=lambda now=now: now, sink=consumer.accept_heartbeat
        ).emit(generation)
    assert consumer.entry_rank("PENNY", now=first.end_ns - NS // 2) == 1
    assert consumer.entry_rank("PENNY", now=first.end_ns) is None
    assert consumer.ranked_names(now=first.end_ns) == ()
    fresh = RankingConsumer(  # the next segment's consumer has its generation at the boundary
        registry=registry(),
        feed="sip",
        segment=second,
        universe_hash=universe_digest(NAMES),
        baseline_hash=baseline().identity,
        entry_heartbeat_max_age_s=10,
        entry_generation_max_age_s=10,
        entry_input_max_age_s=10,
    )
    for generation in out["receiver"].received:
        if generation.generation_id.producer_epoch.endswith("RTH"):
            fresh.handle(CustomData(ranking_data_type("sip"), generation), now=generation.known_at)
    assert fresh.reader.generation is not None
    assert fresh.reader.generation.known_at == second.start_ns


def test_a_tick_at_the_boundary_goes_to_the_new_segment_once(tmp_path) -> None:
    first, second = two_segments(300)  # a print lands exactly at 300 s
    entered: list[tuple[str, Any]] = []
    real = SegmentedHost._enter

    def recording(self, segment):
        generation = real(self, segment)
        entered.append((segment.name, self.host))
        return generation

    SegmentedHost._enter = recording  # type: ignore[method-assign]
    try:
        out = session_run(tmp_path, segments=lambda ts: first if ts < first.end_ns else second)
    finally:
        SegmentedHost._enter = real  # type: ignore[method-assign]
    assert [name for name, _ in entered] == ["PRE", "RTH"]  # each segment entered once
    pre, rth = (host.publisher for _, host in entered)
    assert pre.last_event == START + 240 * NS  # the 300 s print never reached the old segment
    assert rth.states["PENNY"].latest is not None and out["session"].late == 0


@pytest.mark.parametrize(
    ("moment", "until"),
    [
        ((2026, 11, 13, 20, 30), (2026, 11, 15, 20)),  # Friday night: Sunday's overnight
        ((2026, 11, 14, 21, 0), (2026, 11, 15, 20)),  # Saturday night
        ((2026, 11, 27, 17, 30), (2026, 11, 29, 20)),  # the early-close gap before a weekend
        ((2026, 12, 25, 12, 0), (2026, 12, 27, 20)),  # a holiday into a weekend
    ],
)
def test_every_closed_interval_ends_at_the_next_overnight_start(moment, until) -> None:
    def at(parts: tuple[int, ...]) -> int:
        return int(datetime(*parts, tzinfo=ET).timestamp()) * NS

    assert segment_start(at(moment))[0] is None
    assert closed_until(at(moment)) == at(until)
    following = segment_start(at(until))[0]
    assert following is not None and following.start_ns == at(until)

"""N2b-2a fix round (Codex xhigh r1, P1): a gap at a new segment's boundary degrades that segment.

The reviewer's witnesses (review-gpt-gates/n2b-7b75642-codex-r1, test_review_witnesses.py:96
and :334) enter the new segment by the node-clock turn, which arrives in N2b-4; here the
boundary tick enters it, the only entry this part has. Helpers are the reviewer's, unchanged.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model import TradeTick
from test_ranking import NS, SCOPE, START, registry
from test_ranking_actor import NAMES, ticks
from test_ranking_node_session import inputs as fixture_inputs

from sextant.ranking.engine import ReferencePrices, Segment
from sextant.ranking.generation import Coverage, CoverageState
from sextant.ranking.node_session import SegmentedHost
from sextant.ranking.publisher import AssetState


def tick_at(timestamp: int) -> TradeTick:
    original = ticks()[-1]
    return TradeTick(
        original.instrument_id,
        original.price,
        original.size,
        original.aggressor_side,
        original.trade_id,
        timestamp,
        timestamp,
    )


def host_at(tmp_path: Path, segment_of, source=fixture_inputs):
    clock = [START - NS]
    host = SegmentedHost(
        feed="sip",
        universe=NAMES,
        universe_source="offline review fixtures only",
        registry=registry(),
        inputs=source,
        segment_of=segment_of,
        clock=lambda: clock[0],
        receipt_dir=tmp_path,
    )
    return host, clock


def pair():
    first = replace(SCOPE, end_ns=START + 300 * NS)
    second = Segment("RTH", first.session_date, first.end_ns, SCOPE.end_ns, "fixture")
    return first, second


@pytest.mark.parametrize("gap_first", [False, True])
def test_boundary_gap_degrades_new_segment_in_either_order(tmp_path, gap_first):
    first, second = pair()
    host, clock = host_at(tmp_path, lambda timestamp: first if timestamp < first.end_ns else second)
    clock[0] = START
    host.on_trade(tick_at(START), facts=(True, ("@",)))
    clock[0] = second.start_ns
    if gap_first:
        host.set_coverage(Coverage(CoverageState.DEGRADED, False))
    host.on_trade(tick_at(clock[0]), facts=(True, ("@",)))
    if not gap_first:
        host.set_coverage(Coverage(CoverageState.DEGRADED, False))
    host.close()
    receipt = json.loads((tmp_path / "ranking-2026-10-27-RTH.json").read_text())
    previous = json.loads((tmp_path / "ranking-2026-10-27-PRE.json").read_text())
    assert host.observed is False, receipt
    assert host.host is not None and host.host.supplied.state is CoverageState.DEGRADED, receipt
    assert receipt["gaps"] == 1, receipt
    assert previous["gaps"] == 0 and previous["observed"] is True, previous


def test_a_gap_before_the_boundary_stays_with_the_old_segment(tmp_path):
    first, second = pair()
    host, clock = host_at(tmp_path, lambda timestamp: first if timestamp < first.end_ns else second)
    clock[0] = START
    host.on_trade(tick_at(START), facts=(True, ("@",)))
    clock[0] = second.start_ns - 1
    host.set_coverage(Coverage(CoverageState.DEGRADED, False))
    clock[0] = second.start_ns
    host.on_trade(tick_at(clock[0]), facts=(True, ("@",)))
    host.close()
    assert host.observed is True and host.gaps == 0
    previous = json.loads((tmp_path / "ranking-2026-10-27-PRE.json").read_text())
    assert previous["gaps"] == 1 and previous["observed"] is False


def test_boundary_gap_cannot_yield_a_rank(tmp_path):
    first, second = pair()

    def source(segment):
        original = fixture_inputs(segment)
        table = replace(
            original.baseline,
            samples=tuple(
                replace(sample, segment=segment.name) for sample in original.baseline.samples
            ),
        )
        return replace(original, baseline=table)

    host, clock = host_at(
        tmp_path, lambda timestamp: first if timestamp < first.end_ns else second, source
    )
    clock[0] = START
    host.on_trade(tick_at(START), facts=(True, ("@",)))
    clock[0] = second.start_ns
    host.set_coverage(Coverage(CoverageState.DEGRADED, False))
    last = None
    shift = second.start_ns - START
    for index, original in enumerate(ticks()):
        trade = TradeTick(
            original.instrument_id,
            original.price,
            original.size,
            original.aggressor_side,
            original.trade_id,
            original.ts_event + shift,
            original.ts_init + shift,
        )
        clock[0] = trade.ts_init
        last = host.on_trade(trade, facts=(True, ("@",))) or last
        if index == 0:
            host.set_symbol(
                "PENNY",
                references=ReferencePrices(None, 0, "ignored"),
                asset=AssetState(second.start_ns, second.end_ns, False, "common", "fixture"),
            )
    assert last is not None
    assert all(row.rank is None for row in last.rows), [
        (row.symbol, row.rank, row.score) for row in last.rows
    ]


@pytest.mark.parametrize("path", ["gap-notice", "socket-disconnect"])
def test_a_gap_at_the_boundary_through_the_actor_is_one_rule(tmp_path, path) -> None:
    """N1e's single gap path (DEGRADED first, then every name unknown; #531, #537) and this
    part's timing rule together: a gap the Actor receives at the next segment's start leaves
    that segment DEGRADED with its gap counted, and no READY rank is published after it."""
    from nautilus_trader.common import SocketState, SocketStateChanged
    from nautilus_trader.core import UUID4
    from nautilus_trader.model import ClientId, CustomData, TraderId
    from test_ranking_actor import Logged
    from test_ranking_node_session import session_run

    from sextant.alpaca.stream_facts import AlpacaStreamGap, gap_type

    pre = replace(SCOPE, end_ns=START + 305 * NS)
    rth = Segment("RTH", SCOPE.session_date, pre.end_ns, SCOPE.end_ns, "fixture")
    published: list[tuple[int, Any]] = []

    class Gapped(Logged):
        def watch_statuses(self) -> None:
            super().watch_statuses()
            self.clock.set_time_alert_ns("boundary-gap", rth.start_ns, self.deliver)

        def deliver(self, _event) -> None:
            now = self.clock.timestamp_ns()
            if path == "gap-notice":
                notice = AlpacaStreamGap("sip", 2, now, now)
                self.on_data(CustomData(gap_type("sip"), notice))
            else:
                trader, client = TraderId.from_str("TRADER-001"), ClientId("ALPACA")
                state = SocketState.DISCONNECTED
                event = SocketStateChanged(
                    trader, client, None, "fixture", state, UUID4(), now, now
                )
                self.on_socket_state(event)

        def publish_data(self, data_type, data) -> None:
            published.append((self.clock.timestamp_ns(), data.data))
            super().publish_data(data_type, data)

    out = session_run(
        tmp_path, segments=lambda ts: pre if ts < pre.end_ns else rth, actor_cls=Gapped
    )
    assert out["actor"].errors == {} and out["actor"].resets == 1
    after = [g for at, g in published if at >= rth.start_ns]
    assert after and all(row.rank is None for g in after for row in g.rows)
    later = json.loads((tmp_path / "receipts" / "ranking-2026-10-27-RTH.json").read_text())
    assert later["gaps"] == 1 and later["observed"] is False

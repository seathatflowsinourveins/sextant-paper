"""TV-BRIDGE proofs: pure j5-ranked/1 -> ns-display-lists/1 projection.

The real LiveProducer unit is the oracle input; `read_research_inputs` and
`sync_research` against the SDK in-memory Peer are the independent consumers.
Fixture bounds exercise mechanisms only and grant no operating authority.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import Client
from test_ranking import DAY, NS, START, publisher
from test_tradingview import Peer
from test_tradingview_producer import AT, warm
from test_tradingview_producer import unit as j5_unit

from sextant.ranking.generation import (
    Coverage,
    CoverageState,
    GenerationId,
    GenerationStatus,
    RankingFeature,
    RankingGeneration,
    RankingReason,
    RankingRow,
    generation_digest,
)
from sextant.ranking.publisher import METHOD, RANKER, universe_digest
from sextant.tradingview.bridge import BridgeError, DisplayBounds, project_display_unit
from sextant.tradingview.inputs import _ns, read_research_inputs, timestamp
from sextant.tradingview.producer import LiveProducer, reserve_stream
from sextant.tradingview.sync import OfficialTools, sync_research

BOUNDS = DisplayBounds(Decimal("0.05"), Decimal("2"), 600)


def fresh_j5(tmp_path: Path) -> dict:
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture stop receipt",
    ) as lease:
        live = LiveProducer(publisher(), lease, sip_authority="fixture SIP entitlement only")
        warm(live)
        assert live.heartbeat(at=AT, received_ns=AT)
        result = j5_unit(live)
    assert result["status"] == "fresh", result
    return result


def write_unit(tmp_path: Path, projected) -> Path:
    unit_dir = tmp_path / "unit"
    unit_dir.mkdir(exist_ok=True)
    for name, doc in projected.documents().items():
        (unit_dir / name).write_text(json.dumps(doc))
    return unit_dir


def read_now(projected):
    return timestamp(projected.display_lists["generated_at"]) + timedelta(seconds=1)


def rebuild_generation(j5: dict, **changes) -> dict:
    from dataclasses import replace

    result = copy.deepcopy(j5)
    generation = RankingGeneration.from_json(result["ranking_generation"])
    generation = replace(generation, **changes)
    result["ranking_generation"] = generation.to_dict()
    result["generation_sha256"] = generation_digest(generation)
    return result


def test_real_producer_projection_is_accepted_by_research_inputs(tmp_path: Path):
    j5 = fresh_j5(tmp_path)
    projected = project_display_unit(j5, bounds=BOUNDS)
    for doc in projected.documents().values():
        assert json.loads(json.dumps(doc)) == doc
    generation = RankingGeneration.from_json(j5["ranking_generation"])
    display = projected.display_lists
    assert _ns(display["as_of_snapshot"]) == generation.ts_event
    assert _ns(display["as_of"]) == generation.input_cutoff
    assert _ns(display["generated_at"]) == generation.known_at == j5["generated_at_ns"]
    assert display["session_date"] == DAY.isoformat()
    assert display["generation"] == {
        "as_of": display["as_of_snapshot"],
        "sha256": j5["generation_sha256"],
    }
    assert [row["tradingview_symbol"] for row in display["ns_movers"]] == j5["watchlists"][
        "NS Ranked"
    ]
    assert [row["symbol"] for row in display["ns_movers"]] == ["ADR", "LARGE"]
    assert projected.top100["candidates"] == display["ns_movers"]
    assert projected.focus["candidates"] == display["ns_focus"] == []
    assert display["counts"] == {"ns_movers": 2, "ns_focus": 0}
    assert display["excluded_unknown"] == 0  # PENNY is ranked but beyond top_n.
    data = read_research_inputs(write_unit(tmp_path, projected), now=read_now(projected))
    assert data.memberships["NS movers"] == frozenset(j5["watchlists"]["NS Ranked"])
    assert data.memberships["NS focus"] == frozenset()
    assert data.unmapped == frozenset()
    assert data.generation == display["generation"]


def test_projection_syncs_through_sdk_in_memory_server(tmp_path: Path):
    projected = project_display_unit(fresh_j5(tmp_path), bounds=BOUNDS)
    now = read_now(projected)
    data = read_research_inputs(write_unit(tmp_path, projected), now=now)
    peer = Peer()

    async def run():
        async with Client(peer.server) as client:
            tools = OfficialTools(client, interval=0, now=lambda: now)
            first = await sync_research(tools, data, apply=True)
            second = await sync_research(tools, data, apply=True)
            return first, second

    first, second = asyncio.run(run())
    assert first["applied"] is True and second["operations"] == []
    assert sorted(peer.lists["1"]["symbols"]) == ["NASDAQ:ADR", "NASDAQ:LARGE"]
    assert peer.lists["2"]["symbols"] == []
    assert peer.lists["3"]["symbols"] == ["NYSE:IBM"]  # Owner list untouched.
    assert first["readback"]["NS movers"]["stored"] == ["NASDAQ:ADR", "NASDAQ:LARGE"]


def test_focus_is_exactly_focus_eligible_paired_rows(tmp_path: Path):
    from dataclasses import replace

    j5 = fresh_j5(tmp_path)
    generation = RankingGeneration.from_json(j5["ranking_generation"])
    rows = tuple(
        replace(row, focus_eligible=True) if row.symbol == "LARGE" else row
        for row in generation.rows
    )
    j5 = rebuild_generation(j5, rows=rows)
    projected = project_display_unit(j5, bounds=BOUNDS)
    assert [row["symbol"] for row in projected.display_lists["ns_focus"]] == ["LARGE"]
    data = read_research_inputs(write_unit(tmp_path, projected), now=read_now(projected))
    assert data.memberships["NS focus"] == frozenset({"NASDAQ:LARGE"})
    assert data.memberships["NS movers"] == frozenset({"NASDAQ:ADR", "NASDAQ:LARGE"})


def test_unavailable_j5_projects_to_unavailable_never_empty_lists():
    unavailable = {
        "schema": "sextant.j5-ranked/1",
        "display_only": True,
        "paper_slot": 2,
        "status": "unavailable",
        "watchlists": None,
        "reason": "fixture refusal",
    }
    with pytest.raises(BridgeError):
        project_display_unit(unavailable, bounds=BOUNDS)


@pytest.mark.parametrize(
    "tamper",
    [
        "digest",
        "params",
        "rank",
        "score",
        "partial",
        "banned",
        "alias",
        "extra_member",
        "expires",
        "lag",
        "coverage",
        "revoked_slot",
    ],
)
def test_tampered_j5_is_unavailable(tmp_path: Path, tamper: str):
    from dataclasses import replace

    j5 = fresh_j5(tmp_path)
    generation = RankingGeneration.from_json(j5["ranking_generation"])
    if tamper == "digest":
        j5["generation_sha256"] = "b" * 64
    elif tamper == "params":
        j5["params_hash"] = "c" * 64
    elif tamper == "rank":
        rows = list(generation.rows)
        rows[0], rows[1] = replace(rows[0], rank=2), replace(rows[1], rank=1)
        j5 = rebuild_generation(j5, rows=tuple(rows))
    elif tamper == "score":
        rows = (replace(generation.rows[0], score=Decimal("999")), *generation.rows[1:])
        j5 = rebuild_generation(j5, rows=rows)
    elif tamper == "partial":
        rows = (replace(generation.rows[0], partial=True), *generation.rows[1:])
        j5 = rebuild_generation(j5, rows=rows)
    elif tamper == "banned":
        j5["watchlists"] = {"NS Ranked": ["ALPACA:ADR", "NASDAQ:LARGE"]}
    elif tamper == "alias":
        j5["watchlists"] = {"NS Ranked": ["NASDAQ:ADR", "NASDAQ:ADR"]}
    elif tamper == "extra_member":
        j5["watchlists"] = {
            "NS Ranked": [*j5["watchlists"]["NS Ranked"], "NASDAQ:ZZZ", "NASDAQ:YYY"]
        }
    elif tamper == "expires":
        j5["expires_at_ns"] = j5["generated_at_ns"]
    elif tamper == "revoked_slot":
        j5["paper_slot"] = 3
    elif tamper == "lag":
        cutoff = generation.ts_event + 601 * NS
        j5 = rebuild_generation(j5, input_cutoff=cutoff, known_at=cutoff, ts_init=cutoff)
        j5["generated_at_ns"] = cutoff
        j5["expires_at_ns"] = cutoff + 10 * NS
    else:
        j5 = rebuild_generation(j5, coverage=replace(generation.coverage, complete=False))
    with pytest.raises(BridgeError):
        project_display_unit(j5, bounds=BOUNDS)


def synthetic_j5(count: int) -> dict:
    symbols = tuple(f"S{index:03d}" for index in range(count))
    params_hash = "a" * 64
    event, cutoff = START + 360 * NS, START + 360 * NS + 1
    source = json.dumps(
        {
            "feed": "sip",
            "segment": "PRE",
            "start_ns": START,
            "end_ns": START + 19_800 * NS,
            "session_source": "fixture XNYS boundary",
            "universe_source": "fixture only",
            "baseline_source": "fixture only",
            "params_hash": params_hash,
            "universe_hash": universe_digest(symbols),
            "baseline_hash": "fixture-baseline",
        },
        sort_keys=True,
    )
    features = (
        RankingFeature("move", Decimal("0.1"), event, cutoff),
        RankingFeature("move_1m", Decimal("0.01"), event, cutoff),
        RankingFeature("dvol_ratio", Decimal("2"), event, cutoff),
        RankingFeature("trades_accel", Decimal("6"), event, cutoff),
        RankingFeature("price", Decimal("10"), event, cutoff),
    )
    generation = RankingGeneration(
        generation_id=GenerationId("fixture-bridge", 0),
        session_date=DAY,
        input_cutoff=cutoff,
        ts_event=event,
        ts_init=cutoff,
        known_at=cutoff,
        method_version=METHOD,
        profile_version="registry/" + params_hash,
        ranker_version=RANKER,
        coverage=Coverage(CoverageState.READY, True, count, count, 0, 0, 0),
        status=GenerationStatus.PROVISIONAL,
        rows=tuple(
            RankingRow(
                symbol,
                score=Decimal("0.2"),
                rank=index + 1,
                features=features,
                focus_eligible=False,
                reasons=(RankingReason("SOURCE", source),),
            )
            for index, symbol in enumerate(symbols)
        ),
    )
    return {
        "schema": "sextant.j5-ranked/1",
        "display_only": True,
        "paper_slot": 2,
        "status": "fresh",
        "generation_sha256": generation_digest(generation),
        "params_hash": params_hash,
        "generated_at_ns": cutoff,
        "expires_at_ns": cutoff + 60 * NS,
        "ranking_generation": generation.to_dict(),
        "watchlists": {"NS Ranked": [f"NASDAQ:{symbol}" for symbol in symbols]},
        "provenance": {},
    }


def test_hundred_members_project_and_101_refuse_without_truncation():
    projected = project_display_unit(synthetic_j5(100), bounds=BOUNDS)
    assert len(projected.display_lists["ns_movers"]) == 100
    with pytest.raises(BridgeError):
        project_display_unit(synthetic_j5(101), bounds=BOUNDS)


@pytest.mark.parametrize(
    "make",
    [
        lambda: DisplayBounds(0.05, Decimal("2"), 600),  # type: ignore[arg-type]
        lambda: DisplayBounds(Decimal("0"), Decimal("2"), 600),
        lambda: DisplayBounds(Decimal("0.05"), Decimal("2"), True),  # type: ignore[arg-type]
        lambda: DisplayBounds(Decimal("0.05"), Decimal("2"), 0),
    ],
)
def test_bounds_refuse_floats_bools_and_nonpositive_values(make):
    with pytest.raises(BridgeError):
        make()


def test_bounds_have_no_default_authority(tmp_path: Path):
    j5 = fresh_j5(tmp_path)
    with pytest.raises(TypeError):
        project_display_unit(j5)  # type: ignore[call-arg]
    with pytest.raises(BridgeError):
        project_display_unit(j5, bounds=None)  # type: ignore[arg-type]


def test_projection_is_pure_and_deterministic(tmp_path: Path):
    j5 = fresh_j5(tmp_path)
    before = copy.deepcopy(j5)
    first = project_display_unit(j5, bounds=BOUNDS)
    second = project_display_unit(j5, bounds=BOUNDS)
    assert first.documents() == second.documents()
    assert j5 == before

"""Offline OVERNIGHT display witnesses; fixtures confer no account authority."""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from alpaca.data.enums import DataFeed
from alpaca.data.models import BarSet
from test_tradingview_prestage import (
    DAY,
    MINUTE_NS,
    NS,
    NY,
    PRIOR,
    SYMBOLS,
    D,
    DayCloses,
    RecordedSource,
    _run_fixture,
    at,
    bounds,
    registry,
)

from sextant.ranking.baseline import segment_start
from sextant.ranking.generation import RankingGeneration, generation_digest
from sextant.tradingview import prestage
from sextant.tradingview.bars import Bar
from sextant.tradingview.baseline import _session_start, build_baseline
from sextant.tradingview.bridge import BridgeError, project_display_unit
from sextant.tradingview.cadence import Cadence
from sextant.tradingview.inputs import InputError, read_research_inputs
from sextant.tradingview.loop import _pre_open, serve
from sextant.tradingview.producer import reserve_rest_display


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline witness attempted a network connection or credential read")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(prestage, "_credentials", refuse)


def overnight(day=DAY):
    section, _, _ = segment_start(at(day, 1))
    assert section is not None and section.name == "OVERNIGHT"
    return section


def bars(day, *, current=False, minutes=480):
    start = at(day - timedelta(days=1), 20)
    return tuple(
        Bar(
            name,
            start + minute * MINUTE_NS,
            D("10") if not current else D("10.10") + D("0.10") * minute,
            D("10") if not current else D("10.10") + D("0.10") * minute,
            1_000 if not current else 5_000,
            5 if not current or minute < 5 else 40,
        )
        for minute in range(minutes)
        for name in SYMBOLS
    )


def policy(tmp_path, *, enabled=True, **updates):
    raw = json.loads(prestage.DEFAULT_CADENCE_REGISTRY.read_text())
    updates["overnight_enabled"] = int(enabled)
    for row in raw["rows"]:
        if row["name"] in updates:
            row.update(value=updates[row["name"]], source="synthetic overnight policy")
    path = tmp_path / "cadence.json"
    path.write_text(json.dumps(raw))
    return Cadence(tmp_path / "cadence", path, page=lambda _: None)


@pytest.mark.parametrize(
    "day,want",
    [
        (date(2026, 11, 2), "2026-11-01T20:00:00-05:00"),
        (date(2026, 10, 30), "2026-10-29T20:00:00-04:00"),
    ],
)
def test_history_anchor_uses_previous_calendar_day_and_its_dst(day, want):
    got = datetime.fromtimestamp(_session_start(day, overnight()) // NS, NY)
    assert got.isoformat() == want


def test_baseline_uses_matching_overnight_prefix_and_actual_feed():
    section = overnight()
    historical = tuple((day, bars(day)) for day in PRIOR)
    table = build_baseline(
        historical,
        symbols=SYMBOLS,
        segment=section,
        known_at=at(PRIOR[-1], 4),
        provenance="recorded BOATS",
        feed="boats",
    )
    source = RecordedSource(tuple(item for _, batch in historical for item in batch))
    streamed = prestage.build_segment_baseline(
        source,
        segment=section,
        symbols=SYMBOLS,
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 4),
        provenance="recorded BOATS",
        feed="boats",
    )
    value = table.cumulative(SYMBOLS[0], "OVERNIGHT", 90)
    assert value is not None
    assert value.dollar_volume == D("15000") and value.trade_count == D("7.5")
    assert table.feed == streamed.feed == "boats"
    assert table.identity == streamed.identity
    # Alpaca's end is inclusive; exclude the following PRE opening.
    assert source.ranges[0] == (at(date(2026, 11, 1), 20), at(PRIOR[0], 4) - NS)


@pytest.mark.parametrize("bad", ["missing-day", "premature", "wrong-feed"])
def test_overnight_baseline_refuses_unavailable_history(bad):
    historical = tuple(item for day in PRIOR for item in bars(day))
    if bad == "missing-day":
        historical = historical[480 * len(SYMBOLS) :]
    with pytest.raises(ValueError):
        prestage.build_segment_baseline(
            RecordedSource(historical),
            segment=overnight(),
            symbols=SYMBOLS,
            sessions=PRIOR,
            known_at=at(PRIOR[-1], 3 if bad == "premature" else 4),
            provenance="recorded history",
            feed="sip" if bad == "wrong-feed" else "boats",
        )


@pytest.mark.parametrize(
    "stamp,want",
    [
        ("2026-11-08T20:01:00-05:00", 300),
        ("2026-11-09T00:01:00-05:00", 300),
        ("2026-11-13T20:01:00-05:00", None),
        ("2026-11-25T20:01:00-05:00", None),
        ("2026-11-26T20:01:00-05:00", 300),
        ("2026-11-27T03:59:00-05:00", 300),
    ],
)
def test_cadence_admits_only_enabled_xnys_overnight_dates(tmp_path, stamp, want):
    gate = policy(tmp_path)
    now = datetime.fromisoformat(stamp)
    assert gate.floor(now, D(0)) == want
    if want:
        assert gate.floor(now, D(81)) == 900
    disabled = policy(tmp_path, enabled=False)
    assert disabled.floor(now, D(0)) is None


def test_overnight_policy_needs_valid_sourced_count_and_flag(tmp_path):
    for updates in ({"overnight_baseline_sessions": 4}, {"overnight_enabled": 2}):
        # policy's enable argument sets the flag; mutate the wire document explicitly.
        gate = policy(tmp_path)
        raw = json.loads(gate.registry_path.read_text())
        for row in raw["rows"]:
            if row["name"] in updates:
                row["value"] = updates[row["name"]]
        gate.registry_path.write_text(json.dumps(raw))
        with pytest.raises(ValueError):
            Cadence(tmp_path / "bad", gate.registry_path, page=lambda _: None)


@pytest.mark.parametrize(
    "stamp,want",
    [
        ("2026-11-08T19:00:00-05:00", "2026-11-08T20:00:00-05:00"),
        ("2026-11-13T20:00:00-05:00", "2026-11-15T20:00:00-05:00"),
        ("2026-11-25T20:00:00-05:00", "2026-11-26T20:00:00-05:00"),
        ("2026-11-27T17:00:00-05:00", "2026-11-29T20:00:00-05:00"),
    ],
)
def test_producer_and_loop_wake_at_the_next_real_overnight_open(tmp_path, stamp, want):
    now = datetime.fromisoformat(stamp)
    expected = datetime.fromisoformat(want)
    gate = policy(tmp_path)
    assert (
        prestage._next_display_open(int(now.timestamp()) * NS, overnight_enabled=True)
        == int(expected.timestamp()) * NS
    )
    assert _pre_open(now, gate.params) == expected


@pytest.mark.parametrize("day,minute", [(DAY, 8), (DAY, 248), (date(2026, 11, 10), 8)])
def test_overnight_run_publishes_boats_with_completed_sip_reference(tmp_path, day, minute):
    section = overnight(day)
    history = prestage.prior_sessions(day)
    source = DayCloses(
        tuple(item for prior in history for item in bars(prior))
        + bars(day, current=True, minutes=minute),
        {history[-1]: dict.fromkeys(SYMBOLS, D("10"))},
    )
    nasdaq, other, mapping = _run_fixture(tmp_path)
    gate = policy(tmp_path)
    now_ns = section.start_ns + minute * MINUTE_NS
    messages = []
    code = prestage.run(
        research_dir=tmp_path / "unit",
        nasdaq_listed=nasdaq,
        other_listed=other,
        symbol_map_path=mapping,
        bounds=bounds(),
        state_root=tmp_path / "state",
        cadence_registry=gate.registry_path,
        once=True,
        clock=lambda: now_ns,
        sleep=lambda _: None,
        source_factory=lambda lease: source,
        page=messages.append,
    )
    assert code == 0, messages
    assert source.days == [history[-1]]
    inputs = read_research_inputs(tmp_path / "unit", now=datetime.fromtimestamp(now_ns / NS, NY))
    assert inputs.memberships["NS movers"]
    raw = json.loads((tmp_path / "unit" / "top100.json").read_text())
    assert raw["ranking_context"] == {
        "session": "overnight",
        "session_date": day.isoformat(),
        "intraday_feed": "boats",
        "daily_history_feed": "sip",
        "daily_history_scope": "prior completed XNYS sessions; not BOATS intraday evidence",
    }
    j5 = json.loads((tmp_path / "state" / "j5-unit.json").read_text())
    assert "BOATS" in j5["provenance"]["feed_authority"]
    assert "SIP" not in json.dumps(j5["provenance"]["trades"])
    with pytest.raises(InputError):
        read_research_inputs(tmp_path / "unit", now=datetime.fromtimestamp(now_ns / NS + 601, NY))
    generation = RankingGeneration.from_json(j5["ranking_generation"])
    changed = []
    for row in generation.rows:
        reasons = []
        for reason in row.reasons:
            if reason.code == "SOURCE":
                assert reason.detail is not None
                detail = json.loads(reason.detail)
                detail["feed"] = "sip"
                reason = replace(reason, detail=json.dumps(detail))
            reasons.append(reason)
        changed.append(replace(row, reasons=tuple(reasons)))
    mismatched = replace(generation, rows=tuple(changed))
    with pytest.raises(BridgeError):
        project_display_unit(
            {
                **j5,
                "ranking_generation": mismatched.to_dict(),
                "generation_sha256": generation_digest(mismatched),
            },
            bounds=bounds(),
        )
    raw["ranking_context"]["intraday_feed"] = "sip"
    (tmp_path / "unit" / "top100.json").write_text(json.dumps(raw))
    with pytest.raises(InputError):
        read_research_inputs(tmp_path / "unit", now=datetime.fromtimestamp(now_ns / NS, NY))


def test_disabled_overnight_does_not_construct_a_data_source(tmp_path):
    nasdaq, other, mapping = _run_fixture(tmp_path)

    def refuse(lease):
        raise AssertionError("disabled overnight constructed a source")

    assert (
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=mapping,
            bounds=bounds(),
            state_root=tmp_path / "state",
            once=True,
            clock=lambda: at(DAY, 1),
            source_factory=refuse,
            page=lambda _: None,
        )
        == 0
    )
    assert not (tmp_path / "unit").exists()


def test_operator_source_selects_boats_minutes_and_sip_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(prestage, "_credentials", lambda: ("synthetic", "synthetic"))
    requests = []

    def recorded(self, request):
        requests.append(request)
        return BarSet({})

    monkeypatch.setattr(prestage.PacedStockBars, "get_stock_bars", recorded)
    with reserve_rest_display(tmp_path, paper_slot=2) as lease:
        source = prestage.operator_source(lease, requests_per_minute=200, feed=DataFeed.BOATS)
        source.minute_bars(
            SYMBOLS,
            start=datetime(2026, 11, 8, 20, tzinfo=NY),
            end=datetime(2026, 11, 9, 4, tzinfo=NY),
        )
        source.daily_closes(SYMBOLS, day=PRIOR[-1])
    assert [request.feed for request in requests] == [DataFeed.BOATS, DataFeed.SIP]
    assert [str(request.timeframe) for request in requests] == ["1Min", "1Day"]


def test_derived_feed_is_refused_before_credentials_or_a_request(tmp_path):
    with reserve_rest_display(tmp_path, paper_slot=2) as lease, pytest.raises(ValueError):
        prestage.operator_source(lease, requests_per_minute=200, feed=DataFeed.OVERNIGHT)


def test_wrong_feed_cannot_replace_an_existing_display_unit(tmp_path):
    section = overnight()
    source = RecordedSource(
        tuple(item for day in PRIOR for item in bars(day)) + bars(DAY, current=True, minutes=8)
    )
    table = prestage.build_segment_baseline(
        source,
        segment=section,
        symbols=SYMBOLS,
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 4),
        feed="boats",
    )
    sentinel = tmp_path / "unit"
    sentinel.write_text("previous unit")
    fetched = list(source.ranges)
    with reserve_rest_display(tmp_path / "lease", paper_slot=2) as lease, pytest.raises(ValueError):
        prestage.publish_tick(
            lease=lease,
            source=source,
            segment=section,
            baseline=table,
            symbols=SYMBOLS,
            prior_closes=dict.fromkeys(SYMBOLS, D("10")),
            prior_close_ns=at(PRIOR[-1], 16),
            symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
            registry=registry(),
            epoch="test",
            universe_source="recorded",
            now_ns=section.start_ns + 8 * MINUTE_NS,
            unit_dir=sentinel,
            j5_path=tmp_path / "j5.json",
            bounds=bounds(),
            feed="sip",
        )
    assert sentinel.read_text() == "previous unit"
    assert source.ranges == fetched


def test_baseline_count_row_controls_the_historical_windows(tmp_path):
    gate = policy(tmp_path, overnight_baseline_sessions=6)
    nasdaq, other, mapping = _run_fixture(tmp_path)
    sessions = prestage.prior_sessions(DAY, 6)
    source = DayCloses(
        tuple(item for day in sessions for item in bars(day)) + bars(DAY, current=True, minutes=8),
        {sessions[-1]: dict.fromkeys(SYMBOLS, D("10"))},
    )
    assert (
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=mapping,
            bounds=bounds(),
            cadence_registry=gate.registry_path,
            state_root=tmp_path / "state",
            once=True,
            clock=lambda: at(date(2026, 11, 8), 20, 8),
            source_factory=lambda lease: source,
            page=lambda _: None,
        )
        == 0
    )
    assert len(source.ranges) == 7  # six historical windows plus the current prefix
    assert source.ranges[0] == (at(date(2026, 10, 29), 20), at(date(2026, 10, 30), 4) - NS)


def test_enabled_loop_waits_elapsed_seconds_across_a_dst_weekend(tmp_path):
    gate = policy(tmp_path)
    clock = [datetime(2026, 10, 30, 20, tzinfo=NY)]
    delays = []

    async def sleep(seconds):
        delays.append(seconds)
        clock[0] = datetime.fromtimestamp(clock[0].timestamp() + seconds, NY)

    assert (
        asyncio.run(
            serve(
                tools=cast(Any, None),
                cadence=gate,
                research_dir=tmp_path / "absent",
                max_age=600,
                clock=lambda: clock[0],
                sleep=sleep,
                page=lambda _: None,
                weekly_usage_pct=D(0),
                usage_source="synthetic",
                cycles=1,
            )
        )
        == 0
    )
    assert delays == [176400]  # 49 hours: Friday EDT to Sunday EST


def test_overnight_reference_uses_the_previous_half_days_real_close(tmp_path, monkeypatch):
    day = date(2026, 11, 30)
    sessions = prestage.prior_sessions(day)
    source = DayCloses(
        tuple(item for prior in sessions for item in bars(prior))
        + bars(day, current=True, minutes=8),
        {date(2026, 11, 27): dict.fromkeys(SYMBOLS, D("10"))},
    )
    nasdaq, other, mapping = _run_fixture(tmp_path)
    gate = policy(tmp_path)
    observed = []
    original = prestage._replay

    def replay(**kwargs):
        observed.append(kwargs["prior_close_ns"])
        return original(**kwargs)

    monkeypatch.setattr(prestage, "_replay", replay)
    assert (
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=mapping,
            bounds=bounds(),
            cadence_registry=gate.registry_path,
            state_root=tmp_path / "state",
            once=True,
            clock=lambda: at(date(2026, 11, 29), 20, 8),
            source_factory=lambda lease: source,
            page=lambda _: None,
        )
        == 0
    )
    assert source.days == [date(2026, 11, 27)]
    assert observed == [at(date(2026, 11, 27), 13)]

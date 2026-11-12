"""The ranked view carries its replay between ticks and feeds only the new minutes.

Measured 2026-11-11: the part of a tick after the publication clock, mostly the view's
replay of every directory bar since the segment opened, grew 16 s → 121 s by 11:03.
Source shape: the engine's streaming batches keep actor state between batches
(nautilus_trader d53849b2 `docs/concepts/backtesting/apis-and-runs.md:74-91`), and the
merged host is a per-minute poller whose revised-bar rule needs a new epoch
(`tradingview/host.py:150-165`, `:186-192`). The unit keeps its full replay.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_tradingview_prestage import (
    DAY,
    NS,
    PRIOR,
    SYMBOLS,
    RecordedSource,
    at,
    bar,
    bounds,
    registry,
    segment,
)

D = Decimal
MINUTE_NS = 60 * NS
ALL = (*SYMBOLS, "DDD")
STEP = {"AAA": D("0.10"), "BBB": D("0.10"), "CCC": D("0.10"), "DDD": D("0.30")}


def _bars(minutes: int, *, revised: int | None = None) -> tuple[Any, ...]:
    prior = tuple(
        bar("RTH", day, minute, name, "10.00", 1_000, 5)
        for day in PRIOR
        for minute in range(390)
        for name in ALL
    )
    return prior + tuple(
        bar(
            "RTH",
            DAY,
            minute,
            name,
            f"{D('10.10') + STEP[name] * minute:f}",
            5_000 + (1 if minute == revised and name == "DDD" else 0),
            5 if minute < 5 else 40,
        )
        for minute in range(minutes)
        for name in ALL
    )


def _view(tmp_path: Path, source: RecordedSource) -> Any:
    from sextant.tradingview.prestage import RankedView, build_segment_baseline

    view = RankedView(directory=tmp_path / "view", max_age_s=600, state_root=tmp_path / "state")
    view.baseline = build_segment_baseline(
        source,
        segment=segment("RTH"),
        symbols=ALL,
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 20),
        provenance="fixture five prior sessions",
    )
    view.symbols, view.closes = ALL, dict.fromkeys(ALL, D("10.00"))
    view.universe_source = "fixture directory; NOT PIT"
    return view


def _tick(tmp_path: Path, source: RecordedSource, view: Any, lease: Any, minute: int) -> dict:
    from sextant.tradingview.prestage import publish_tick

    section = segment("RTH")
    return publish_tick(
        lease=lease,
        source=source,
        segment=section,
        baseline=_unit_baseline(source),
        symbols=SYMBOLS,
        prior_closes=dict.fromkeys(SYMBOLS, D("10.00")),
        prior_close_ns=at(PRIOR[-1], 16),
        symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
        registry=registry(),
        epoch="tv-producer-fixture-incremental",
        universe_source="fixture directory; NOT PIT",
        now_ns=section.start_ns + minute * MINUTE_NS + 5 * NS,
        unit_dir=tmp_path / "unit",
        j5_path=tmp_path / "j5.json",
        bounds=bounds(),
        view=view,
    )


def _unit_baseline(source: RecordedSource) -> Any:
    from sextant.tradingview.prestage import build_segment_baseline

    return build_segment_baseline(
        source,
        segment=segment("RTH"),
        symbols=SYMBOLS,
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 20),
        provenance="fixture five prior sessions",
    )


def _view_minutes(monkeypatch: Any) -> list[int]:
    from sextant.tradingview.host import BarFidelityHost

    seen: list[int] = []
    original = BarFidelityHost.ingest

    def spy(self: Any, poll: Any, *, market_ns: int, received_ns: int) -> int:
        if self.publisher.epoch.endswith("-view"):
            seen.append(market_ns)
        return original(self, poll, market_ns=market_ns, received_ns=received_ns)

    monkeypatch.setattr(BarFidelityHost, "ingest", spy)
    return seen


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "leases" not in path.parts and "state" not in path.parts
    }


def _run_pair(tmp_path: Path, monkeypatch: Any, *, revised: int | None = None):
    from sextant.tradingview.producer import reserve_stream

    first = RecordedSource(_bars(8))
    second = RecordedSource(_bars(12, revised=revised))
    carried, fresh = tmp_path / "carried", tmp_path / "fresh"
    seen = _view_minutes(monkeypatch)
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
    ) as lease:
        view = _view(carried, first)
        _tick(carried, first, view, lease, 8)
        seen.clear()
        _tick(carried, second, view, lease, 12)
        carried_minutes = list(seen)
        seen.clear()
        _tick(fresh, second, _view(fresh, second), lease, 12)
        full_minutes = list(seen)
    return carried, fresh, carried_minutes, full_minutes


def test_the_second_tick_feeds_only_the_new_minutes_and_matches_a_full_replay(
    tmp_path: Path, monkeypatch: Any
) -> None:
    carried, fresh, carried_minutes, full_minutes = _run_pair(tmp_path, monkeypatch)
    start = segment("RTH").start_ns
    assert full_minutes == [start + minute * MINUTE_NS for minute in range(1, 13)]
    # The last fed minute is fed again so the host's revised-bar rule still sees it.
    assert carried_minutes == [start + minute * MINUTE_NS for minute in range(8, 13)]
    page = json.loads((carried / "view" / "ranked.json").read_text())
    assert page["status"] == "fresh" and page["generation"]["ranked"] > 0
    assert page["generation"]["coverage"]["state"] == "READY"
    assert _files(carried / "view") == _files(fresh / "view")
    assert (carried / "unit").resolve().name == (fresh / "unit").resolve().name
    unit_files = _files((carried / "unit").resolve())
    assert unit_files and unit_files == _files((fresh / "unit").resolve())


def test_a_revised_last_minute_restarts_from_the_kept_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The host's own rule: a revised bar needs a new publisher epoch, never a merge.

    The refused carry is dropped; the state kept before that minute, which never saw
    the old bar, feeds it and the new minutes (test_tradingview_prestage_checkpoint.py).
    """
    carried, fresh, carried_minutes, full_minutes = _run_pair(tmp_path, monkeypatch, revised=7)
    start = segment("RTH").start_ns
    assert carried_minutes[0] == start + 8 * MINUTE_NS
    assert carried_minutes[1:] == full_minutes[7:]
    assert _files(carried / "view") == _files(fresh / "view")


@pytest.mark.parametrize(
    "change", ["segment", "epoch", "reference", "params", "lease", "baseline", "symbols", "closes"]
)
def test_a_changed_input_drops_the_carried_replay(tmp_path: Path, change: str) -> None:
    """A new segment, run, baseline, universe or reference set is a new replay."""
    from sextant.tradingview.prestage import _carry_key

    source = RecordedSource(_bars(8))
    view = _view(tmp_path, source)
    lease = object()
    inputs = {"epoch": "e", "prior_close_ns": 1, "params": "p", "lease": lease}
    key = _carry_key(view, segment("RTH"), **inputs)
    assert _carry_key(view, segment("RTH"), **inputs) == key
    section = segment("PRE") if change == "segment" else segment("RTH")
    if change in {"epoch", "params"}:
        inputs[change] = "other"
    elif change == "reference":
        inputs["prior_close_ns"] = 2
    elif change == "lease":
        inputs["lease"] = object()
    elif change == "baseline":
        view.baseline = _view(tmp_path / "other", source).baseline
    elif change == "symbols":
        view.symbols = tuple(list(view.symbols))
    elif change == "closes":
        view.closes = dict(view.closes)
    assert _carry_key(view, section, **inputs) != key

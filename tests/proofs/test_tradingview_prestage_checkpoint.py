"""A revised newest minute restarts the view from the state kept just before that minute.

Recorded 2026-11-11 on #622: 2 of 3 carried ticks met "a revised bar requires a new
publisher epoch" and replayed the whole segment (publish to receipt 252 s and 304 s
against 10 s carried). By the host's rule (`host.py:186-193`) only the minute the last
tick fed newest can raise, so one kept state, copied before that minute, suffices.
The copy is the stdlib's `copy.deepcopy` with a seeded memo (CPython 3.13 `copy.py:119-131`).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from test_tradingview_prestage import NS, RecordedSource, segment
from test_tradingview_prestage_incremental import _bars, _files, _tick, _view, _view_minutes

MINUTE_NS = 60 * NS


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name, data in _files(root).items():
        digest.update(name.encode() + b"\0" + data)
    return digest.hexdigest()


def _ticks(tmp_path: Path, monkeypatch: Any, plan: list[tuple[int, int | None]]):
    """Carried ticks over ``plan`` [(minute, revised)], then one fresh full replay of the last."""
    from sextant.tradingview.producer import reserve_stream

    seen = _view_minutes(monkeypatch)
    carried, fresh = tmp_path / "carried", tmp_path / "fresh"
    fed: list[list[int]] = []
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
    ) as lease:
        view = None
        for minute, revised in plan:
            source = RecordedSource(_bars(minute, revised=revised))
            view = view or _view(carried, source)
            seen.clear()
            _tick(carried, source, view, lease, minute)
            fed.append(list(seen))
        minute, revised = plan[-1]
        source = RecordedSource(_bars(minute, revised=revised))
        _tick(fresh, source, _view(fresh, source), lease, minute)
    return carried, fresh, fed


def _at(*minutes: int) -> list[int]:
    start = segment("RTH").start_ns
    return [start + minute * MINUTE_NS for minute in minutes]


def test_a_revised_newest_minute_replays_only_the_new_minutes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # Tick 1 feeds closes 1..8; its newest bar (minute 7, close 8) is revised by tick 2.
    carried, fresh, fed = _ticks(tmp_path, monkeypatch, [(8, None), (12, 7)])
    # The carried state refuses at close 8; the kept state then feeds closes 8..12 only.
    assert fed[1] == _at(8, 8, 9, 10, 11, 12)
    assert _digest(carried / "view") == _digest(fresh / "view")
    assert _files(carried / "view")


def test_the_kept_state_advances_with_every_tick(tmp_path: Path, monkeypatch: Any) -> None:
    # Tick 2's newest bar (minute 11, close 12) is revised by tick 3.
    carried, fresh, fed = _ticks(tmp_path, monkeypatch, [(8, None), (12, None), (16, 11)])
    assert fed[1] == _at(8, 9, 10, 11, 12)
    assert fed[2] == _at(12, 12, 13, 14, 15, 16)
    assert _digest(carried / "view") == _digest(fresh / "view")

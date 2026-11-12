"""Follow-up witnesses for #208: the operator seam, a refused setup, the publication clock."""

from __future__ import annotations

import json
import os
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
    bounds,
    current_bars,
    prior_bars,
    registry,
    segment,
)

D = Decimal
MINUTE_NS = 60 * NS


class _Stop(Exception):
    """Ends a loop witness once the refused segment has been skipped."""


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    nasdaq = tmp_path / "nasdaqlisted.txt"
    nasdaq.write_text(
        "Symbol|Security Name|Test Issue|ETF\n"
        "AAA|Alpha Corporation|N|N\n"
        "BBB|Beta Corporation|N|N\n"
        "CCC|Gamma Corporation|N|N\n"
        "File Creation Time|2026-11-09 00:00:00.000\n",
        encoding="utf-8",
    )
    other = tmp_path / "otherlisted.txt"
    other.write_text("ACT Symbol|Security Name|Test Issue|ETF\n", encoding="utf-8")
    symbol_map = tmp_path / "map.json"
    symbol_map.write_text(
        json.dumps({name: f"NASDAQ:{name}" for name in SYMBOLS}), encoding="utf-8"
    )
    return nasdaq, other, symbol_map


def test_the_operator_source_uses_the_with_keys_environment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The documented entry point builds its client with explicit credentials."""
    from sextant.tradingview.prestage import KEY_NAMES, operator_source
    from sextant.tradingview.producer import reserve_stream

    monkeypatch.setenv(KEY_NAMES[0], "fixture-operator-key")
    monkeypatch.setenv(KEY_NAMES[1], "fixture-operator-secret")
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
    ) as lease:
        source = operator_source(lease, requests_per_minute=60)
    client = source._bars.client
    assert client._api_key == os.environ[KEY_NAMES[0]]
    assert client._secret_key == os.environ[KEY_NAMES[1]]
    assert source._daily._client is client


def test_a_refused_segment_setup_is_skipped_and_the_loop_waits(
    tmp_path: Path,
) -> None:
    """A missing reference close skips the segment; the loop does not end."""
    from sextant.tradingview.prestage import run

    nasdaq, other, symbol_map = _fixture(tmp_path)
    source = RecordedSource(prior_bars("RTH", PRIOR) + current_bars("RTH"), closes={})
    pages: list[str] = []
    sleeps: list[float] = []
    calls = 0

    def clock() -> int:
        nonlocal calls
        calls += 1
        return at(DAY, 9, 31)

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        raise _Stop

    with pytest.raises(_Stop):
        run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=symbol_map,
            bounds=bounds(),
            state_root=tmp_path / "state",
            once=False,
            clock=clock,
            sleep=sleep,
            source_factory=lambda lease: source,
            page=pages.append,
        )
    assert any("skipped" in line for line in pages)
    assert sleeps == [float(segment("RTH").end_ns - at(DAY, 9, 31)) / NS]


def test_the_publication_clock_is_resampled_after_the_replay(tmp_path: Path) -> None:
    """The tick is stamped with the clock read after the fetch, not its start."""
    from sextant.tradingview.prestage import publish_tick
    from sextant.tradingview.producer import reserve_stream

    section = segment("RTH")
    source = RecordedSource(prior_bars("RTH", PRIOR) + current_bars("RTH"))
    baseline = _baseline(source, "RTH")
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
    ) as lease:
        receipt = publish_tick(
            lease=lease,
            source=source,
            segment=section,
            baseline=baseline,
            symbols=SYMBOLS,
            prior_closes={name: D("10.00") for name in SYMBOLS},
            prior_close_ns=at(PRIOR[-1], 16),
            symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
            registry=registry(),
            epoch="tv-producer-fixture-clock",
            universe_source="fixture directory; NOT PIT",
            now_ns=section.start_ns + 8 * MINUTE_NS,
            unit_dir=tmp_path / "unit",
            j5_path=tmp_path / "j5.json",
            bounds=bounds(),
            clock=lambda: section.start_ns + 9 * MINUTE_NS,
        )
    assert receipt["publish_clock_ns"] == section.start_ns + 9 * MINUTE_NS


def test_a_tick_that_finishes_after_the_segment_closes_is_refused(tmp_path: Path) -> None:
    """A tick whose fetch outlives the segment publishes nothing."""
    from sextant.tradingview.prestage import PrestageError, publish_tick
    from sextant.tradingview.producer import reserve_stream

    section = segment("PRE")
    source = RecordedSource(prior_bars("PRE", PRIOR) + current_bars("PRE"))
    baseline = _baseline(source, "PRE")
    with (
        reserve_stream(
            tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
        ) as lease,
        pytest.raises(PrestageError, match="closed before"),
    ):
        publish_tick(
            lease=lease,
            source=source,
            segment=section,
            baseline=baseline,
            symbols=SYMBOLS,
            prior_closes={name: D("10.00") for name in SYMBOLS},
            prior_close_ns=at(PRIOR[-1], 16),
            symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
            registry=registry(),
            epoch="tv-producer-fixture-closed",
            universe_source="fixture directory; NOT PIT",
            now_ns=section.end_ns - 30 * NS,
            unit_dir=tmp_path / "unit",
            j5_path=tmp_path / "j5.json",
            bounds=bounds(),
            clock=lambda: section.end_ns + 30 * NS,
        )
    assert not (tmp_path / "unit").exists()
    assert not (tmp_path / "j5.json").exists()


def _baseline(source: RecordedSource, name: str) -> Any:
    from sextant.tradingview.prestage import build_segment_baseline

    return build_segment_baseline(
        source,
        segment=segment(name),
        symbols=SYMBOLS,
        sessions=PRIOR,
        known_at=at(PRIOR[-1], 20),
        provenance="fixture five prior sessions",
    )


def test_the_entry_point_reads_the_slot_two_pair_and_names_no_revoked_slot() -> None:
    from sextant.tradingview import prestage, producer

    root = Path(__file__).parents[2]
    prestage_source = (root / "src/sextant/tradingview/prestage.py").read_text()

    assert prestage.KEY_NAMES == (
        "ALPACA_PAPER_2_API_KEY",
        "ALPACA_PAPER_2_SECRET_KEY",
    )
    forbidden = ("PAPER_" + "3", "slot" + "-3", "slot " + "3")
    for token in forbidden:
        assert token not in prestage_source
    assert producer.SCOPE.account == "paper-slot-2"
    assert "paper_slot=2" in prestage_source


def test_the_3a_arrival_note_is_gone_once_the_loop_ships() -> None:
    root = Path(__file__).parents[2]
    docs = (root / "docs/tradingview.md").read_text()
    manifest = (root / "docs/manifest.md").read_text()

    assert "This part ships the unit publication step" not in docs
    assert "arrive with the next part" not in docs
    assert "arrive in the next part" not in manifest

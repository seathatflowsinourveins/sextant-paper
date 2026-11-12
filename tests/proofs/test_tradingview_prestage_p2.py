"""The seven P2 vetoes from the part-3b review: each one fails closed at its decision."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alpaca.common.exceptions import APIError
from test_tradingview_prestage import (
    DAY,
    NS,
    PRIOR,
    SYMBOLS,
    RecordedSource,
    at,
    bar,
    baseline_for,
    bounds,
    current_bars,
    prior_bars,
    segment,
)

from sextant.tradingview import prestage
from sextant.tradingview.producer import reserve_stream

D = Decimal


class _Stop(Exception):
    """Ends a loop witness once the refused segment has been dealt with."""


def _run_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
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


def test_a_backwards_clock_never_stamps_a_unit_ahead_of_itself(tmp_path: Path) -> None:
    """A resampled clock behind the replayed bars is refused, never clamped forward."""
    section = segment("RTH")
    baseline, source = baseline_for("RTH")
    wall_clock_ns = at(DAY, 9, 35)
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
    ) as lease:
        with pytest.raises(prestage.PrestageError, match="moved behind"):
            prestage.publish_tick(
                lease=lease,
                source=source,
                segment=section,
                baseline=baseline,
                symbols=SYMBOLS,
                prior_closes={name: D("10.00") for name in SYMBOLS},
                prior_close_ns=at(PRIOR[-1], 16),
                symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
                registry=prestage.load_registry(prestage.DEFAULT_K2_REGISTRY),
                epoch="backwards-clock",
                universe_source="fixture universe",
                now_ns=at(DAY, 9, 38),
                unit_dir=tmp_path / "unit",
                j5_path=tmp_path / "j5.json",
                bounds=bounds(),
                clock=lambda: wall_clock_ns,
            )
    assert not (tmp_path / "unit").exists()
    assert not (tmp_path / "j5.json").exists()


def test_an_invalid_k2_registry_vetoes_the_next_publication(tmp_path: Path) -> None:
    """The registry is re-read and validated before every publication."""
    nasdaq, other, symbol_map = _run_fixture(tmp_path)
    registry_path = tmp_path / "k2.json"
    registry_document = json.loads(prestage.DEFAULT_K2_REGISTRY.read_text(encoding="utf-8"))
    registry_path.write_text(json.dumps(registry_document), encoding="utf-8")
    source = RecordedSource(prior_bars("RTH", PRIOR) + current_bars("RTH"))
    now_ns = at(DAY, 9, 38)
    receipts: list[dict[str, Any]] = []
    sleeps: list[float] = []

    def page(message: str) -> None:
        if message.startswith("{"):
            receipts.append(json.loads(message))
            if len(receipts) == 1:
                row = next(item for item in registry_document["rows"] if item["name"] == "top_n")
                row["value"] = True
                registry_path.write_text(json.dumps(registry_document), encoding="utf-8")

    def sleep(delay: float) -> None:
        nonlocal now_ns
        sleeps.append(delay)
        now_ns += int(delay * NS)
        if len(sleeps) == 2:
            raise _Stop

    with pytest.raises(_Stop):
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=symbol_map,
            bounds=bounds(),
            k2_registry=registry_path,
            state_root=tmp_path / "state",
            clock=lambda: now_ns,
            sleep=sleep,
            source_factory=lambda lease: source,
            page=page,
        )
    assert len(receipts) == 1
    with pytest.raises(prestage.PrestageError, match="invalid registry value"):
        prestage.load_registry(registry_path)


def test_an_invalid_cadence_is_refused_before_the_first_unit(tmp_path: Path) -> None:
    """Cadence rows are validated before a unit is written, including under --once."""
    nasdaq, other, symbol_map = _run_fixture(tmp_path)
    cadence_path = tmp_path / "cadence.json"
    cadence_document = json.loads(prestage.DEFAULT_CADENCE_REGISTRY.read_text(encoding="utf-8"))
    row = next(item for item in cadence_document["rows"] if item["name"] == "onset_floor_s")
    row["value"] = 0
    cadence_path.write_text(json.dumps(cadence_document), encoding="utf-8")
    source = RecordedSource(prior_bars("RTH", PRIOR) + current_bars("RTH"))

    with pytest.raises(ValueError, match="invalid cadence policy bounds"):
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=symbol_map,
            bounds=bounds(),
            cadence_registry=cadence_path,
            state_root=tmp_path / "state",
            once=True,
            clock=lambda: at(DAY, 9, 38),
            sleep=lambda _: None,
            source_factory=lambda lease: source,
            page=lambda _: None,
        )
    assert not (tmp_path / "unit").exists()


def test_a_request_budget_without_a_source_is_refused(tmp_path: Path) -> None:
    """A value is not an authority: the row needs a non-empty source."""
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "prestage_request_budget",
                    "unit": "requests/minute",
                    "value": 60,
                    "source": "",
                    "calibration": "UNCALIBRATED",
                    "last_change": "2026-11-10",
                    "scope": "REVIEWED",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(prestage.PrestageError, match="request budget"):
        prestage.load_request_budget(path)


def test_a_refused_segment_is_not_published_after_a_clock_rollback(tmp_path: Path) -> None:
    """A remembered refusal never publishes later in the same segment, and pages once."""
    nasdaq, other, symbol_map = _run_fixture(tmp_path)
    now_ns = at(DAY, 15, 59)
    pages: list[str] = []
    sleeps: list[float] = []
    receipts: list[dict[str, Any]] = []
    clock_calls = 0

    class OnceRefused(RecordedSource):
        reference_calls = 0

        def daily_closes(self, symbols: Any, *, day: Any) -> dict[str, Decimal]:
            self.reference_calls += 1
            if self.reference_calls == 1:
                return {}
            return super().daily_closes(symbols, day=day)

    bars = tuple(
        bar("RTH", DAY, minute, name, "10.70", 5_000, 5 if minute < 5 else 40)
        for minute in range(389)
        for name in SYMBOLS
    )
    source = OnceRefused(prior_bars("RTH", PRIOR) + bars)

    def clock() -> int:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls > 20:
            raise _Stop
        return now_ns

    def page(message: str) -> None:
        pages.append(message)
        if message.startswith("{"):
            receipts.append(json.loads(message))
            raise _Stop

    def sleep(delay: float) -> None:
        nonlocal now_ns
        sleeps.append(delay)
        now_ns = at(DAY, 15, 59)
        if len(sleeps) == 2:
            raise _Stop

    with pytest.raises(_Stop):
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=symbol_map,
            bounds=bounds(),
            state_root=tmp_path / "state",
            clock=clock,
            sleep=sleep,
            source_factory=lambda lease: source,
            page=page,
        )
    assert not receipts and not (tmp_path / "unit").exists()
    assert sum("skipped the RTH segment" in message for message in pages) == 1


def test_an_exhausted_vendor_quota_skips_the_segment_and_pages(tmp_path: Path) -> None:
    """An SDK APIError on the setup path is a counted, paged segment skip."""
    nasdaq, other, symbol_map = _run_fixture(tmp_path)

    class VendorRefusal(RecordedSource):
        def daily_closes(self, symbols: Any, *, day: Any) -> dict[str, Decimal]:
            raise APIError('{"code": 42910000, "message": "synthetic exhausted quota"}')

    source = VendorRefusal(prior_bars("RTH", PRIOR))
    pages: list[str] = []
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        raise _Stop

    with pytest.raises(_Stop):
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=symbol_map,
            bounds=bounds(),
            state_root=tmp_path / "state",
            clock=lambda: at(DAY, 9, 38),
            sleep=sleep,
            source_factory=lambda lease: source,
            page=pages.append,
        )
    assert len(pages) == 1 and "skipped the RTH segment" in pages[0]
    assert sleeps and not (tmp_path / "unit").exists()


def test_the_wait_after_a_refusal_uses_the_clock_after_the_collection(tmp_path: Path) -> None:
    """The wait is measured from a clock read after the failed collection."""
    nasdaq, other, symbol_map = _run_fixture(tmp_path)
    now_ns = at(DAY, 15, 59)
    sleeps: list[float] = []
    pages: list[str] = []

    class DelayedRefusal(RecordedSource):
        def daily_closes(self, symbols: Any, *, day: Any) -> dict[str, Decimal]:
            nonlocal now_ns
            now_ns = at(DAY, 16, 1)
            return {}

    source = DelayedRefusal(prior_bars("RTH", PRIOR), closes={})

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        raise _Stop

    with pytest.raises(_Stop):
        prestage.run(
            research_dir=tmp_path / "unit",
            nasdaq_listed=nasdaq,
            other_listed=other,
            symbol_map_path=symbol_map,
            bounds=bounds(),
            state_root=tmp_path / "state",
            clock=lambda: now_ns,
            sleep=sleep,
            source_factory=lambda lease: source,
            page=pages.append,
        )
    assert len(pages) == 1 and "skipped the RTH segment" in pages[0]
    assert sleeps == [0.0]
    assert not (tmp_path / "unit").exists()

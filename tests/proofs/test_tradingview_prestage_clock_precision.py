"""The producer validates its unit with the exact nanosecond clock that stamped it.

Recorded shape (2026-11-11, 116 of 116 generations): `generated_at` carries the
publication clock to the nanosecond, while the check read `now` through
`datetime.fromtimestamp(ns / 1e9)`, which keeps microseconds only. When that
rounding went down, `generated_at` was a few hundred nanoseconds after `now` and
the point-in-time check refused an honest tick, about half of all ticks.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from test_tradingview_prestage import (
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
from test_tradingview_prestage_followups import MINUTE_NS, _baseline

# A sub-microsecond remainder that a float-to-microsecond conversion rounds down,
# the shape of the recorded refusals (the remainder only, no recorded instant).
SUB_MICROSECOND = 19 * NS + 632_089_315


def _publish(tmp_path: Path, publication_ns: int) -> dict:
    from sextant.tradingview.prestage import publish_tick
    from sextant.tradingview.producer import reserve_stream

    section = segment("RTH")
    source = RecordedSource(prior_bars("RTH", PRIOR) + current_bars("RTH"))
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source="fixture"
    ) as lease:
        return publish_tick(
            lease=lease,
            source=source,
            segment=section,
            baseline=_baseline(source, "RTH"),
            symbols=SYMBOLS,
            prior_closes={name: Decimal("10.00") for name in SYMBOLS},
            prior_close_ns=at(PRIOR[-1], 16),
            symbol_map={name: f"NASDAQ:{name}" for name in SYMBOLS},
            registry=registry(),
            epoch="tv-producer-fixture-precision",
            universe_source="fixture directory; NOT PIT",
            now_ns=section.start_ns + 8 * MINUTE_NS,
            unit_dir=tmp_path / "unit",
            j5_path=tmp_path / "j5.json",
            bounds=bounds(),
            clock=lambda: publication_ns,
        )


def test_a_sub_microsecond_publication_clock_publishes_its_unit(tmp_path: Path) -> None:
    """The recorded refusal: the same clock stamps and validates the unit."""
    from datetime import UTC, datetime

    from sextant.tradingview.inputs import _ns

    publication_ns = segment("RTH").start_ns + 9 * MINUTE_NS + SUB_MICROSECOND
    # The fixture reproduces the recorded shape: a lossy read lands before the stamp.
    assert _ns(datetime.fromtimestamp(publication_ns / NS, UTC)) < publication_ns
    receipt = _publish(tmp_path, publication_ns)
    assert receipt["publish_clock_ns"] == publication_ns
    assert (tmp_path / "unit").is_symlink()


def test_the_guard_still_refuses_a_clock_one_nanosecond_early(tmp_path: Path) -> None:
    """Exactness, not tolerance: one nanosecond before the stamp is a future clock."""
    from nautilus_trader.core.datetime import unix_nanos_to_dt

    from sextant.tradingview.inputs import InputError, read_research_inputs

    publication_ns = segment("RTH").start_ns + 9 * MINUTE_NS + SUB_MICROSECOND
    _publish(tmp_path, publication_ns)
    unit = tmp_path / "unit"
    read_research_inputs(unit, now=unix_nanos_to_dt(publication_ns))
    with pytest.raises(InputError, match="Inconsistent or future research clocks"):
        read_research_inputs(unit, now=unix_nanos_to_dt(publication_ns - 1))

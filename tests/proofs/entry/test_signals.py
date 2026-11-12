"""Catch rising-run leakage, fabricated extension knowledge and premature reclaims."""

from decimal import Decimal as D

import pytest
from nautilus_trader.model import InstrumentId, Price

from sextant.entry.signals import EntryState, Extension, Surge, TradeCoverage, classify

from .conftest import IID, SEC, START, bar, params, trade


def state(*, start_ns=START - 3600 * SEC, **overrides) -> EntryState:
    return EntryState(IID, params(**overrides), "fixture-segment", start_ns=start_ns)


def extension(s, price):
    return classify(price, s.vwap, s.last_atr, s.params["entry_max_vwap_extension_atr"])


def arm(s: EntryState) -> None:
    s.arm(Surge(IID, "fixture-segment", START, START, START, START + 120 * SEC, D("10"), "TEST"))


def feed(s: EntryState, at, price, **kwargs) -> bool:
    tick = trade(at, price, **kwargs)
    return s.on_trade(tick, now=tick.ts_init)


def test_lower_trade_restarts_first_leg() -> None:
    s = state()
    arm(s)
    for at, price in ((0, "10.01"), (1, "10.03"), (2, "10.02")):
        feed(s, at, price)
    assert not s.first_leg_ready(D("10.02"), START + 2 * SEC)
    feed(s, 3, "10.02")
    assert not s.first_leg_ready(D("10.02"), START + 3 * SEC)
    feed(s, 4, "10.03")
    assert s.first_leg_ready(D("10.03"), START + 4 * SEC)


def test_first_leg_caps_ask_and_time_even_with_unknown_atr() -> None:
    s = state()
    arm(s)
    for at in (0, 1, 2):
        feed(s, at, "10.01")
    assert s.extension is Extension.UNKNOWN
    assert s.first_leg_ready(D("11.00"), START + 2 * SEC)
    assert not s.first_leg_ready(D("11.01"), START + 2 * SEC)
    assert not s.first_leg_ready(D("10.01"), START + 121 * SEC)


@pytest.mark.parametrize(("ask", "ready"), [("9.99", False), ("10.00", True)])
def test_first_leg_ask_is_at_or_above_quiet(ask, ready) -> None:
    s = state()
    arm(s)
    for at, price in ((0, "10.01"), (1, "10.02"), (2, "10.03")):
        feed(s, at, price)
    assert s.first_leg_ready(D(ask), START + 2 * SEC) is ready


def test_missing_atr_is_unknown_until_own_completed_bars_initialize() -> None:
    s = state()
    feed(s, 0, "10.00")
    assert s.extension is Extension.UNKNOWN
    assert extension(s, D("10")) is Extension.UNKNOWN
    for offset in (-180, -120):
        assert s.on_bar(bar(offset), now=START)
    assert not s.on_bar(bar(-60, iid=InstrumentId.from_str("OTHER.SIM")), now=START)
    assert not s.on_bar(bar(-60, minutes=5), now=START)
    feed(s, 1, "10.10")
    assert s.extension is Extension.UNKNOWN
    assert s.episode_started is None
    assert s.on_bar(bar(-60), now=START + SEC)
    feed(s, 2, "10.10")
    assert s.extension is Extension.NOT_EXTENDED
    assert extension(s, D("10.40")) is Extension.NOT_EXTENDED
    assert extension(s, D("11.00")) is Extension.EXTENDED


def test_exact_atr_extension_boundary_is_inclusive_without_float_roundoff() -> None:
    s = state()
    for offset in (-180, -120, -60):
        s.on_bar(bar(offset), now=START)
    feed(s, 0, "10.00", size=200)
    feed(s, 1, "10.60", size=100)  # VWAP 10.20; last - VWAP == 2 * ATR(0.20).
    assert s.extension is Extension.NOT_EXTENDED
    assert extension(s, D("10.60")) is Extension.NOT_EXTENDED
    assert extension(s, D("10.61")) is Extension.EXTENDED


def test_segment_reset_clears_vwap_and_first_leg() -> None:
    s = state()
    arm(s)
    feed(s, 0, "10.01")
    s.reset_segment("next-segment", start_ns=START + SEC)
    assert s.vwap is None
    assert s.extension is Extension.UNKNOWN
    assert s.surge is None
    feed(s, 1, "20.00")
    assert s.vwap == D("20")
    assert not s.first_leg_ready(D("20"), START + SEC)


def test_bad_duplicate_future_or_stale_trade_cannot_complete_run() -> None:
    s = state()
    arm(s)
    tick = trade(0, "10.01")
    assert s.on_trade(tick, now=tick.ts_init)
    assert not s.on_trade(tick, now=tick.ts_init)
    assert not s.on_trade(trade(1, "10.02"), now=START)
    assert not s.on_trade(trade(1, "10.02"), now=START + 20 * SEC)
    assert not feed(s, 2, "10.02", iid=InstrumentId.from_str("OTHER.SIM"))
    assert not s.first_leg_ready(D("10.02"), START + 2 * SEC)


def extended_state() -> EntryState:
    s = state()
    for offset in (-180, -120, -60):
        s.on_bar(bar(offset), now=START)
    feed(s, 0, "10.00", size=10000)
    feed(s, 1, "11.00")
    assert s.extension is Extension.EXTENDED
    assert s.episode_started == START + SEC
    feed(s, 2, "9.99")  # First later touch closes this episode.
    return s


def test_reclaim_requires_pullback_higher_low_and_hold() -> None:
    s = extended_state()
    for at, price in ((3, "10.03"), (4, "10.04"), (5, "10.05")):
        feed(s, at, price)
        assert not s.reclaim_ready(D(price), D("0.10"), START + at * SEC, covered_flow=True)
    feed(s, 6, "10.02")
    assert not s.reclaim_ready(D("10.03"), D("0.10"), START + 6 * SEC, covered_flow=True)
    feed(s, 7, "10.02")
    assert not s.reclaim_ready(D("10.03"), D("0.10"), START + 7 * SEC, covered_flow=True)
    feed(s, 8, "10.03")
    assert s.reclaim_ready(D("10.03"), D("0.10"), START + 8 * SEC, covered_flow=True)
    assert not s.reclaim_ready(D("10.03"), D("0.10"), START + 8 * SEC, covered_flow=False)
    assert not s.reclaim_ready(D("10.30"), D("0.10"), START + 8 * SEC, covered_flow=True)


def test_missed_reclaim_requires_a_new_pullback_and_hold() -> None:
    s = extended_state()
    for at, price in (
        (3, "10.03"),
        (4, "10.04"),
        (5, "10.05"),
        (6, "10.02"),
        (7, "10.02"),
        (8, "10.03"),
    ):
        feed(s, at, price)
    assert s.reclaim_ready(D("10.03"), D("0.10"), START + 8 * SEC, covered_flow=True)
    assert not s.reclaim_ready(D("10.20"), D("0.10"), START + 8 * SEC, covered_flow=True)
    for at, price in ((9, "10.06"), (10, "10.08"), (11, "10.09"), (12, "10.10")):
        feed(s, at, price)
        assert not s.reclaim_ready(D(price), D("0.10"), START + at * SEC, covered_flow=True)
    for at, price in ((13, "10.02"), (14, "10.02")):
        feed(s, at, price)
        assert not s.reclaim_ready(D(price), D("0.10"), START + at * SEC, covered_flow=True)
    feed(s, 15, "10.03")
    assert s.reclaim_ready(D("10.03"), D("0.10"), START + 15 * SEC, covered_flow=True)
    feed(s, 16, "10.11")
    assert not s.reclaim_ready(D("10.11"), D("0.10"), START + 16 * SEC, covered_flow=True)


@pytest.mark.parametrize(("middle", "last"), [("10.04", "10.05"), ("10.03", "10.04")])
def test_consumed_reclaim_needs_a_pullback_below_its_event(middle, last) -> None:
    s = extended_state()
    for at, price in (
        (3, "10.03"),
        (4, "10.04"),
        (5, "10.05"),
        (6, "10.02"),
        (7, "10.02"),
        (8, "10.03"),
    ):
        feed(s, at, price)
    assert s.reclaim_ready(D("10.03"), D("0.10"), START + 8 * SEC, covered_flow=True)
    feed(s, 9, middle)
    feed(s, 11, last)
    assert not s.reclaim_ready(D(last), D("0.10"), START + 11 * SEC, covered_flow=True)
    for at in (12, 13):
        feed(s, at, "10.02")
        assert not s.reclaim_ready(D("10.02"), D("0.10"), START + at * SEC, covered_flow=True)
    feed(s, 14, "10.03")
    assert s.reclaim_ready(D("10.03"), D("0.10"), START + 14 * SEC, covered_flow=True)


def test_invalidated_pullback_keeps_the_consumed_event_floor() -> None:
    s = extended_state()
    for at, price in (
        (3, "10.03"),
        (4, "10.04"),
        (5, "10.05"),
        (6, "10.02"),
        (7, "10.02"),
        (8, "10.03"),
        (9, "10.02"),
        (10, "10.06"),
        (11, "10.04"),
        (13, "10.05"),
    ):
        feed(s, at, price)
    assert not s.reclaim_ready(D("10.05"), D("0.10"), START + 13 * SEC, covered_flow=True)
    for at, price in ((14, "10.02"), (15, "10.02"), (16, "10.03")):
        feed(s, at, price)
    assert s.reclaim_ready(D("10.03"), D("0.10"), START + 16 * SEC, covered_flow=True)


def test_a_confirmed_retest_at_vwap_keeps_its_confirmation() -> None:
    s = state()
    for offset in (-180, -120, -60):
        s.on_bar(bar(offset), now=START)
    feed(s, 0, "10.00", size=10600)
    for at, price in ((1, "11.00"), (2, "9.99"), (3, "10.03"), (4, "10.04"), (5, "10.05")):
        feed(s, at, price)
    assert s.confirmed and s.vwap == D("10.01")
    feed(s, 6, "10.01")
    assert s.confirmed
    assert s.trough == s.vwap == D("10.01")
    assert s.hold_started == START + 6 * SEC


@pytest.mark.parametrize("invalid_price", ["9.98", "11.10"])
def test_below_level_or_new_episode_cannot_reuse_held_reclaim(invalid_price) -> None:
    s = extended_state()
    for at, price in ((3, "10.03"), (4, "10.04"), (5, "10.05"), (6, "10.02")):
        feed(s, at, price)
    feed(s, 7, invalid_price)
    feed(s, 9, "10.03")
    assert not s.reclaim_ready(D("10.03"), D("0.10"), START + 9 * SEC, covered_flow=True)


def test_lower_trough_restarts_hold_and_new_high_needs_another_pullback() -> None:
    s = extended_state()
    for at, price in (
        (3, "10.03"),
        (4, "10.04"),
        (5, "10.05"),
        (6, "10.03"),
        (7, "10.02"),
        (8, "10.03"),
    ):
        feed(s, at, price)
    assert not s.reclaim_ready(D("10.03"), D("0.10"), START + 8 * SEC, covered_flow=True)
    feed(s, 9, "10.03")
    assert s.reclaim_ready(D("10.03"), D("0.10"), START + 9 * SEC, covered_flow=True)
    s = extended_state()
    for at, price in (
        (3, "10.03"),
        (4, "10.04"),
        (5, "10.05"),
        (6, "10.02"),
        (7, "10.06"),
        (9, "10.06"),
    ):
        feed(s, at, price)
    assert not s.reclaim_ready(D("10.06"), D("0.10"), START + 9 * SEC, covered_flow=True)


def test_covered_flow_needs_same_segment_causal_complete_receipt() -> None:
    s = state()
    for at in (0, 1, 2):
        feed(s, at, "10.01")
    receipt = TradeCoverage(
        IID,
        "fixture-segment",
        START - 60 * SEC,
        START + 2 * SEC,
        START + 2 * SEC,
        "TEST complete eligible tape",
        {60 * SEC: 3, 2 * SEC: 2},
    )
    assert s.covered_flow(START + 2 * SEC, receipt)
    assert not s.covered_flow(START + SEC, receipt)  # Not yet known.
    assert not s.covered_flow(START + 3 * SEC, receipt)  # Coverage ended.
    assert not s.covered_flow(START + 2 * SEC, None)
    s.reset_segment("next-segment", start_ns=START + 2 * SEC)
    feed(s, 2, "10.01")
    assert not s.covered_flow(START + 2 * SEC, receipt)


@pytest.mark.parametrize(
    ("first_leg_window", "coverage_start", "last_trade", "covered"),
    [("120", -60, -30, False), ("30", -120, -29, True)],
    ids=["short-receipt-refuses", "wide-snapshot-counts"],
)
def test_covered_flow_wider_window_is_bound_to_attested_counts(
    first_leg_window, coverage_start, last_trade, covered
) -> None:
    s = state(
        covered_flow_window_s="120",
        covered_flow_baseline_min="1",
        first_leg_window_s=first_leg_window,
        flow_share_window_s="30",
    )
    for at in (-90, last_trade):
        feed(s, at, "10.01", size=10000)
    receipt = TradeCoverage(
        IID,
        "fixture-segment",
        START + coverage_start * SEC,
        START,
        START,
        "TEST complete tape",
        {60 * SEC: 1, 120 * SEC: 2},
    )
    # One baseline trade and two recent trades: 2 * 60 == 1 * 120.
    assert s.covered_flow(START, receipt) is covered


def test_the_first_leg_window_runs_from_the_surge_flag_as_n1_states() -> None:
    # northstar-lanes ADR-0005 N1:46: "Within `first_leg_window_s` of a surge flag".
    s = state()
    flag = START + 50 * SEC
    s.arm(Surge(IID, "fixture-segment", START, flag, flag, START + 300 * SEC, D("10"), "TEST"))
    for at in (166, 167, 168):
        feed(s, at, "10.01")
    assert s.first_leg_ready(D("10.01"), flag + 120 * SEC)
    assert not s.first_leg_ready(D("10.01"), flag + 121 * SEC)


@pytest.mark.parametrize(
    "quiet",
    ["10", 10, 10.0, D("10"), Price.from_str("10.00")],
    ids=["str", "int", "float", "decimal", "price"],
)
def test_arm_admits_only_a_decimal_or_price_quiet_price(quiet) -> None:
    s = state()
    surge = Surge(IID, "fixture-segment", START, START, START, START + 120 * SEC, quiet, "TEST")
    if isinstance(quiet, D | Price):
        s.arm(surge)
        assert type(s.surge.quiet_price) is D and s.surge.quiet_price == 10
    else:
        with pytest.raises(ValueError, match="quiet price"):
            s.arm(surge)
        assert s.surge is None

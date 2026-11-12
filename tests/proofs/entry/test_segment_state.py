"""N2 fresh segment boundaries, deduplication and ordinary rolling flow windows."""

from decimal import Decimal as D

import pytest

from .conftest import SEC, START, trade
from .test_signals import arm, feed, state


@pytest.mark.parametrize("lag", [2 * SEC, 600 * SEC], ids=["late", "stale"])
def test_prior_segment_straggler_cannot_change_new_segment_vwap(lag):
    s = state()
    feed(s, 98, "10.00")
    s.reset_segment("next-segment", start_ns=START + 100 * SEC)
    for at in (101, 102):
        feed(s, at, "10.00")
    old = trade(99, "8.00", size=50000, lag=lag)
    assert not s.on_trade(old, now=max(old.ts_init, START + 103 * SEC))
    assert s.volume == 200 and s.vwap == D("10.00")


def test_segment_start_is_inclusive_and_resets_identity_scope():
    s = state()
    tick = trade(100, "10.00")
    assert s.on_trade(tick, now=tick.ts_init)
    s.reset_segment("next-segment", start_ns=tick.ts_event)
    assert s.on_trade(tick, now=tick.ts_init)
    assert s.volume == 100


def test_flow_windows_expire_and_an_old_print_untrusts_only_vwap():
    s = state()
    for i in range(400):
        assert feed(s, i, "10.00", identity=f"memory-{i}")
    assert s.volume == 40000 and s.vwap == D("10.00")
    assert s.flow_dollars(START + 399 * SEC) == 60000
    assert s.flow_dollars(START + 410 * SEC) == 49000
    old = trade(0, "9.00", identity="old-veto", lag=400 * SEC)
    assert not s.on_trade(old, now=old.ts_init)
    assert not s.failure and s.void_reason == "late print: VWAP untrusted"
    assert s.volume == 40000 and s.vwap is None
    assert feed(s, 401, "10.01") and s.vwap is None
    assert s.flow_dollars(START + 411 * SEC) == D("49001")  # t352..t399 and t401


def test_compact_first_leg_keeps_its_true_origin_and_count_span():
    s = state()
    arm(s)
    for i in range(100):
        feed(s, str(D(i) / 10), "10.01")
    assert s.first_leg_ready(D("10.01"), START + 10 * SEC)
    assert s.run[0].ts_event == START
    assert len(s.run) <= s.params["first_leg_trades_count"] + 1

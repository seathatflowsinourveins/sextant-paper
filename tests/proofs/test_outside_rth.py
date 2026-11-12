"""Outside-RTH emulation witnesses.

The native Alpaca trailing stop rests outside regular hours but does not trigger
(``docs/manifest.md`` B row, Alpaca orders page read 2026-11-09).  B therefore emulates the
trigger from quotes through NautilusTrader's own calculator and releases one marketable
LIMIT after a persistent breach, exactly as ``docs/design.md`` X4 states.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from sextant.exits.policy import ExitParams
from sextant.paper.b_feed import QuoteObservation
from sextant.paper.outside_rth import OutsideRthError, OutsideRthTrail, rebuild_trigger

D = Decimal
SEC = 1_000_000_000
T0 = 1_790_000_000 * SEC  # an arbitrary, fixed instant


def trail(fraction: str = "0.025", **overrides: object) -> OutsideRthTrail:
    params = replace(ExitParams.starts(), **overrides) if overrides else ExitParams.starts()
    return OutsideRthTrail(trail_fraction=D(fraction), params=params)


def test_adopting_the_resting_native_trail_sets_the_trigger() -> None:
    state = trail()
    state.adopt(D("97.50"), T0)
    assert state.trigger == D("97.50")
    assert state.has_trigger


def test_a_quote_raises_the_trigger_for_a_sell() -> None:
    state = trail()
    state.adopt(D("97.50"), T0)
    moved = state.observe_quote(bid=D("102.00"), ask=D("102.02"), last=D("102.01"), ts_ns=T0)
    # 2.5% of the 102.00 bid is 2.55, so the candidate trigger is 99.45.
    assert moved == D("99.45")
    assert state.trigger == D("99.45")


def test_a_lower_quote_never_lowers_the_trigger() -> None:
    state = trail()
    state.adopt(D("97.50"), T0)
    state.observe_quote(bid=D("102.00"), ask=D("102.02"), last=D("102.01"), ts_ns=T0)
    moved = state.observe_quote(bid=D("101.00"), ask=D("101.02"), last=D("101.01"), ts_ns=T0 + SEC)
    assert moved is None
    assert state.trigger == D("99.45")


def test_without_an_adopted_trigger_the_first_quote_seeds_one() -> None:
    state = trail()
    moved = state.observe_quote(bid=D("100.00"), ask=D("100.02"), last=D("100.01"), ts_ns=T0)
    assert moved == D("97.50")
    assert state.trigger == D("97.50")


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(None, D("100.02")), (D("100.00"), None), (D("100.02"), D("100.00")), (D("0"), D("100.00"))],
)
def test_a_one_sided_crossed_or_nonpositive_quote_is_ignored(
    bid: Decimal | None, ask: Decimal | None
) -> None:
    state = trail()
    state.adopt(D("97.50"), T0)
    assert state.observe_quote(bid=bid, ask=ask, last=None, ts_ns=T0) is None
    assert state.trigger == D("97.50")


def test_a_breach_needs_three_eligible_trades_over_one_second() -> None:
    state = trail()
    state.adopt(D("99.45"), T0)
    assert state.observe_trade(D("99.40"), T0) is False
    assert state.observe_trade(D("99.40"), T0 + SEC // 2) is False
    assert state.observe_trade(D("99.40"), T0 + SEC) is True


def test_a_trade_above_the_trigger_resets_the_run() -> None:
    state = trail()
    state.adopt(D("99.45"), T0)
    assert state.observe_trade(D("99.40"), T0) is False
    assert state.observe_trade(D("99.40"), T0 + SEC) is False
    assert state.observe_trade(D("99.50"), T0 + SEC + 1) is False
    assert state.observe_trade(D("99.40"), T0 + SEC + 2) is False
    assert state.observe_trade(D("99.40"), T0 + 2 * SEC + 2) is False
    assert state.observe_trade(D("99.40"), T0 + 2 * SEC + 3) is True


def test_trades_without_a_trigger_cannot_breach() -> None:
    state = trail()
    assert state.observe_trade(D("99.40"), T0) is False
    assert state.observe_trade(D("99.40"), T0 + SEC) is False


def test_the_release_price_is_the_fresh_bid_collared_to_the_sub_penny_ladder() -> None:
    state = trail()
    state.adopt(D("99.45"), T0)
    state.observe_quote(bid=D("100.129"), ask=D("100.135"), last=D("100.13"), ts_ns=T0)
    assert state.observe_trade(D("99.40"), T0) is False
    assert state.observe_trade(D("99.40"), T0 + SEC // 2) is False
    assert state.observe_trade(D("99.40"), T0 + SEC) is True
    assert state.release_price(T0 + SEC) == D("100.12")


def test_a_sub_dollar_trigger_uses_the_sub_penny_increment() -> None:
    state = trail()
    state.adopt(D("0.1950"), T0)
    # 2.5% of the $0.20 bid is $0.005, so the candidate is exactly $0.1950; the penny
    # increment would round it up to $0.20 and make unchanged $0.20 prints a breach.
    assert state.observe_quote(bid=D("0.20"), ask=D("0.2001"), last=None, ts_ns=T0) is None
    assert state.trigger == D("0.1950")
    for at in (T0, T0 + SEC // 2, T0 + SEC):
        assert state.observe_trade(D("0.20"), at) is False


def test_a_sub_dollar_bid_uses_four_decimals() -> None:
    state = trail()
    state.adopt(D("0.49"), T0)
    state.observe_quote(bid=D("0.51234"), ask=D("0.51244"), last=D("0.5124"), ts_ns=T0)
    for at in (T0, T0 + SEC // 2, T0 + SEC):
        state.observe_trade(D("0.4995"), at)
    assert state.release_price(T0 + SEC) == D("0.5123")


def test_a_stale_quote_cannot_price_the_release() -> None:
    state = trail()
    state.adopt(D("99.45"), T0)
    state.observe_quote(bid=D("100.00"), ask=D("100.02"), last=D("100.01"), ts_ns=T0)
    assert state.observe_trade(D("99.40"), T0) is False
    assert state.observe_trade(D("99.40"), T0 + SEC // 2) is False
    assert state.observe_trade(D("99.40"), T0 + SEC) is True
    with pytest.raises(OutsideRthError, match="fresh"):
        state.release_price(T0 + 3 * SEC)


def test_a_release_is_marked_once_and_rearm_restores_the_breach() -> None:
    state = trail()
    state.adopt(D("99.45"), T0)
    state.observe_quote(bid=D("100.00"), ask=D("100.02"), last=D("100.01"), ts_ns=T0)
    for at in (T0, T0 + SEC // 2, T0 + SEC):
        state.observe_trade(D("99.40"), at)
    state.begin_release(T0 + SEC)
    state.mark_released(T0 + SEC)
    assert state.released
    assert state.observe_trade(D("99.40"), T0 + 2 * SEC) is False
    with pytest.raises(OutsideRthError, match="already outstanding"):
        state.release_price(T0 + 2 * SEC)
    state.rearm()
    assert not state.released
    assert state.trigger == D("99.45")


def test_breach_count_and_span_come_from_the_exit_policy() -> None:
    state = trail(breach_persist_count=2, breach_persist_span_s=D("0"))
    state.adopt(D("99.45"), T0)
    assert state.observe_trade(D("99.40"), T0) is False
    assert state.observe_trade(D("99.40"), T0) is True


def test_a_quote_at_the_staleness_boundary_still_prices_the_release() -> None:
    state = trail()
    state.adopt(D("99.45"), T0)
    state.observe_quote(bid=D("100.00"), ask=D("100.02"), last=D("100.01"), ts_ns=T0)
    for at in (T0, T0 + SEC // 2, T0 + SEC):
        state.observe_trade(D("99.40"), at)
    assert state.release_price(T0 + 2 * SEC) == D("100.00")


def test_release_price_refuses_before_a_persistent_breach() -> None:
    state = trail()
    state.adopt(D("99.45"), T0)
    state.observe_quote(bid=D("100.00"), ask=D("100.02"), last=D("100.01"), ts_ns=T0)
    assert not state.breached
    with pytest.raises(OutsideRthError, match="breach"):
        state.release_price(T0)


def test_rebuild_trigger_replays_the_tape_in_order_and_keeps_the_high_mark() -> None:
    observations = [
        QuoteObservation("AAPL", D("38.70"), D("38.71"), None, T0 + 2 * SEC),
        QuoteObservation("AAPL", D("36.99"), D("37.00"), None, T0 + SEC),
    ]

    rebuilt = rebuild_trigger(observations, trail_fraction=D("0.025"), params=ExitParams.starts())

    # 2.5% below the after-hours 38.70 bid is 37.73; the later 36.99 quote cannot lower it.
    assert rebuilt == D("37.73")


def test_rebuild_trigger_is_none_without_observations() -> None:
    assert rebuild_trigger([], trail_fraction=D("0.025"), params=ExitParams.starts()) is None


def test_raising_the_trigger_keeps_the_breach_run() -> None:
    # F5: two prints already counted against 99.45; a higher adoption must not forget them.
    state = trail()
    state.adopt(D("99.45"), T0)
    assert state.observe_trade(D("99.40"), T0) is False
    assert state.observe_trade(D("99.40"), T0 + SEC // 2) is False
    state.adopt(D("99.60"), T0 + SEC // 2)
    assert state.trigger == D("99.60")
    assert state.observe_trade(D("99.40"), T0 + SEC) is True

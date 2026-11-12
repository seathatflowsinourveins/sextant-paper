"""Entry bar subscription controls on the native offline BacktestNode."""

import pytest
from nautilus_trader.model import Bar, BarType
from test_b_lifecycle import PENNY
from test_b_lifecycle_entry import at, run_entry, ticks

from sextant.paper.b_lifecycle import BLifecycleConfig
from sextant.ranking.registry import RANK_GATE_ENABLED


@pytest.mark.parametrize(
    "kwargs, subscribed", [({}, True), ({"subscribe_entry_bars": False}, False)]
)
def test_entry_bar_subscription_defaults_on_and_can_be_disabled(tmp_path, kwargs, subscribed):
    last = ticks()[-1]
    stamp = at("0")
    bar = Bar(
        BarType.from_str(f"{PENNY}-1-MINUTE-LAST-EXTERNAL"),
        last.price,
        last.price,
        last.price,
        last.price,
        last.size,
        stamp,
        stamp,
    )
    _, _, strategy = run_entry(tmp_path, bars=[bar], **kwargs)
    book = next(iter(strategy.books.values()))
    assert book.entry is not None
    assert book.entry.last_bar_ns == (stamp if subscribed else -1)


@pytest.mark.parametrize("invalid", [None, 0, 1, "false"])
def test_entry_bar_subscription_requires_a_bool(invalid):
    with pytest.raises(ValueError, match="subscribe_entry_bars"):
        BLifecycleConfig(
            namespace="BARTEST",
            instrument_ids=(PENNY,),
            consumer=None,
            rank_gate=RANK_GATE_ENABLED,
            subscribe_entry_bars=invalid,
        )


def test_disabling_entry_subscription_preserves_shared_protection_bars(tmp_path):
    from test_b_lifecycle_width import run_width, widths

    journal, _, strategy = run_width(tmp_path, subscribe_entry_bars=False)
    book = next(iter(strategy.books.values()))
    assert book.entry is not None and book.width_bar == book.entry.bar_type
    assert widths(journal), "entry opt-out must retain the protection width subscription"

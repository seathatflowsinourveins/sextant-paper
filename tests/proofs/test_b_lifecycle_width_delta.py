"""P-R4d: the Codex MAX delta probes of S1-4f (review-s134f-d272fd3, test_delta_probes.py),
committed unchanged as failing proofs before their fixes."""

from dataclasses import replace
from decimal import Decimal as D
from typing import cast

import pytest
import test_b_lifecycle_width as W
import test_b_lifecycle_width_review as R
from nautilus_trader.model import Bar, OrderSide
from test_b_lifecycle import GATE_ON, NS, PENNY, quote, trade
from test_b_lifecycle_entry import QUOTES, TAPE, Recording, at, run_entry
from test_b_lifecycle_protect import NATIVE, OPEN, Owner, run, trails

from sextant.entry.indicators import DecimalATR
from sextant.paper.b_width import trail_percent


def test_changed_native_caps_new_trails_between_width_bars(tmp_path):
    change_ns = OPEN + 3 * W.MINUTE + NS // 4
    new_native = D("0.005")

    class Case(Owner):
        actions = (
            (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
            (change_ns + NS // 4, "OWNER-ADD", OrderSide.BUY, 10, "0.6000"),
        )

        def on_start(self):
            super().on_start()
            self.clock.set_time_alert_ns(
                "native-change",
                change_ns,
                callback=lambda _event: setattr(self._config, "native_trail_fraction", new_native),
            )

    _, orders, _ = run_entry(
        tmp_path,
        cls=Case,
        gate=replace(GATE_ON, value=False),
        width_rows=dict(W.ROWS),
        native_trail_fraction=NATIVE,
        quotes=[*QUOTES, *W.rth_quotes()],
        bars=W.bars(),
        tape=TAPE,
        extra_trades=W.rth_tape(),
    )
    added = [order for order in trails(orders) if order.ts_init > change_ns]
    print(
        "native-change arms",
        [(str(order.trailing_offset), order.ts_init - OPEN) for order in added],
    )
    assert added
    assert all(order.trailing_offset <= trail_percent(new_native) * 100 for order in added)


@pytest.mark.parametrize("mutation", ["missing", "none", "unknown"])
def test_new_arm_revalidates_width_authority_between_bars(tmp_path, mutation):
    change_ns = OPEN + 3 * W.MINUTE + NS // 4
    rows = dict(W.ROWS)

    class Case(Owner):
        actions = (
            (at("0.7"), "OWNER-INITIAL", OrderSide.BUY, 100, "0.6000"),
            (change_ns + NS // 4, "OWNER-ADD", OrderSide.BUY, 10, "0.6000"),
        )

        def on_start(self):
            super().on_start()
            self.clock.set_time_alert_ns("rows-change", change_ns, callback=self.invalidate)

        def invalidate(self, _event):
            if mutation == "none":
                self._config.width_rows = None
            elif mutation == "missing":
                rows.pop("trail_atr_period")
            else:
                rows["unknown"] = D(1)

    _, orders, _ = run_entry(
        tmp_path,
        cls=Case,
        gate=replace(GATE_ON, value=False),
        width_rows=rows,
        native_trail_fraction=NATIVE,
        quotes=[*QUOTES, *W.rth_quotes()],
        bars=W.bars(),
        tape=TAPE,
        extra_trades=W.rth_tape(),
    )
    added = [order for order in trails(orders) if order.ts_init > change_ns]
    print(
        "invalid-width arms",
        mutation,
        [(str(order.trailing_offset), order.ts_init - OPEN) for order in added],
    )
    assert added
    assert all(order.trailing_offset == trail_percent(NATIVE) * 100 for order in added)


def test_width_atr_resets_at_the_next_rth_session(tmp_path):
    next_day = 24 * 60 * W.MINUTE
    previous = W.SHAPES[:5]
    following = [
        ("0.5385", "0.5388", "0.5384", "0.5386"),
        ("0.5386", "0.5389", "0.5385", "0.5387"),
        ("0.5387", "0.5390", "0.5386", "0.5388"),
    ]
    new_bars = [
        Bar(
            bar.bar_type,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.ts_event + next_day,
            bar.ts_init + next_day,
        )
        for bar in W.bars(following)
    ]
    new_quotes = [
        quote(item.ts_event + next_day, str(item.bid_price), str(item.ask_price))
        for item in W.rth_quotes(following)
    ]
    new_trades = [
        trade(item.ts_event + next_day, str(item.price)) for item in W.rth_tape(following)
    ]
    journal, _, strategy = run(
        tmp_path,
        width_rows=dict(W.ROWS),
        quotes=[*QUOTES, *W.rth_quotes(previous), *new_quotes],
        bars=[*W.bars(previous), *new_bars],
        extra_trades=[*W.rth_tape(previous), *new_trades],
    )
    expected = DecimalATR(3)
    for bar in new_bars:
        expected.handle_bar(bar)
    actual = strategy.books[PENNY].atr.value
    new_widths = [row for row in W.widths(journal) if row["ts"] > OPEN + next_day]
    print(
        "session reset",
        actual,
        expected.value,
        [(row["ts"] - OPEN - next_day, row["atr"]) for row in new_widths],
    )
    assert actual == expected.value
    assert len(new_widths) == 1


def test_recovered_width_rows_replay_the_bars_received_while_invalid(tmp_path):
    class Recover(Recording):
        def on_start(self):
            super().on_start()
            self.clock.set_time_alert_ns(
                "restore-period",
                OPEN + 4 * W.MINUTE + NS // 4,
                callback=lambda _event: cast(dict, self._config.width_rows).update(
                    trail_atr_period=D(3)
                ),
            )

    journal, *_ = R.run_rows(tmp_path, lambda rows: rows.pop("trail_atr_period"), cls=Recover)
    expected = DecimalATR(3)
    for bar in W.bars():
        expected.handle_bar(bar)
    actual = D(W.widths(journal)[-1]["atr"])
    print("row recovery", actual, expected.value)
    assert actual == expected.value


def test_crossed_quote_does_not_send_an_offset_modify(tmp_path):
    crossed = quote(OPEN + 3 * W.MINUTE - NS // 4, "0.5400", "0.5300")

    class Crossed(Recording):
        def __init__(self, config):
            super().__init__(config)
            self.width_errors = []

        def on_bar(self, bar):
            try:
                super().on_bar(bar)
            except ValueError as error:
                self.width_errors.append(str(error))
                raise

    journal, _, strategy = run_entry(
        tmp_path,
        cls=Crossed,
        width_rows=dict(W.ROWS),
        native_trail_fraction=NATIVE,
        quotes=sorted([*QUOTES, *W.rth_quotes(), crossed], key=lambda item: item.ts_event),
        bars=W.bars(),
        extra_trades=W.rth_tape(),
    )
    assert not [row for row in W.widths(journal) if row["ts"] == OPEN + 3 * W.MINUTE]
    assert strategy.width_errors == ["the width takes a finite, positive and uncrossed quote"]
    assert not [
        row
        for row in journal
        if row["kind"] == "tighten" and row["ts"] == OPEN + 3 * W.MINUTE and row["sent"]
    ]

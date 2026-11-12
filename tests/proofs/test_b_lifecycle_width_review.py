"""P-R4: the Codex MAX probes of S1-4 (review-s134-d088137)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
import test_b_lifecycle_width as W
from test_b_lifecycle import NS
from test_b_lifecycle_entry import QUOTES, TAPE, Recording, run_entry
from test_b_lifecycle_protect import NATIVE, OPEN

from sextant.entry.indicators import DecimalATR


def run_rows(tmp_path, change=None, cls=Recording, **kwargs):
    rows = dict(W.ROWS)

    class Case(cls):
        def on_start(self):
            super().on_start()
            edit = change
            if edit is not None:
                at_ns = OPEN + 3 * W.MINUTE + NS // 4
                self.clock.set_time_alert_ns("row-change", at_ns, callback=lambda _e: edit(rows))

    found = run_entry(
        tmp_path,
        cls=Case,
        width_rows=rows,
        native_trail_fraction=NATIVE,
        quotes=[*QUOTES, *W.rth_quotes()],
        bars=W.bars(),
        tape=TAPE,
        extra_trades=W.rth_tape(),
        **kwargs,
    )
    return (*found, rows)


def test_width_period_change_after_warmup_changes_atr(tmp_path):
    journal, *_ = run_rows(tmp_path, lambda rows: rows.update(trail_atr_period=D(5)))
    oracle = DecimalATR(5)
    for bar in W.bars():
        oracle.handle_bar(bar)
    assert D(W.widths(journal)[-1]["atr"]) == oracle.value


@pytest.mark.parametrize("mutation", ["invalid", "missing"])
def test_invalid_width_period_after_warmup_refuses_modify(tmp_path, mutation):
    def change(rows):
        if mutation == "missing":
            rows.pop("trail_atr_period")
        else:
            rows["trail_atr_period"] = D(0)

    journal, _, strategy, _ = run_rows(tmp_path, change)
    later = [r for r in journal if r["kind"] == "tighten" and r["ts"] > OPEN + 3 * W.MINUTE]
    assert not any(row["sent"] for row in later)
    assert any(row.get("reason") == "width_rows_invalid" for row in journal)
    # ADAPTED (ADR-0007:17, cancel plus one resubmit): a cancel now re-arms a changed quantity,
    # never a width. Original: `"cancel" not in` the commands. Adapted: no modify, and each cancel
    # is a re-arm.
    kinds = [kind for kind, _, _ in strategy.sent]
    assert "modify" not in kinds
    assert kinds.count("cancel") == len([r for r in journal if r["kind"] == "rearm"])

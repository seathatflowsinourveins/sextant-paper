"""P-R3d: the Codex MAX delta probes of S1-3f (review-s134f-d272fd3, test_delta_probes.py),
committed unchanged as failing proofs before their fixes."""

from decimal import Decimal as D

import pytest
from test_b_lifecycle import PENNY, quote
from test_b_lifecycle_entry import QUOTES, Recording, at, run_entry
from test_b_lifecycle_protect import NATIVE, held, run, trails


@pytest.mark.parametrize("value", [D("NaN"), D("sNaN"), D("0.026")])
def test_nonfinite_native_pages_and_refuses_without_stopping(tmp_path, value):
    journal, orders, _ = run(tmp_path, native_trail_fraction=value)
    assert not trails(orders)
    assert any(row.get("reason") == "native_trail_fraction_invalid" for row in journal)


@pytest.mark.parametrize("value", [None, D("0.026")])
def test_restored_native_authority_rearms_the_unprotected_position(tmp_path, value):
    class Restore(Recording):
        def on_start(self):
            super().on_start()
            self.clock.set_time_alert_ns(
                "restore-native",
                at("0.75"),
                callback=lambda _event: setattr(self._config, "native_trail_fraction", NATIVE),
            )

    journal, orders, strategy = run_entry(
        tmp_path,
        cls=Restore,
        native_trail_fraction=value,
        quotes=[*QUOTES, quote(at("1"), "0.5050", "0.5100"), quote(at("2"), "0.5050", "0.5100")],
    )
    coverage = sum(
        (order.leaves_qty.as_decimal() for order in trails(orders) if order.is_open), D(0)
    )
    print(
        "restored native",
        value,
        "held",
        held(orders),
        "coverage",
        coverage,
        "rearm",
        strategy.books[PENNY].rearm,
    )
    assert any(row.get("reason", "").startswith("native_trail_fraction_") for row in journal)
    assert coverage == held(orders) > 0

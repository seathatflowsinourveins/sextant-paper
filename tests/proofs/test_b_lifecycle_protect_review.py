"""P-R3 review proofs: the Codex MAX probes of S1-3 (review-s134-d088137, test_review_probes.py),
committed as failing proofs before their fixes, with the reviewer's passing controls.

Each probe is the reviewer's own, on a real BacktestNode; fixture values only.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest
from nautilus_trader.config import RiskEngineConfig
from nautilus_trader.model import ClientOrderId, OrderSide, Quantity
from nautilus_trader.trading import Strategy
from test_b_lifecycle import GATE_ON, PENNY, consumer, publisher, quote
from test_b_lifecycle_entry import QUOTES, Recording, at, run_entry
from test_b_lifecycle_protect import NATIVE, Owner, held, run, trails

from sextant.paper.b_lifecycle import BLifecycleConfig


@pytest.mark.parametrize("new_quantity", [10, 50])
def test_owner_reservation_quantity_update_reconciles_house_coverage(tmp_path, new_quantity):
    """Review P1: a confirmed owner-reservation replacement must reach reconcile."""

    class OwnerResize(Owner):
        def on_start(self):
            super().on_start()
            self.clock.set_time_alert_ns(
                "resize-owner",
                at("0.9"),
                callback=lambda event: Strategy.modify_order(
                    self, ClientOrderId("OWNER-RESERVE"), quantity=Quantity.from_int(new_quantity)
                ),
            )

    OwnerResize.actions = ((at("0.7"), "OWNER-RESERVE", OrderSide.SELL, 30, "0.9000"),)
    _, orders, _ = run_entry(
        tmp_path,
        quotes=[
            *QUOTES,
            quote(at("0.8"), "0.5050", "0.5100"),
            quote(at("1.0"), "0.5050", "0.5100"),
        ],
        cls=OwnerResize,
        native_trail_fraction=NATIVE,
    )
    reservation = next(o for o in orders if str(o.client_order_id) == "OWNER-RESERVE")
    coverage = sum((o.leaves_qty.as_decimal() for o in trails(orders) if o.is_open), D(0))
    assert coverage == held(orders) - reservation.leaves_qty.as_decimal()


def test_submit_throttle_denial_rearms_after_budget_recovers(tmp_path):
    """Review P1: a trail denied by the submit rate is armed again on a later event."""
    risk = RiskEngineConfig(max_order_submit_rate="1/00:00:01")
    _, orders, _ = run(
        tmp_path,
        quotes=[
            *QUOTES,
            quote(at("1.0"), "0.5050", "0.5100"),
            quote(at("2.0"), "0.5050", "0.5100"),
            quote(at("3.0"), "0.5050", "0.5100"),
        ],
        risk=risk,
    )
    assert held(orders) > 0
    assert any(o.is_open for o in trails(orders))
    open_leaves = sum((o.leaves_qty.as_decimal() for o in trails(orders) if o.is_open), D(0))
    assert open_leaves == held(orders)  # the whole position, never a second cover


def test_native_row_is_read_after_warmup_not_copied_at_construction(tmp_path):
    """Review P2: the native row in force at the decision, not the construction copy."""

    class NativeChange(Recording):
        def __init__(self, config):
            super().__init__(config)
            self.decision_config = config

        def on_start(self):
            super().on_start()
            self.clock.set_time_alert_ns("native-row-change", at("0.25"), callback=self.change)

        def change(self, event):
            self.decision_config.native_trail_fraction = D("0.01")

    _, orders, _ = run_entry(tmp_path, cls=NativeChange, native_trail_fraction=NATIVE)
    assert trails(orders)
    assert all(o.trailing_offset == D("100.00") for o in trails(orders))


@pytest.mark.parametrize("value", [D("0.026"), D("0.5")])
def test_invalid_native_fraction_cannot_arm_outside_registry_cap(tmp_path, value):
    """Review P1: no trail wider than ADR-0025's 0.025 cap is ever submitted."""
    try:
        journal, orders, _ = run_entry(tmp_path, native_trail_fraction=value)
    except (TypeError, ValueError):
        return
    assert not trails(orders)
    assert any(row.get("reason") == "native_trail_fraction_invalid" for row in journal)


def test_b_claims_the_external_orders_on_every_instrument_it_trades_or_protects() -> None:
    """S1-8's finding, moved down: owner orders reach B only through the engine's claim."""
    entry = consumer(publisher(), heartbeat=2, generation=1, inputs=2)
    config = BLifecycleConfig(
        namespace="BTEST", instrument_ids=(PENNY,), consumer=entry, rank_gate=GATE_ON
    )
    assert config.external_order_instrument_ids == [PENNY]


# -- the reviewer's passing controls ---------------------------------------------------------


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_rank_gate_change_after_warmup_fails_closed(tmp_path, value):
    class GateChange(Recording):
        def on_start(self):
            super().on_start()
            self.clock.set_time_alert_ns("gate-change", at("0.25"), callback=self.change_gate)

        def change_gate(self, event):
            self.rank_gate = replace(GATE_ON, value=value)

    _, orders, _ = run_entry(tmp_path, cls=GateChange, native_trail_fraction=NATIVE)
    assert not orders


def test_new_option_typos_are_refused():
    entry = consumer(publisher(), heartbeat=2, generation=1, inputs=2)
    with pytest.raises(TypeError, match="native_trail_fracton"):
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=entry,
            rank_gate=GATE_ON,
            native_trail_fracton=NATIVE,
        )

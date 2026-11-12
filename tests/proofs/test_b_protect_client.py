# pyright: reportIncompatibleMethodOverride=false, reportAttributeAccessIssue=false
"""S1-P2b: the execution client is the venue's row `client_id`, passed on every submit, cancel
and modify. Upstream's IB strategies name it on each command
(nautilus_trader v2.0.0rc8 examples/live/interactive_brokers/exec_tester.py:116), because the
node routes the IB client by the venue `IB` it is configured for
(crates/live/src/node/builder.rs:719-772), not by the instrument's exchange. On Alpaca it is
unset and the venue routes. Fixture values only.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model import ClientId, ClientOrderId
from test_b_lifecycle import PENNY
from test_b_protect_single import TRAIL, Probe, probe

from sextant.paper.b_lifecycle import BLifecycle, BLifecycleConfig


class Sends(Probe):
    """The stand-in with B's own trail builder, every engine command watched with its client."""

    _arm = BLifecycle._arm

    @property
    def order_factory(self):
        built = SimpleNamespace(client_order_id=ClientOrderId("O-20261027-080600-001-BTEST-9"))
        return SimpleNamespace(trailing_stop_market=lambda *args, **kwargs: built)

    def submit_order(self, order, position_id=None, client_id=None, params=None):
        self.calls.append(("submit", client_id))

    def cancel_order(self, client_order_id, client_id=None, params=None):
        self.calls.append(("cancel", client_id))

    def modify_order(self, client_order_id, client_id=None, **kwargs):
        self.calls.append(("modify", client_id))


def sends(client_id):
    base = probe(held=150)
    config = base._config
    engine = Sends(
        BLifecycleConfig(
            namespace="BTEST",
            instrument_ids=(PENNY,),
            consumer=config.consumer,
            rank_gate=config.rank_gate,
            native_trail_fraction=config.native_trail_fraction,
            client_id=client_id,
        )
    )
    engine.calls, engine.held, engine.orders = [], Decimal(150), dict(base.orders)
    book = engine.books[PENNY]
    engine.reconcile(book)  # the quantity changed: one cancel
    engine.orders.pop(ClientOrderId(TRAIL))
    engine.reconcile(book)  # the cancel ended: one submit
    engine._modify(ClientOrderId("O-20261027-080600-001-BTEST-1"), price=Decimal(1))
    return engine.calls


@pytest.mark.parametrize(("row", "expected"), [("IB", ClientId("IB")), (None, None)])
def test_every_send_names_the_venues_client(row, expected):
    calls = sends(row)
    assert [kind for kind, _ in calls] == ["cancel", "submit", "modify"]
    assert all(client_id == expected for _, client_id in calls), calls

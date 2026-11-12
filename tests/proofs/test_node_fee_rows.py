"""R3 fee admission witness; synthetic inputs only, no operating authority."""

from argparse import Namespace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from sextant.alpaca.commission import NAMES, paper_commission_resolver
from sextant.paper import node_order


def fee_rows():
    return [
        {
            "name": name,
            "value": "0.00",
            "unit": "USD per fill",
            "source": "TEST ONLY; https://docs.alpaca.markets/us/docs/paper-trading",
            "account_scope": "paper",
            "asset_class": "us_equity",
            "currency": "USD",
            "calibration_state": "UNCALIBRATED",
            "last_change": "2026-11-11",
        }
        for name in NAMES
    ]


def test_node_fee_admission_preserves_duplicate_rows_for_the_resolver(monkeypatch):
    """A duplicate must not disappear before the real commission validator."""
    fees = fee_rows()
    supplied = [
        {"name": "native_inflight_check_interval_ms", "value": 100},
        fees[0] | {"value": "0.01"},
        *fees,
    ]
    arguments = Namespace(
        b_parameters="synthetic-b",
        node_parameters="synthetic-node",
        custody_parameters="synthetic-custody",
        entry_parameters="synthetic-entry",
        exit_parameters="synthetic-exit",
        reentry_parameters="synthetic-reentry",
    )
    # File I/O is the only substituted boundary: no parameter files are opened.
    monkeypatch.setattr(
        node_order,
        "load_b_parameters",
        lambda _path: SimpleNamespace(
            native_trail_fraction=D("0.025"), width_on=False, reconcile_interval_s=D(1)
        ),
    )
    monkeypatch.setattr(
        node_order, "_rows", lambda path: supplied if path == "synthetic-node" else []
    )
    with pytest.raises(ValueError, match="duplicate"):
        loaded = node_order.load_rows(arguments)
        paper_commission_resolver(loaded.commission)

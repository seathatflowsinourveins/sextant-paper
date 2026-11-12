"""LEAD-1452 T1: native B Strategy against the existing offline node/venue fixtures."""

from __future__ import annotations

import gc
from dataclasses import replace

import pytest
from nautilus_trader.model import (
    InstrumentId,
)

from sextant.paper import node_order

from .test_b_order_node import argv
from .test_b_order_node import expected as expected

IID = InstrumentId.from_str("ZA.ALPACA")


@pytest.fixture(autouse=True)
def owner_thread_gc():
    # Native PyO3 objects must be collected on the node's owner thread, before SDK workers.
    gc.collect()
    yield
    gc.collect()


def opted(tmp_path):
    return replace(
        node_order.parse([*argv(tmp_path), "--attended-test-entry", str(IID)]), duration_s=1
    )

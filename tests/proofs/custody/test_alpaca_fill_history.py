"""E5 u15: the node starts on an account with fill history.

The pinned LiveNode runs its own startup reconciliation over the fake venue (the harness of
test_alpaca_node.py, adapted from NT d53849b template integration tests). Alpaca's FILL
activity rows carry no stream execution id and no fee (account activities reference, read
2026-11-10), so no fill report is built from them; the order and position reports explain
them instead.
"""

import asyncio
import gc
from decimal import Decimal
from uuid import UUID

import pytest
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig, ExecutionClientConfig, LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live.clients import DataClientFactory, ExecutionClientFactory, MarketDataClient
from nautilus_trader.model import (
    AccountBalance,
    AccountId,
    AccountType,
    Currency,
    Equity,
    InstrumentId,
    Money,
    OmsType,
    OrderSide,
    Price,
    Quantity,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from sextant.alpaca.execution import (
    AlpacaExecutionClient,
    IncompleteCoverage,
    execution_engine_config,
)
from sextant.alpaca.wire import AccountGateway, submit_payload
from sextant.execution.pacer import AccountPacer

from .alpaca_venue import STAMP
from .alpaca_venue import Venue as FakeVenue
from .conftest import controls
from .test_alpaca_execution import order

IID = InstrumentId.from_str("TEST.ALPACA")


def _filled(venue, side, qty, client_order_id=None):
    payload = submit_payload(order(side=side, quantity=Quantity.from_str(qty)), {}, "fixture-")
    if client_order_id is not None:
        payload["client_order_id"] = client_order_id
    row = venue.post("/orders", payload)
    venue.rows[row["id"]].update(status="filled", filled_qty=qty, filled_avg_price="0.1234")
    venue.activities.append(  # Alpaca FILL activity shape: no execution id, no fee.
        {
            "id": f"20261110150000000::{UUID(int=500 + len(venue.activities))}",
            "activity_type": "FILL",
            "transaction_time": STAMP,
            "type": "fill",
            "price": "0.1234",
            "qty": qty,
            "side": side.name.lower(),
            "symbol": "TEST",
            "leaves_qty": "0",
            "order_id": row["id"],
            "cum_qty": qty,
            "order_status": "filled",
        }
    )
    return row


def _venue(shape):
    venue = FakeVenue()
    _filled(venue, OrderSide.BUY, "12", "owner-entry-0")
    if shape == "flat":  # The house trail sold the holding: every order is closed.
        _filled(venue, OrderSide.SELL, "12")
    else:  # The holding is still there.
        venue.positions = [
            {
                "asset_id": str(UUID(int=100)),
                "symbol": "TEST",
                "exchange": "NASDAQ",
                "asset_class": "us_equity",
                "avg_entry_price": "0.1234",
                "qty": "12",
                "side": "long",
                "cost_basis": "1.4808",
            }
        ]
    return venue


def _start(venue):
    """Run the pinned node's startup; return what a strategy sees on start, or the failure."""
    stock = Equity(IID, Symbol("TEST"), Currency.from_str("USD"), 4, Price.from_str("0.0001"), 0, 0)
    seen: dict = {}

    class Data(MarketDataClient):
        async def _connect(self):
            self._handle_instrument(stock)

        async def _disconnect(self):
            pass

    class DataFactory(DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return Data(**kwargs, venue=Venue("ALPACA"))

    class Exec(AlpacaExecutionClient):
        async def _connect(self):
            self.house_prefix = "fixture-"
            self.gateway = AccountGateway(
                venue, AccountPacer(controls(), self.clock.timestamp_ns), self._position_counts
            )
            balance = AccountBalance(
                Money.from_str("100 USD"), Money.from_str("0 USD"), Money.from_str("100 USD")
            )
            self.generate_account_state([balance], [], True, self.clock.timestamp_ns())

    class ExecFactory(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Exec(
                **kwargs,
                venue=Venue("ALPACA"),
                account_id=AccountId("ALPACA-fixture"),
                account_type=AccountType.MARGIN,
                oms_type=OmsType.NETTING,
            )

    class Consumer(Strategy):
        def on_start(self):
            seen["positions"] = [
                (p.quantity.as_decimal(), p.avg_px_open, [str(c) for c in p.commissions()])
                for p in self.cache.positions_open()
            ]
            seen["orders"] = {str(o.client_order_id): str(o.status) for o in self.cache.orders()}
            self.shutdown_system("offline reconciliation checked")

    node = LiveNode.build(
        "OFFLINE-E5",
        LiveNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TraderId("FIXTURE-001"),
            timeout_connection_secs=2,
            timeout_reconciliation_secs=2,
            timeout_portfolio_secs=2,
            timeout_disconnection_secs=1,
            delay_post_stop_secs=0,
            exec_engine=execution_engine_config(universe=[IID]),
            data_clients={"ALPACA": DataClientConfig()},
            exec_clients={"ALPACA": ExecutionClientConfig()},
        ),
        data_factories={"ALPACA": DataFactory},
        exec_factories={"ALPACA": ExecFactory},
    )
    node.add_strategy(Consumer(StrategyConfig(external_order_instrument_ids=[IID])))

    async def run(owned):
        await asyncio.wait_for(owned.run_async(), 10)

    try:
        asyncio.run(run(node))
    except Exception as exc:  # The witness records the failure.
        seen["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            node.dispose()
        finally:
            del node  # Empty the coroutine's closure before collecting native cycles.
            gc.collect()
    return seen


@pytest.mark.parametrize("shape", ["flat", "held"])
def test_the_node_starts_on_an_account_with_fill_history(shape):
    seen = _start(_venue(shape))
    print(f"\n{shape}: {seen}")  # The witness output (RED-u15.txt).
    assert "error" not in seen and "positions" in seen, seen.get("error")
    held = sum(quantity for quantity, _, _ in seen["positions"])
    assert held == (Decimal(0) if shape == "flat" else Decimal(12))


@pytest.mark.parametrize("shape", ["flat", "held"])
def test_the_same_accounts_start_without_fill_history(shape):  # Control: history is the cause.
    venue = _venue(shape)
    venue.activities.clear()
    seen = _start(venue)
    print(f"\n{shape} without history: {seen}")
    assert "error" not in seen and "positions" in seen, seen.get("error")


def test_a_holding_no_order_report_explains_still_stops_the_start():  # The engine's own refusal.
    venue = _venue("held")
    venue.rows.clear()
    venue.activities.clear()
    seen = _start(venue)
    print(f"\nunexplained holding: {seen}")
    assert "positions" not in seen


def _client(venue):
    from .test_alpaca_client import client

    node = client()
    node.gateway.sdk = venue
    return node


SCOPE = type("Scope", (), {"instrument_id": None, "start": None, "end": None})()


def test_a_fill_no_listed_order_explains_is_refused():  # The constraint stays.
    venue = _venue("held")
    venue.activities[0]["order_id"] = str(UUID(int=999))
    node = _client(venue)
    with pytest.raises(IncompleteCoverage) as refused:
        asyncio.run(node._generate_fill_reports(SCOPE))
    assert [row["order_id"] for row, _ in refused.value.failures] == [str(UUID(int=999))]
    assert not node.coverage_complete


def test_a_fill_beyond_its_orders_filled_quantity_is_refused():
    venue = _venue("held")
    venue.activities[0]["cum_qty"] = "13"
    with pytest.raises(IncompleteCoverage):
        asyncio.run(_client(venue)._generate_fill_reports(SCOPE))


def test_fill_activity_pages_are_read_to_the_end():
    venue = _venue("flat")
    for _ in range(150):  # Partial fills of the explained orders, beyond one page of 100.
        venue.activities.append(
            dict(venue.activities[0], id=f"x::{UUID(int=len(venue.activities))}")
        )
    venue.activities.append(dict(venue.activities[0], order_id=str(UUID(int=999)), id="last::1"))
    with pytest.raises(IncompleteCoverage) as refused:  # Only the last page holds the stray row.
        asyncio.run(_client(venue)._generate_fill_reports(SCOPE))
    assert [row["id"] for row, _ in refused.value.failures] == ["last::1"]
    pages = [r[2] for r in venue.requests if r[1] == "/account/activities/FILL"]
    assert [page.get("page_token") for page in pages] == [None, venue.activities[99]["id"]]

"""Native reconciliation witness, adapted from NT d53849b template integration tests."""

import asyncio
import gc
from decimal import Decimal
from uuid import UUID

from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig, ExecutionClientConfig, LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live.clients import DataClientFactory, ExecutionClientFactory, MarketDataClient
from nautilus_trader.model import (
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

from sextant.alpaca.execution import AlpacaExecutionClient, execution_engine_config
from sextant.alpaca.wire import AccountGateway, submit_payload
from sextant.execution.pacer import AccountPacer

from .alpaca_venue import Venue as FakeVenue
from .conftest import controls
from .test_alpaca_execution import order

IID = InstrumentId.from_str("TEST.ALPACA")


def test_engine_reconciles_owner_fills_and_adds_without_mutating_owner_orders():
    stock = Equity(IID, Symbol("TEST"), Currency.from_str("USD"), 4, Price.from_str("0.0001"), 0, 0)
    venue = FakeVenue()
    for index, qty in enumerate(("9", "3")):
        payload = submit_payload(
            order(side=OrderSide.BUY, quantity=Quantity.from_str(qty)), {}, "fixture-"
        )
        payload["client_order_id"] = f"owner-entry-{index}"
        row = venue.post("/orders", payload)
        venue.rows[row["id"]].update(status="filled", filled_qty=qty, filled_avg_price="0.1234")
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
    clients = []

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
            self.generate_account_state(
                [
                    AccountBalance(
                        Money.from_str("100 USD"),
                        Money.from_str("0 USD"),
                        Money.from_str("100 USD"),
                    )
                ],
                [],
                True,
                self.clock.timestamp_ns(),
            )
            clients.append(self)

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
            self.positions = self.cache.positions_open()
            self.orders = self.cache.orders()
            self.shutdown_system("offline reconciliation checked")

    from nautilus_trader.model import AccountBalance

    strategy = Consumer(StrategyConfig(external_order_instrument_ids=[IID]))
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
    node.add_strategy(strategy)

    async def run(owned):
        await asyncio.wait_for(owned.run_async(), 10)

    try:
        asyncio.run(run(node))
        positions, orders = strategy.positions, strategy.orders
        wire_actions = clients[0].gateway.pacer.wire_actions
    finally:
        try:
            node.dispose()
        finally:
            del node, strategy
            clients.clear()
            gc.collect()
    assert sum(p.quantity.as_decimal() for p in positions) == Decimal("12")
    assert len(orders) == 2
    assert {str(o.client_order_id) for o in orders} == {"owner-entry-0", "owner-entry-1"}
    assert all(r[0] in ("POST", "GET") for r in venue.requests)
    assert len([r for r in venue.requests if r[0] == "POST"]) == 2  # Fixture seeding only.
    assert not wire_actions

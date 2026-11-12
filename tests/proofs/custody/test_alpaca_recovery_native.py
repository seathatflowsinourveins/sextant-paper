"""E5 u14 gate e5u14-b435d5b P1, carried in: a fill that arrives during the recovery reads.

The gate's executed witness (test_review_witnesses.py::
test_native_engine_preserves_fill_during_recovery[False]): the second fill reaches the engine
while recovery reads positions. A mass status stamped after the reads is newer than that fill,
so the pinned engine treats the older order snapshot as authoritative and voids it
(native-head.log:133, cached=1 venue=2). The composer stamps before the reads
(crates/live/src/python/client/mod.rs:2118-2120 at d53849b2).
"""

import asyncio
from copy import deepcopy
from uuid import UUID

from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig, ExecutionClientConfig, LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live.clients import DataClientFactory, ExecutionClientFactory, MarketDataClient
from nautilus_trader.model import (
    AccountBalance,
    AccountId,
    AccountType,
    ClientOrderId,
    Currency,
    Equity,
    InstrumentId,
    Money,
    OmsType,
    OrderSide,
    OrderStatus,
    Price,
    Quantity,
    Symbol,
    TimeInForce,
    TraderId,
    Venue,
)
from nautilus_trader.trading import Strategy

from sextant.alpaca.execution import AlpacaExecutionClient, execution_engine_config
from sextant.alpaca.wire import AccountGateway, submit_payload
from sextant.execution.pacer import AccountPacer

from .alpaca_venue import Venue as FakeVenue
from .conftest import controls
from .test_alpaca_execution import order

IID = InstrumentId.from_str("TEST.ALPACA")


async def _until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_a_fill_during_the_recovery_reads_is_kept():
    stock = Equity(IID, Symbol("TEST"), Currency.from_str("USD"), 4, Price.from_str("0.0001"), 0, 0)
    venue, clients, seen, tasks = FakeVenue(), [], {}, []
    owner = submit_payload(order(), {}, "fixture-") | {"client_order_id": "owner-open"}
    venue.post("/orders", owner)

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
            self.commission_for_fill = lambda data: Money.from_str("0 USD")
            usd = Money.from_str("100 USD")
            balance = AccountBalance(usd, Money.from_str("0 USD"), usd)
            self.generate_account_state([balance], [], True, self.clock.timestamp_ns())
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
            self.native = self.order_factory.limit(
                IID,
                OrderSide.BUY,
                Quantity.from_str("9"),
                Price.from_str("0.1234"),
                time_in_force=TimeInForce.DAY,
                client_order_id=ClientOrderId("fixture-native"),
            )
            self.submit_order(self.native)
            tasks.append(asyncio.create_task(self.scenario()))

        def native_order(self):
            cached = self.cache.order(self.native.client_order_id)
            assert cached is not None
            return cached

        def filled(self):
            return self.native_order().filled_qty

        async def scenario(self):
            try:
                cid = self.native.client_order_id
                await _until(lambda: self.native_order().status == OrderStatus.ACCEPTED)
                execution = clients[0]
                row = next(r for r in venue.rows.values() if r["client_order_id"] == str(cid))

                async def fill(cumulative):
                    row.update(
                        status="partially_filled",
                        filled_qty=str(cumulative),
                        filled_avg_price="0.1234",
                    )
                    data = {
                        "event": "partial_fill",
                        "order": deepcopy(row),
                        "execution_id": f"trade-{cumulative}",
                        "price": "0.1234",
                        "qty": "1",
                        "timestamp": row["updated_at"],
                    }
                    await execution._on_trade_update({"stream": "trade_updates", "data": data})
                    await _until(lambda: self.filled() == Quantity.from_str(str(cumulative)))

                await fill(1)
                positions = execution._generate_position_status_reports

                async def second_fill_during_positions(command):
                    await fill(2)
                    venue.positions = [
                        {
                            "asset_id": str(UUID(int=100)),
                            "symbol": "TEST",
                            "exchange": "NASDAQ",
                            "asset_class": "us_equity",
                            "avg_entry_price": "0.1234",
                            "qty": "2",
                            "side": "long",
                            "cost_basis": "0.2468",
                        }
                    ]
                    return await positions(command)

                execution._generate_position_status_reports = second_fill_during_positions
                await execution._recover()
                await asyncio.sleep(0.15)
                seen["after"] = str(self.filled())
            except Exception as exc:  # The witness records the failure.
                seen["error"] = repr(exc)
            finally:
                self.shutdown_system("offline recovery witness complete")

    node = LiveNode.build(
        "OFFLINE-E5-U14",
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
    node.add_strategy(Consumer())

    async def run():
        await asyncio.wait_for(node.run_async(), 15)
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert "error" not in seen, seen
    assert seen["after"] == "2", seen  # The fill during the reads is not voided.

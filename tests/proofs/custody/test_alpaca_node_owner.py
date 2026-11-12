"""Native node witness in the production engine configuration with owner activity.

NT d53849b template integration test shape; manager.rs:4665-4668 (unclaimed
external orders filtered), 3726-3730 and 1478-1600 (unexplained positions are
unresolved when generate_missing_orders is off). No strategy claims external
orders. Design: handoffs/tasks/e5-execution-client-gpt/DESIGN-owner-activity.md.
"""

import asyncio
import gc
import threading
from types import SimpleNamespace
from uuid import UUID

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
    Price,
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


def _owner(venue, name, **changes):
    row = venue.post("/orders", submit_payload(order(), {}, "fixture-") | {"client_order_id": name})
    venue.rows[row["id"]].update(changes)
    return venue.rows[row["id"]]


def _run_node(venue, *, claims=(), lookback=None):
    """Build and run one node on its own thread (one LiveNode per thread).

    The engine flags are the production ones; reconciliation is scoped to the
    house universe (TEST) and the Strategy claims ``claims`` (LEAD-2201 item 8).
    """
    stock = Equity(IID, Symbol("TEST"), Currency.from_str("USD"), 4, Price.from_str("0.0001"), 0, 0)
    seen: dict = {"clients": []}

    class Data(MarketDataClient):
        async def _connect(self):
            self._handle_instrument(stock)  # OTHER is never loaded.

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
            seen["clients"].append(self)

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
            seen["orders"] = list(self.cache.orders())
            seen["positions"] = list(self.cache.positions())
            seen["strategy"] = self.strategy_id
            self.shutdown_system("offline reconciliation checked")

    def target():
        node = LiveNode.build(
            "OFFLINE-E5-OWNER",
            LiveNodeConfig(
                environment=Environment.SANDBOX,
                trader_id=TraderId("FIXTURE-001"),
                timeout_connection_secs=2,
                timeout_reconciliation_secs=2,
                timeout_portfolio_secs=2,
                timeout_disconnection_secs=1,
                delay_post_stop_secs=0,
                exec_engine=execution_engine_config(
                    universe=[IID], reconciliation_lookback_mins=lookback
                ),
                data_clients={"ALPACA": DataClientConfig()},
                exec_clients={"ALPACA": ExecutionClientConfig()},
            ),
            data_factories={"ALPACA": DataFactory},
            exec_factories={"ALPACA": ExecFactory},
        )
        node.add_strategy(Consumer(StrategyConfig(external_order_instrument_ids=list(claims))))

        async def run(owned):
            await asyncio.wait_for(owned.run_async(), 10)

        try:
            asyncio.run(run(node))
        except Exception as exc:  # The witness records the node's own outcome.
            seen["error"] = str(exc)
        finally:
            try:
                node.dispose()
            except Exception as exc:  # Thread exceptions alone are only pytest warnings.
                seen["error"] = str(exc)
            finally:
                # Keep observations, not unsendable clients, across the thread boundary.
                seen["clients"] = [
                    SimpleNamespace(
                        withheld=client.withheld,
                        withheld_positions=client.withheld_positions,
                        failures=[(row, str(error)) for row, error in client.failures],
                        gateway=SimpleNamespace(
                            pacer=SimpleNamespace(wire_actions=client.gateway.pacer.wire_actions)
                        ),
                    )
                    for client in seen["clients"]
                ]
                del node
                gc.collect()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(30)
    assert not thread.is_alive()
    return seen


def _seeded(venue):
    return len([r for r in venue.requests if r[0] == "POST"])


def test_node_starts_with_open_replaced_and_unloaded_owner_orders_untouched():
    venue = FakeVenue()
    open_order = _owner(venue, "owner-open")
    old = _owner(venue, "owner-was", status="replaced")
    new = _owner(venue, "owner-now", replaces=old["id"])
    old["replaced_by"] = new["id"]
    _owner(venue, "owner-other", symbol="OTHER", status="canceled")
    seeded = _seeded(venue)
    seen = _run_node(venue)
    assert "error" not in seen, seen.get("error")
    # filter_unclaimed_external_orders: the open owner order never enters the cache.
    assert seen["orders"] == [] and seen["positions"] == []
    assert {row["client_order_id"] for row, _ in seen["clients"][0].withheld} == {
        "owner-was",
        "owner-other",
    }
    assert venue.rows[open_order["id"]]["status"] == "new"
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "no owner mutation sent"
    assert not seen["clients"][0].gateway.pacer.wire_actions


def test_unexplained_owner_holding_fails_startup_closed_without_alignment_or_mutation():
    venue = FakeVenue()
    _owner(venue, "owner-filled", status="filled", filled_qty="9", filled_avg_price="0.1234")
    venue.positions = [
        {
            "asset_id": str(UUID(int=100)),
            "symbol": "TEST",
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "0.1234",
            "qty": "9",
            "side": "long",
            "cost_basis": "1.1106",
        }
    ]
    seeded = _seeded(venue)
    seen = _run_node(venue)
    # generate_missing_orders on would align it with a synthetic EXTERNAL order, and
    # filter_position_reports on or unclaimed orders kept would let the node start.
    assert "Unresolved positions" in seen.get("error", ""), seen
    assert "generate_missing_orders is disabled" in seen["error"]
    assert "orders" not in seen, "strategy must not start on an unexplained holding"
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "no mutation sent"

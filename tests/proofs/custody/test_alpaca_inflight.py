"""A venue-working order must never read as closed after a lost mutation reply.

Pinned NT d53849b: manager.rs check_inflight_orders (1860-1960) queries an order
that stays SUBMITTED, PENDING_UPDATE or PENDING_CANCEL past
inflight_check_threshold_ms every inflight_check_interval_ms, and after
inflight_check_retries emits CANCELED for PENDING_UPDATE or PENDING_CANCEL
(1935-1939); node_config.rs:457-465 (defaults 2000 ms, 5000 ms, 5 retries).
A report equal to the order changes nothing (reconcile_order_report, 4414-4420).
"""

import asyncio
import gc
import sys
import threading

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
    Money,
    OmsType,
    OrderSide,
    OrderStatus,
    Price,
    Quantity,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from sextant.alpaca.execution import AlpacaExecutionClient, execution_engine_config
from sextant.alpaca.wire import AccountGateway
from sextant.execution.pacer import AccountPacer

from .alpaca_venue import Venue as FakeVenue
from .conftest import controls
from .test_alpaca_node_owner import IID
from .test_alpaca_outcomes import _server_error

# Witness-only engine timings so the retry ladder finishes inside the test; the
# in-flight loop starts after reconciliation_startup_delay_secs (default 10 s).
INFLIGHT = dict(
    inflight_check_interval_ms=100,
    inflight_check_threshold_ms=200,
    inflight_check_retries=2,
    reconciliation_startup_delay_secs=0.0,
)


def _lose_reply(venue, method):
    def answer_5xx(path, body):
        venue.requests.append((method.upper(), path, dict(body or {})))  # Not applied.
        raise _server_error()

    setattr(venue, method, answer_5xx)


def _run(venue, lost):
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
            self.house_prefix = "O-"  # The engine's own client order ids in this witness.
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

    class Mutator(Strategy):
        def on_start(self):
            order = self.order_factory.limit(
                IID, OrderSide.BUY, Quantity.from_str("9"), Price.from_str("0.1234")
            )
            seen["id"] = order.client_order_id
            self.submit_order(order)

        def on_order_accepted(self, event):
            if "sent" in seen:
                return
            seen["sent"] = True
            _lose_reply(venue, lost)
            if lost == "patch":
                self.modify_order(event.client_order_id, price=Price.from_str("0.1235"))
            else:
                self.cancel_order(event.client_order_id)
            self.clock.set_time_alert_ns(
                "check", self.clock.timestamp_ns() + 2_500_000_000, self.check
            )

        def check(self, event):
            order = self.cache.order(seen["id"])
            assert order is not None
            seen["status"], seen["price"] = order.status, order.price
            self.shutdown_system("in-flight ladder checked")

    def target():
        node = LiveNode.build(
            "OFFLINE-E5-INFLIGHT",
            LiveNodeConfig(
                environment=Environment.SANDBOX,
                trader_id=TraderId("FIXTURE-001"),
                timeout_connection_secs=2,
                timeout_reconciliation_secs=2,
                timeout_portfolio_secs=2,
                timeout_disconnection_secs=1,
                delay_post_stop_secs=0,
                exec_engine=execution_engine_config(universe=[IID], **INFLIGHT),
                data_clients={"ALPACA": DataClientConfig()},
                exec_clients={"ALPACA": ExecutionClientConfig()},
            ),
            data_factories={"ALPACA": DataFactory},
            exec_factories={"ALPACA": ExecFactory},
        )
        node.add_strategy(Mutator(StrategyConfig()))

        async def run(owned):
            await asyncio.wait_for(owned.run_async(), 10)

        try:
            asyncio.run(run(node))
        except Exception as exc:
            seen["error"] = str(exc)
        finally:
            try:
                node.dispose()
            except Exception as exc:  # Keep failed cleanup in the caller's failure channel.
                seen["error"] = str(exc)
            finally:
                del node
                gc.collect()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(30)
    assert not thread.is_alive()
    return seen


@pytest.mark.parametrize("failure", ["5xx", "transport"])
@pytest.mark.parametrize("lost", ["patch", "delete"])
def test_working_order_with_a_lost_mutation_reply_never_reads_closed(lost, failure, monkeypatch):
    if failure == "transport":  # Review e5fix-2a7d367 P1 witness: only the type changes.
        import requests

        monkeypatch.setattr(
            sys.modules[__name__], "_server_error", lambda: requests.ConnectionError("reset")
        )
    venue = FakeVenue()
    seen = _run(venue, lost)
    assert "error" not in seen, seen.get("error")
    (row,) = venue.rows.values()
    assert row["status"] == "new", "the venue still works the order"
    assert [r[0] for r in venue.requests].count(lost.upper()) == 1
    assert seen["status"] == OrderStatus.ACCEPTED, "engine state equals the venue's"
    assert seen["price"] == Price.from_str("0.1234")

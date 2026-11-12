"""E5 u12 fix round: after a 429 on a replacement or a cancel, never two working SELLs.

Carried in from the Codex max gate on u12 4b94349c (e5u12-4b94349-codex-r1,
test_applied_ambiguous_review.py::test_rate_limited_applied_patch_never_rearms_a_second_sell,
proof-reproduce.txt:192: live_sells=2 sell_quantity=18). With the engine's in-flight check on
(the accelerated settings of test_alpaca_inflight.py), an ambiguous replacement or cancel stays
PENDING_UPDATE or PENDING_CANCEL; the pacer's 429 pause delays the status query past the retry
budget; the engine then closes the order locally (nautilus d53849b2
crates/live/src/execution/manager.rs:1935-1939), and a strategy that re-arms on that cancel
places a second SELL while the first still works at the venue.
"""

import asyncio
import threading
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from sextant.alpaca.execution import AlpacaExecutionClient, execution_engine_config
from sextant.alpaca.wire import AccountGateway, submit_payload
from sextant.execution.pacer import AccountPacer

from . import test_alpaca_inflight as native
from .alpaca_venue import Venue as FakeVenue
from .conftest import controls
from .test_alpaca_execution import order as fixture_order
from .test_alpaca_exits import _api


def _venue():
    venue = FakeVenue()
    entry = fixture_order(side=native.OrderSide.BUY, quantity=native.Quantity.from_str("9"))
    payload = submit_payload(entry, {}, "fixture-")
    payload["client_order_id"] = "owner-fixture-entry"
    row = venue.post("/orders", payload)
    venue.rows[row["id"]].update(status="filled", filled_qty="9", filled_avg_price="0.1234")
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
    venue.requests.clear()
    return venue


def _run(method, applied, *, status=429, failed_reads=False, restore_reads=False):
    """One protective SELL, an injected mutation response, and re-arm on any cancel."""
    venue, seen = _venue(), dict[str, Any](rearms=0)
    original = getattr(venue, method)

    def unavailable(path, params):
        if path.startswith("/orders"):
            seen["failed_reads"] = seen.get("failed_reads", 0) + 1
            raise ConnectionError("fixture status reads unavailable")
        return get(path, params)

    get = venue.get

    def rate_limited(path, body):
        if applied:
            original(path, body)
            if method == "patch":
                venue.confirm()
        if failed_reads:
            venue.get = unavailable
        raise _api(status)

    stock = native.Equity(
        native.IID, native.Symbol("TEST"), native.Currency.from_str("USD"), 4,
        native.Price.from_str("0.0001"), 0, 0,
    )  # fmt: skip

    class Data(native.MarketDataClient):
        async def _connect(self):
            self._handle_instrument(stock)

        async def _disconnect(self):
            pass

    class DataFactory(native.DataClientFactory):
        @staticmethod
        def create(**kwargs):
            return Data(**kwargs, venue=native.Venue("ALPACA"))

    class Exec(AlpacaExecutionClient):
        async def _connect(self):
            self.house_prefix = "O-"
            self.gateway = AccountGateway(
                venue, AccountPacer(controls(), self.clock.timestamp_ns), self._position_counts
            )
            usd = native.Money.from_str("100 USD")
            balance = native.AccountBalance(usd, native.Money.from_str("0 USD"), usd)
            self.generate_account_state([balance], [], True, self.clock.timestamp_ns())

    class ExecFactory(native.ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            return Exec(
                **kwargs,
                venue=native.Venue("ALPACA"),
                account_id=native.AccountId("ALPACA-fixture"),
                account_type=native.AccountType.MARGIN,
                oms_type=native.OmsType.NETTING,
            )

    class RearmOnCancel(native.Strategy):
        def arm(self):
            quantity, price = native.Quantity.from_str("9"), native.Price.from_str("0.1234")
            sell = self.order_factory.limit(native.IID, native.OrderSide.SELL, quantity, price)
            seen.setdefault("first", sell.client_order_id)
            self.submit_order(sell)

        def on_start(self):
            self.arm()
            now = self.clock.timestamp_ns()
            self.clock.set_time_alert_ns("check", now + 2_500_000_000, self.check)
            if restore_reads:
                self.clock.set_time_alert_ns("restore", now + 1_500_000_000, self.restore)

        def restore(self, event):
            order = self.cache.order(seen["first"])
            assert order is not None
            seen["before_recovery"] = order.status
            venue.get = get

        def on_order_accepted(self, event):
            if event.client_order_id != seen["first"] or seen.get("sent"):
                return
            seen["sent"] = True
            setattr(venue, method, rate_limited)
            if method == "patch":
                self.modify_order(event.client_order_id, price=native.Price.from_str("0.1235"))
            else:
                self.cancel_order(event.client_order_id)

        def on_order_canceled(self, event):
            if not seen["rearms"]:
                seen["rearms"] += 1
                self.arm()

        def check(self, event):
            order = self.cache.order(seen["first"])
            assert order is not None
            seen["status"] = order.status
            self.shutdown_system("offline rate-limited mutation witness")

    def target():
        node = None
        try:
            node = native.LiveNode.build(
                "OFFLINE-U12-429",
                native.LiveNodeConfig(
                    environment=native.Environment.SANDBOX,
                    trader_id=native.TraderId("FIXTURE-001"),
                    timeout_connection_secs=2,
                    timeout_reconciliation_secs=2,
                    timeout_portfolio_secs=2,
                    timeout_disconnection_secs=1,
                    delay_post_stop_secs=0,
                    exec_engine=execution_engine_config(universe=[native.IID], **native.INFLIGHT),
                    data_clients={"ALPACA": native.DataClientConfig()},
                    exec_clients={"ALPACA": native.ExecutionClientConfig()},
                ),
                data_factories={"ALPACA": DataFactory},
                exec_factories={"ALPACA": ExecFactory},
            )
            config = native.StrategyConfig(external_order_instrument_ids=[native.IID])
            node.add_strategy(RearmOnCancel(config))

            async def run():
                assert node is not None
                await asyncio.wait_for(node.run_async(), 10)

            asyncio.run(run())
        except Exception as error:  # The witness records the failure.
            seen["error"] = repr(error)
        finally:
            if node is not None:
                node.dispose()  # Release this node on its owning thread before assertions.

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "offline native node did not finish"
    return venue, seen


@pytest.mark.parametrize(
    ("method", "applied"), [("patch", True), ("patch", False), ("delete", False)]
)
def test_a_rate_limited_mutation_never_leaves_two_working_sells(method, applied):
    venue, seen = _run(method, applied)
    assert "error" not in seen, seen.get("error")
    working = [r for r in venue.rows.values() if r["side"] == "sell" and r["status"] == "new"]
    quantity = sum((Decimal(r["qty"]) for r in working), Decimal(0))
    print(f"\n{method} applied={applied}: working_sells={len(working)} quantity={quantity}")
    assert quantity <= Decimal(venue.positions[0]["qty"])


@pytest.mark.parametrize("method", ["patch", "delete"])
@pytest.mark.parametrize("applied", [True, False])
def test_undocumented_mutation_and_failed_recovery_never_close_or_oversell(method, applied):
    # Break caught: native in-flight exhaustion synthesizes CANCELED from an ambiguous 400.
    venue, seen = _run(method, applied, status=400, failed_reads=True)
    assert "error" not in seen, seen.get("error")
    working = [r for r in venue.rows.values() if r["side"] == "sell" and r["status"] == "new"]
    quantity = sum((Decimal(r["qty"]) for r in working), Decimal(0))
    position = Decimal(venue.positions[0]["qty"])
    print(
        f"\n{method} applied={applied}: working_sells={len(working)} "
        f"sell_quantity={quantity} position_quantity={position} "
        f"rearms={seen['rearms']} status={seen.get('status')} "
        f"failed_reads={seen.get('failed_reads', 0)} error={seen.get('error')}"
    )
    assert quantity <= position
    assert seen["rearms"] == 0
    assert seen["status"] in (native.OrderStatus.PENDING_UPDATE, native.OrderStatus.PENDING_CANCEL)
    assert seen.get("failed_reads", 0) > 0


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_an_authoritative_recovery_read_resolves_the_undocumented_mutation(method):
    venue, seen = _run(method, False, status=400, failed_reads=True, restore_reads=True)
    assert "error" not in seen, seen.get("error")
    assert seen["before_recovery"] in (
        native.OrderStatus.PENDING_UPDATE,
        native.OrderStatus.PENDING_CANCEL,
    )
    assert seen["status"] == native.OrderStatus.ACCEPTED
    assert seen["rearms"] == 0
    assert seen.get("failed_reads", 0) > 0
    working = [r for r in venue.rows.values() if r["side"] == "sell" and r["status"] == "new"]
    assert [r["qty"] for r in working] == ["9"]

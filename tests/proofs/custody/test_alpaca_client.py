"""Native Alpaca hooks and venue witness; sources in the execution module."""

import asyncio
import importlib.util
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from nautilus_trader.model import ClientOrderId, Money, OrderSide, OrderType, Price, Quantity

from sextant.execution.pacer import AccountPacer

from .test_alpaca_execution import boundary, command, order


def client_module():
    assert importlib.util.find_spec("sextant.alpaca.execution") is not None, "client missing"
    return importlib.import_module("sextant.alpaca.execution")


def client():
    from nautilus_trader.common import Clock
    from nautilus_trader.config import ExecutionClientConfig
    from nautilus_trader.model import (
        AccountId,
        AccountType,
        Currency,
        Equity,
        InstrumentId,
        OmsType,
        Symbol,
        TraderId,
        Venue,
    )

    from .alpaca_venue import Venue as FakeVenue
    from .conftest import controls

    stock = Equity(
        InstrumentId.from_str("TEST.ALPACA"),
        Symbol("TEST"),
        Currency.from_str("USD"),
        4,
        Price.from_str("0.0001"),
        0,
        0,
    )
    config = ExecutionClientConfig()
    cache = Mock()
    cache.instrument.return_value = stock
    cache.positions_open.return_value = []
    cache.orders_open.return_value = []
    node = client_module().AlpacaExecutionClient(
        name="ALPACA",
        config=config,
        cache=cache,
        clock=Clock.new_test(),
        trader_id=TraderId("FIXTURE-001"),
        venue=Venue("ALPACA"),
        account_id=AccountId("ALPACA-fixture"),
        account_type=AccountType.MARGIN,
        oms_type=OmsType.NETTING,
    )
    node.house_prefix = "fixture-"
    node.gateway = boundary().AccountGateway(
        FakeVenue(), AccountPacer(controls(), lambda: 0), lambda: (0, 0)
    )
    node.create_task = lambda coroutine, name: asyncio.create_task(coroutine)
    # These unit helpers wait for receipts; the native hook itself only schedules.
    for name in ("submit_order", "modify_order", "cancel_order", "query_order", "query_account"):
        native_hook = getattr(node, "_" + name)

        async def wait_for_receipt(command, hook=native_hook):
            await hook(command)
            await asyncio.gather(*tuple(node._actions))

        setattr(node, "_" + name, wait_for_receipt)
    node._handle_report = Mock()
    for name in (
        "submitted",
        "denied",
        "rejected",
        "updated",
        "modify_rejected",
        "cancel_rejected",
        "canceled",
        "triggered",
    ):
        setattr(node, f"generate_order_{name}", Mock())
    node.cache.order.return_value = order()
    return node


def test_client_submit_and_confirmed_patch_keep_one_engine_id():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        node.generate_order_submitted.assert_called_once()
        venue = node.gateway.sdk
        old = next(iter(venue.rows.values()))
        modify = command(
            client_order_id=ClientOrderId("fixture-entry"),
            venue_order_id=None,
            price=Price.from_str("0.1235"),
        )
        await node._modify_order(modify)
        node.generate_order_updated.assert_not_called()
        successor = venue.confirm()
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {
                    "event": "new",
                    "order": successor,
                    "timestamp": successor["updated_at"],
                },
            }
        )
        node.generate_order_updated.assert_called_once()
        report = node._handle_report.call_args.args[0]
        assert report.client_order_id == ClientOrderId("fixture-entry")
        assert str(report.venue_order_id) == successor["id"] != old["id"]
        await node._cancel_order(modify)
        assert venue.requests[-1][1].endswith(successor["id"])

    asyncio.run(run())


def test_native_trail_qty_and_offset_patch_and_executing_exit_guard():
    async def run():
        node = client()
        trail = order(order_type=OrderType.TRAILING_STOP_MARKET, side=OrderSide.SELL)
        node.cache.order.return_value = trail
        await node._submit_order(NS(order=trail, params={}))
        change = command(
            client_order_id=trail.client_order_id,
            venue_order_id=None,
            quantity=Quantity.from_str("12"),
            trigger_price=Price.from_str("0.12"),
            params={"trailing_offset": Decimal("125")},
        )
        await node._modify_order(change)
        body = node.gateway.sdk.requests[-1][2]
        assert (body["trail"], body["qty"]) == ("1.25", "12")
        row = node.gateway.sdk.confirm()
        row.update(status="partially_filled", filled_qty="1")
        node.gateway.sdk.rows[row["id"]] = row
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {
                    "event": "new",
                    "order": row,
                    "timestamp": row["updated_at"],
                },
            }
        )
        before = len([r for r in node.gateway.sdk.requests if r[0] != "GET"])
        await node._cancel_order(change)
        await node._modify_order(change)
        assert len([r for r in node.gateway.sdk.requests if r[0] != "GET"]) == before
        node.generate_order_cancel_rejected.assert_called_once()
        node.generate_order_modify_rejected.assert_called_once()

    asyncio.run(run())


def test_owner_mutations_and_account_cancel_are_refused_without_wire():
    async def run():
        node = client()
        owner = command(client_order_id=ClientOrderId("owner-entry"), venue_order_id=None)
        await node._cancel_order(owner)
        await node._modify_order(owner)
        with pytest.raises(NotImplementedError):
            await node._cancel_all_orders(NS())
        await node._disconnect()
        assert not node.gateway.sdk.requests

    asyncio.run(run())


def test_failed_report_row_prevents_absence_and_flat_inference():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        venue.rows["bad"] = row | {"id": "bad", "status": "future_state"}
        scan = NS(open_only=True, instrument_id=None, start=None, end=None)
        with pytest.raises(client_module().IncompleteCoverage) as exc:
            await node._generate_order_status_reports(scan)
        assert len(exc.value.failures) == 1
        assert len(exc.value.reports) == 1
        assert exc.value.failures[0][0]["status"] == "future_state"
        assert node.coverage_complete is False
        assert node._provides_bulk_position_coverage(order().instrument_id) is False

    asyncio.run(run())


def test_owner_successor_is_never_adopted_through_house_parent():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        old = next(iter(node.gateway.sdk.rows.values()))
        owner = old | {
            "id": "00000000-0000-0000-0000-000000000099",
            "client_order_id": "owner-successor",
            "replaces": old["id"],
        }
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {
                    "event": "new",
                    "order": owner,
                    "timestamp": owner["updated_at"],
                },
            }
        )
        node.generate_order_updated.assert_not_called()
        assert node._handle_report.call_args.args[0].client_order_id == ClientOrderId(
            "owner-successor"
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "event",
    [
        "accepted",
        "rejected",
        "pending_new",
        "stopped",
        "pending_cancel",
        "pending_replace",
        "calculated",
        "suspended",
        "order_replace_rejected",
        "order_cancel_rejected",
    ],
)
def test_trade_events_are_read_as_raw_strings(event):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {
                    "event": event,
                    "order": row,
                    "timestamp": row["updated_at"],
                },
            }
        )
        if event == "order_replace_rejected":
            node.generate_order_modify_rejected.assert_called_once()
        elif event == "order_cancel_rejected":
            node.generate_order_cancel_rejected.assert_called_once()
        else:
            assert node._handle_report.called

    asyncio.run(run())


def test_engine_config_retains_venue_position_truth_without_synthetic_alignment():
    config = client_module().execution_engine_config(universe=["TEST.ALPACA"])
    assert config.generate_missing_orders is False
    assert config.filter_position_reports is False
    assert config.filter_unclaimed_external_orders is True
    assert list(config.reconciliation_instrument_ids or []) == ["TEST.ALPACA"]
    with pytest.raises(ValueError, match="universe"):
        client_module().execution_engine_config(universe=[])


def test_fake_venue_models_session_trigger_and_wash_refusal():
    node = client()
    venue = node.gateway.sdk
    payload = boundary().submit_payload(
        order(order_type=OrderType.TRAILING_STOP_MARKET, side=OrderSide.SELL), {}, "fixture-"
    )
    venue.post("/orders", payload)
    venue.regular = False
    venue.quote("1")
    assert venue.quote("0.5")[0]["status"] == "new"
    venue.regular = True
    assert venue.quote("0.5")[0]["status"] == "stopped"
    with pytest.raises(ValueError, match="wash"):
        venue.post("/orders", boundary().submit_payload(order(), {}, "fixture-"))
    with pytest.raises(ValueError, match="LIMIT"):
        venue.post("/orders", payload | {"extended_hours": True})


def test_restarted_client_recovers_confirmed_house_successor():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        change = command(
            client_order_id=ClientOrderId("fixture-entry"),
            venue_order_id=None,
            price=Price.from_str("0.1235"),
        )
        await node._modify_order(change)
        row = node.gateway.sdk.confirm()
        restored = client()
        restored.gateway.sdk = node.gateway.sdk
        report = await restored._generate_order_status_report(
            NS(client_order_id=change.client_order_id, venue_order_id=row["id"])
        )
        assert report.client_order_id == change.client_order_id
        assert restored._active["fixture-entry"] == row["id"]
        await restored._cancel_order(change)
        assert restored.gateway.sdk.requests[-1][1].endswith(row["id"])

    asyncio.run(run())


def test_incomplete_position_fetch_revokes_previous_bulk_coverage():
    async def run():
        node = client()
        node.coverage_complete = True
        node.gateway.sdk.get = Mock(side_effect=ConnectionError("fixture transport failure"))
        with pytest.raises(ConnectionError):
            await node._generate_position_status_reports(NS())
        assert not node._provides_bulk_position_coverage(None)

    asyncio.run(run())


def test_order_queries_return_confirmed_successor_after_predecessor_lookup():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        old = next(iter(node.gateway.sdk.rows.values()))
        change = command(
            client_order_id=ClientOrderId("fixture-entry"),
            venue_order_id=None,
            price=Price.from_str("0.1235"),
        )
        await node._modify_order(change)
        row = node.gateway.sdk.confirm()
        node._active.clear()
        report = await node._generate_order_status_report(
            NS(client_order_id=change.client_order_id, venue_order_id=old["id"])
        )
        assert report is not None, "confirmed successor query must produce a report"
        assert str(report.venue_order_id) == row["id"]

    asyncio.run(run())


def test_missing_fill_commission_is_unavailable_not_zero():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        update = {
            "stream": "trade_updates",
            "data": {
                "event": "fill",
                "order": row,
                "execution_id": "fixture-fill",
                "price": "0.1234",
                "qty": "9",
                "timestamp": row["updated_at"],
            },
        }
        with pytest.raises(client_module().IncompleteCoverage, match="incomplete") as failure:
            await node._on_trade_update(update)
        assert failure.value.failures[0][0] is update
        assert "commission" in str(failure.value.failures[0][1])
        assert not node.coverage_complete

    asyncio.run(run())


def test_stream_fill_with_explicit_evidence_uses_execution_not_order_totals():
    async def run():
        from nautilus_trader.model import Money

        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        node.commission_for_fill = lambda data: Money.from_str("0.01 USD")
        node._handle_report.reset_mock()
        update = {
            "stream": "trade_updates",
            "data": {
                "event": "partial_fill",
                "order": row,
                "execution_id": "fixture-fill",
                "price": "0.1234",
                "qty": "1",
                "timestamp": row["updated_at"],
            },
        }
        await node._on_trade_update(update)
        report = node._handle_report.call_args.args[0]
        assert report.last_qty == Quantity.from_str("1")
        assert report.commission == Money.from_str("0.01 USD")
        assert report.client_order_id == ClientOrderId("fixture-entry")

    asyncio.run(run())


def test_trigger_during_pacing_deferral_revokes_mutation_admission():
    async def run():
        node = client()
        trail = order(
            order_type=OrderType.TRAILING_STOP_MARKET,
            side=OrderSide.SELL,
            is_triggered=False,
            is_sell=True,
            filled_qty=Quantity.from_str("0"),
        )
        node.cache.order.return_value = trail
        await node._submit_order(NS(order=trail, params={}))
        current = node._current

        async def deplete(command):
            row = await current(command)
            node.gateway.pacer.bucket.tokens = 0
            return row

        node._current = deplete
        change = command(
            client_order_id=trail.client_order_id,
            venue_order_id=None,
            params={"trailing_offset": Decimal("125")},
        )
        task = asyncio.create_task(node._modify_order(change))
        for _ in range(20):
            await asyncio.sleep(0.001)
            if node.gateway.pacer.queue:
                break
        assert node.gateway.pacer.queue
        trail.is_triggered = True
        node.gateway.pacer.bucket.tokens = 1
        node.gateway.pacer.drain()
        await task
        assert not any(r[0] == "PATCH" for r in node.gateway.sdk.requests)
        node.generate_order_modify_rejected.assert_called_once()

    asyncio.run(run())


def test_late_predecessor_acceptance_cannot_move_active_identity_back():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        stale = dict(next(iter(node.gateway.sdk.rows.values())))
        change = command(
            client_order_id=ClientOrderId("fixture-entry"),
            venue_order_id=None,
            price=Price.from_str("0.1235"),
        )
        await node._modify_order(change)
        row = node.gateway.sdk.confirm()
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {"event": "new", "order": row, "timestamp": row["updated_at"]},
            }
        )
        node._handle_report.reset_mock()
        await node._on_trade_update(
            {
                "stream": "trade_updates",
                "data": {"event": "new", "order": stale, "timestamp": stale["updated_at"]},
            }
        )
        assert node._active["fixture-entry"] == row["id"]
        node._handle_report.assert_not_called()

    asyncio.run(run())


def test_bad_stream_row_preserves_payload_and_revokes_coverage():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = dict(next(iter(node.gateway.sdk.rows.values())), status="future_state")
        node.coverage_complete = True
        update = {
            "stream": "trade_updates",
            "data": {"event": "new", "order": row, "timestamp": row["updated_at"]},
        }
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._on_trade_update(update)
        assert failure.value.failures[0][0] is update
        assert not node.coverage_complete

    asyncio.run(run())


def test_gateway_configuration_is_serialized_and_missing_rows_stop_before_sdk(monkeypatch):
    from sextant.alpaca.config import AlpacaExecutionClientConfig

    config = AlpacaExecutionClientConfig(
        api_key_env="FIXTURE_KEY",
        api_secret_env="FIXTURE_SECRET",  # noqa: S106 - environment name, not a key.
        account_id="ALPACA-fixture",
        house_prefix="fixture-",
        pacing_rows=None,
    )
    assert config.dict()["house_prefix"] == "fixture-"
    sdk = Mock()
    monkeypatch.setattr(client_module(), "TradingClient", sdk)
    node = client()
    node.config = config
    node.house_prefix = "fixture-"
    with pytest.raises(ValueError, match="pacing"):
        asyncio.run(node._connect())
    sdk.assert_not_called()


def test_stream_callback_does_not_hold_client_on_sdk_thread(monkeypatch):
    import gc
    import weakref
    from unittest.mock import AsyncMock

    from .conftest import controls

    class Stream:
        def subscribe_trade_updates(self, handler):
            self.handler = handler

        def run(self):
            raise AssertionError("offline proof must not start SDK transport")

        async def _dispatch(self, msg):  # The SDK hooks E5 u13 observes, also held weakly.
            pass

        async def _start_ws(self):
            pass

    stream = Stream()
    monkeypatch.setenv("FIXTURE_KEY", "dummy-key")
    monkeypatch.setenv("FIXTURE_SECRET", "dummy-secret")
    node = client()
    fixture_controls = controls()
    monkeypatch.setattr(client_module().Controls, "from_rows", lambda rows: fixture_controls)
    monkeypatch.setattr(client_module(), "TradingClient", Mock())
    monkeypatch.setattr(client_module(), "TradingStream", lambda *a, **k: stream)
    node.config = NS(pacing_rows=[], api_key_env="FIXTURE_KEY", api_secret_env="FIXTURE_SECRET")  # noqa: S106 - fixture environment name
    node._do_query_account = AsyncMock()
    node.commission_for_fill = lambda data: Money.from_str("0.01 USD")  # Fixture evidence only.
    node.create_task = lambda coroutine, name: coroutine.close()
    asyncio.run(node._connect())
    reference = weakref.ref(node)
    del node
    gc.collect()
    assert reference() is None, "SDK callback must only retain a weak client reference"


def test_bulk_order_request_preserves_native_bounds_and_instrument():
    async def run():
        from nautilus_trader.model import InstrumentId

        node = client()
        await node._generate_order_status_reports(
            NS(
                open_only=False,
                instrument_id=InstrumentId.from_str("TEST.ALPACA"),
                start=1_000_000_000,
                end=2_000_000_000,
            )
        )
        params = node.gateway.sdk.requests[0][2]
        assert params["after"] == "1970-01-01T00:00:01.000000000Z"
        assert params["until"] == "1970-01-01T00:00:02.000000000Z"
        assert params["symbols"] == "TEST"

    asyncio.run(run())


def test_parent_read_failure_retains_sibling_reports():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        sibling = dict(
            row,
            id="00000000-0000-0000-0000-000000000099",
            client_order_id="fixture-entry-r1",
            replaces=row["id"],
        )
        listing = [row, sibling]
        get = node.gateway.sdk.get

        def fail_parent(path, params):
            if path == "/orders":
                return listing
            raise ConnectionError("fixture parent observation unavailable")

        node.gateway.sdk.get = fail_parent
        with pytest.raises(client_module().IncompleteCoverage) as failure:
            await node._generate_order_status_reports(
                NS(open_only=False, instrument_id=None, start=None, end=None)
            )
        assert len(failure.value.reports) == len(failure.value.failures) == 1
        assert failure.value.failures[0][0] is sibling
        node.gateway.sdk.get = get

    asyncio.run(run())


def test_account_report_rejects_float_money_before_emission():
    async def run():
        node = client()
        node.generate_account_state = Mock()
        node.gateway.sdk.get = Mock(
            return_value={
                "equity": 100.1,
                "cash": "100.00",
                "buying_power": "100.00",
                "currency": "USD",
            }
        )
        with pytest.raises(ValueError, match="decimal"):
            await node._query_account(None)
        node.generate_account_state.assert_not_called()

    asyncio.run(run())


def test_trade_stream_shutdown_cancels_pending_connect_and_joins(monkeypatch):
    from threading import Event
    from unittest.mock import AsyncMock

    from alpaca.trading.stream import TradingStream

    from .conftest import controls

    async def run():
        node = client()
        fixture_controls = controls()
        stream = TradingStream("fixture-key", "fixture-secret", paper=True, raw_data=True)
        started, closed = Event(), Event()
        owner = []

        async def connecting():
            stream._loop = asyncio.get_running_loop()
            owner.append((stream._loop, asyncio.current_task()))
            started.set()
            await asyncio.Event().wait()  # Model a connect/auth await that does not finish.

        async def close():
            closed.set()

        monkeypatch.setattr(stream, "_run_forever", connecting)
        monkeypatch.setattr(stream, "close", close)
        monkeypatch.setenv("FIXTURE_KEY", "dummy-key")
        monkeypatch.setenv("FIXTURE_SECRET", "dummy-secret")
        monkeypatch.setattr(client_module().Controls, "from_rows", lambda rows: fixture_controls)
        monkeypatch.setattr(client_module(), "TradingClient", Mock())
        monkeypatch.setattr(client_module(), "TradingStream", lambda *a, **k: stream)
        node.config = NS(pacing_rows=[], api_key_env="FIXTURE_KEY", api_secret_env="FIXTURE_SECRET")  # noqa: S106 - fixture environment name  # noqa: S106 - fixture env name
        node._do_query_account = AsyncMock()
        node.commission_for_fill = lambda data: Money.from_str("0.01 USD")  # Fixture evidence only.
        node.create_task = lambda coro, name: asyncio.create_task(coro)
        await node._connect()
        assert await asyncio.to_thread(started.wait, 1)
        try:
            await asyncio.wait_for(node._disconnect(), 0.2)
            assert closed.is_set()
            assert node._stream_task is None or node._stream_task.done()
        finally:
            loop, task = owner[0]
            if not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)

    asyncio.run(run())


def test_partly_filled_entry_can_cancel_its_unfilled_remainder():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        row.update(status="partially_filled", filled_qty="1", filled_avg_price="0.1234")
        update = {
            "stream": "trade_updates",
            "data": {
                "event": "partial_fill",
                "order": row,
                "execution_id": "fixture-fill",
                "price": "0.1234",
                "qty": "1",
                "timestamp": row["updated_at"],
            },
        }
        with pytest.raises(client_module().IncompleteCoverage):
            await node._on_trade_update(update)
        await node._cancel_order(
            command(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
        )
        assert node.gateway.sdk.requests[-1][0] == "DELETE"

    asyncio.run(run())


def test_malformed_trailing_offset_emits_local_modify_rejection():
    async def run():
        node = client()
        trail = order(order_type=OrderType.TRAILING_STOP_MARKET, side=OrderSide.SELL)
        node.cache.order.return_value = trail
        await node._submit_order(NS(order=trail, params={}))
        await node._modify_order(
            command(
                client_order_id=trail.client_order_id,
                venue_order_id=None,
                trigger_price=Price.from_str("0.12"),
                params={"trailing_offset": "invalid-decimal"},
            )
        )
        node.generate_order_modify_rejected.assert_called_once()
        assert not any(r[0] == "PATCH" for r in node.gateway.sdk.requests)

    asyncio.run(run())


def test_empty_order_class_keeps_the_pinned_sdk_simple_order_semantics():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        row = next(iter(node.gateway.sdk.rows.values()))
        row["order_class"] = ""
        reports = await node._generate_order_status_reports(
            NS(open_only=True, instrument_id=None, start=None, end=None)
        )
        assert len(reports) == 1
        assert reports[0].client_order_id == ClientOrderId("fixture-entry")
        assert reports[0].order_type == OrderType.LIMIT
        assert node.coverage_complete

    asyncio.run(run())


def test_missing_commission_evidence_keeps_writer_unavailable_before_sdk(monkeypatch):
    from .conftest import controls

    node = client()
    fixture_controls = controls()
    node.config = NS(pacing_rows=[], api_key_env="FIXTURE_KEY", api_secret_env="FIXTURE_SECRET")  # noqa: S106 - fixture environment name
    monkeypatch.setattr(client_module().Controls, "from_rows", lambda rows: fixture_controls)
    sdk = Mock()
    monkeypatch.setattr(client_module(), "TradingClient", sdk)
    monkeypatch.delenv("FIXTURE_KEY", raising=False)
    monkeypatch.delenv("FIXTURE_SECRET", raising=False)
    with pytest.raises(ValueError, match="commission"):
        asyncio.run(node._connect())
    sdk.assert_not_called()


def test_connect_uses_registered_account_budget_without_a_second_code_pin(monkeypatch):
    from unittest.mock import AsyncMock

    from .conftest import controls

    node = client()
    fixture_controls = controls(request_budget=150)
    node.config = NS(pacing_rows=[], api_key_env="FIXTURE_KEY", api_secret_env="FIXTURE_SECRET")  # noqa: S106 - fixture environment name
    node.commission_for_fill = lambda data: Money.from_str("0.01 USD")  # Fixture evidence only.
    node._do_query_account = AsyncMock()
    node.create_task = lambda coroutine, name: coroutine.close()
    monkeypatch.setenv("FIXTURE_KEY", "dummy-key")
    monkeypatch.setenv("FIXTURE_SECRET", "dummy-secret")
    monkeypatch.setattr(client_module().Controls, "from_rows", lambda rows: fixture_controls)
    sdk = Mock()
    monkeypatch.setattr(client_module(), "TradingClient", sdk)
    monkeypatch.setattr(client_module(), "TradingStream", Mock())
    asyncio.run(node._connect())
    assert node.gateway.pacer.controls is fixture_controls
    assert node.gateway.pacer.bucket.limit == fixture_controls["request_budget"]
    sdk.assert_called_once()

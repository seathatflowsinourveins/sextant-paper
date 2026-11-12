"""Stage 1 start 1 (FAILED-START-1.md): a failed startup reconciliation names its cause.

The engine's composer runs the three report generators under try_join and returns the first
error (nautilus d53849b2 crates/common/src/clients/execution.rs:543-561). The node logs only the
outer context, "Failed to get mass status from ALPACA" (crates/live/src/python/node.rs:676), so
the client records, per stage, its request and row counts and the first failure's class, HTTP
status and reason text, and the node writes them to the receipt and the unit output. No value
(symbol, price, quantity, order id, account id, key) is recorded.
"""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from sextant.paper import node_order

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import order
from .test_alpaca_exits import _api
from .test_b_order_node import AccountVenue, expected, start  # noqa: F401 - autouse fixture

SCOPE = NS(open_only=False, instrument_id=None, start=None, end=None)
STRAY = {"id": "x::1", "order_id": "00000000-0000-0000-0000-000000000999", "cum_qty": "1"}


def _values(node):
    """Every value the fixture venue holds that the trace must never carry."""
    rows = [*node.gateway.sdk.rows.values(), *node.gateway.sdk.activities]
    return {str(v) for row in rows for k, v in row.items() if k in ("id", "symbol", "order_id")}


def test_an_unexplained_fill_names_its_stage_counts_and_reason():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        node.gateway.sdk.activities = [dict(STRAY, symbol="TEST", price="0.1234", qty="1")]
        with pytest.raises(client_module().IncompleteCoverage):
            await node._generate_fill_reports(SCOPE)
        fills = node.report_trace["fills"]
        assert fills["error"] == "IncompleteCoverage" and fills["first"] == "ValueError"
        assert fills["reason"] == "historical fill that no order report explains"
        assert fills["failures"] == 1 and fills["status"] is None
        assert fills["requests"]["/account/activities/FILL"] == {"requests": 1, "rows": 1}
        assert fills["requests"]["/orders"] == {"requests": 1, "rows": 1}
        text = json.dumps(node.report_trace)
        assert not any(value in text for value in _values(node)) and "0.1234" not in text

    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500])
def test_an_http_failure_names_its_status_and_no_body(status):
    async def run():
        node = client()

        def refused(path, params):
            raise _api(status)

        node.gateway.sdk.get = refused
        with pytest.raises(client_module().APIError):
            await node._generate_position_status_reports(SCOPE)
        positions = node.report_trace["positions"]
        assert (positions["error"], positions["status"]) == ("APIError", status)
        assert positions["reason"] is None  # A venue body is never copied.
        assert positions["requests"]["/positions"] == {"requests": 1, "rows": 0}

    asyncio.run(run())


def test_a_failed_start_writes_the_cause_to_the_receipt_and_one_error_line(tmp_path, capfd):
    venue = AccountVenue()
    venue.activities = [dict(STRAY, symbol="ZA")]
    body, code, _ = start(tmp_path, venue)
    assert (code, body["phase"]) == (2, "failed")
    cause = body["reconciliation"]
    assert cause["fills"]["reason"] == "historical fill that no order report explains"
    assert cause["fills"]["requests"]["/account/activities/FILL"]["rows"] == 1
    err = capfd.readouterr().err
    (line,) = [x for x in err.splitlines() if "startup reconciliation failed" in x]
    assert line.startswith("order node ERROR") and "stage=fills" in line
    assert "IncompleteCoverage" in line and STRAY["order_id"] not in line + json.dumps(body)


# Gate fsc-9788f30 P2 (review-gpt.md sha256 589b2452...): the recorder never replaces, wraps or
# suppresses the generator's exception, and never changes a result.


def _raising(error):
    async def generator(self, command):
        raise error

    return client_module()._traced("positions")(generator)


def test_an_unhashable_key_error_surfaces_unchanged_and_is_still_named():
    async def run():
        node, error = client(), KeyError(["symbol"])
        with pytest.raises(KeyError) as raised:
            await _raising(error)(node, SCOPE)
        assert raised.value is error
        entry = node.report_trace["positions"]
        assert entry["error"] == "KeyError" and entry["failures"] == 1

    asyncio.run(run())


def test_a_failing_recorder_never_replaces_the_original_exception(monkeypatch):
    def broken(error):
        raise RuntimeError("recorder fault")

    monkeypatch.setattr(client_module(), "_reason", broken)

    async def run():
        node, error = client(), ValueError("Alpaca order report requires order type and side")
        with pytest.raises(ValueError) as raised:
            await _raising(error)(node, SCOPE)
        assert raised.value is error
        entry = node.report_trace["positions"]
        assert entry["error"] == "ValueError" and entry["recorder_error"] == "RuntimeError"

    asyncio.run(run())


def test_a_count_that_cannot_be_taken_changes_no_result():
    class Uncountable(list):
        def __len__(self):
            raise RuntimeError("fixture length fault")

    async def run():
        node, result = client(), Uncountable()

        async def answer(*args, **kwargs):
            return result

        node.gateway.call = answer

        async def reads(self, command):
            return await self._call(None, "get", "/positions")

        assert await client_module()._traced("positions")(reads)(node, SCOPE) is result

    asyncio.run(run())


def test_a_malformed_trace_never_raises_from_the_node():
    broken = NS(report_trace={"orders": {"error": "ValueError", "requests": None}})
    node_order.startup_cause([broken])  # Must not raise; the run's own failure stands.


@pytest.mark.parametrize("fault_type", [RuntimeError, asyncio.CancelledError])
def test_recorder_enrichment_fault_preserves_the_generator_error(monkeypatch, fault_type):
    def broken(error):
        raise fault_type("fixture recorder fault")

    monkeypatch.setattr(client_module(), "_reason", broken)

    async def run():
        node, error = client(), ValueError("fixture generator failure")
        with pytest.raises(ValueError) as raised:
            await _raising(error)(node, SCOPE)
        assert raised.value is error
        assert node.report_trace["positions"]["recorder_error"] == fault_type.__name__
        assert client_module()._STAGE.get() is None

    asyncio.run(run())


@pytest.mark.parametrize("fails", [False, True])
def test_recorder_setup_fault_does_not_replace_the_generator_outcome(fails):
    async def run():
        node, result, error = client(), object(), ValueError("fixture generator failure")
        node.report_trace = None

        async def generator(self, command):
            if fails:
                raise error
            return result

        wrapped = client_module()._traced("positions")(generator)
        if fails:
            with pytest.raises(ValueError) as raised:
                await wrapped(node, SCOPE)
            assert raised.value is error
        else:
            assert await wrapped(node, SCOPE) is result
        assert client_module()._STAGE.get() is None

    asyncio.run(run())


@pytest.mark.parametrize("fault_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("fails", [False, True])
def test_recorder_warning_fault_preserves_the_generator_outcome(monkeypatch, fault_type, fails):
    def broken(*args, **kwargs):
        raise fault_type("fixture logger fault")

    monkeypatch.setattr(client_module(), "Logger", broken)

    async def run():
        node, result, error = client(), object(), ValueError("fixture generator failure")

        async def generator(self, command):
            self.report_trace["positions"]["skipped_unloaded"] = 1
            if fails:
                raise error
            return result

        wrapped = client_module()._traced("positions")(generator)
        if fails:
            with pytest.raises(ValueError) as raised:
                await wrapped(node, SCOPE)
            assert raised.value is error
        else:
            assert await wrapped(node, SCOPE) is result
        assert client_module()._STAGE.get() is None

    asyncio.run(run())


def test_generator_cancellation_is_recorded_and_propagates_unchanged():
    async def run():
        node, error = client(), asyncio.CancelledError("fixture generator cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await _raising(error)(node, SCOPE)
        assert raised.value is error
        assert node.report_trace["positions"]["error"] == "CancelledError"
        assert client_module()._STAGE.get() is None

    asyncio.run(run())


@pytest.mark.parametrize("fault_type", [RuntimeError, asyncio.CancelledError])
def test_row_count_fault_is_recorded_without_changing_the_result(fault_type):
    class Uncountable(list):
        def __len__(self):
            raise fault_type("fixture length fault")

    async def run():
        node, result = client(), Uncountable()

        async def answer(*args, **kwargs):
            return result

        node.gateway.call = answer

        async def generator(self, command):
            return await self._call(None, "get", "/positions")

        assert await client_module()._traced("positions")(generator)(node, SCOPE) is result
        entry = node.report_trace["positions"]
        assert entry["recorder_error"] == fault_type.__name__
        assert entry["requests"]["/positions"]["rows"] is None

    asyncio.run(run())


@pytest.mark.parametrize("fails", [False, True])
def test_request_count_fault_is_recorded_without_changing_the_call(fails):
    async def run():
        node, result, error = client(), [], ValueError("fixture gateway failure")

        async def answer(*args, **kwargs):
            if fails:
                raise error
            return result

        node.gateway.call = answer

        async def generator(self, command):
            self.report_trace["positions"]["requests"] = None
            return await self._call(None, "get", "/positions")

        wrapped = client_module()._traced("positions")(generator)
        if fails:
            with pytest.raises(ValueError) as raised:
                await wrapped(node, SCOPE)
            assert raised.value is error
        else:
            assert await wrapped(node, SCOPE) is result
        assert node.report_trace["positions"]["recorder_error"] == "AttributeError"

    asyncio.run(run())


def test_startup_trace_cancellation_is_a_recorder_fault():
    class Broken:
        @property
        def report_trace(self):
            raise asyncio.CancelledError("fixture trace cancellation")

    assert node_order.startup_cause([Broken()]) == {"recorder_error": "CancelledError"}


def test_startup_error_line_cancellation_preserves_the_trace(monkeypatch):
    class BrokenStderr:
        def write(self, text):
            raise asyncio.CancelledError("fixture output cancellation")

    entry = dict(error="ValueError", requests={})
    monkeypatch.setattr(node_order.sys, "stderr", BrokenStderr())
    trace = node_order.startup_cause([NS(report_trace={"positions": entry})])
    assert trace is not None
    assert trace["positions"] is entry

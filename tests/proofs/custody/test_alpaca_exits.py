"""Every way control leaves a command's wire await (E5 r3; review e5r2-d7a0379 P1).

On-wire evidence is recorded only at the send boundary (``_mark_sent``, the gateway's
on-wire hook), before any reply; an exit never creates it. Unsent work is refused once
with positive not-sent evidence; sent work stays for an authoritative answer. Python
asyncio: ``Task.cancel`` throws CancelledError, ``wait_for`` cancels its task on timeout,
and ``asyncio.run`` cancels remaining tasks before closing the loop (CPython 3.13 docs,
asyncio-task.html and asyncio-runner.html, read 2026-11-11). The exit table is in the
fix-round note EXIT-TABLE-r3.md.
"""

import asyncio
import gc
import threading
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest
import requests
from nautilus_trader.model import ClientOrderId, OrderSide, OrderStatus

from sextant.alpaca.execution import AlpacaExecutionClient

from .test_alpaca_client import client
from .test_alpaca_execution import command, order

METHOD = {"submit": "post", "modify": "patch", "cancel": "delete"}
PENDING = {"modify": OrderStatus.PENDING_UPDATE, "cancel": OrderStatus.PENDING_CANCEL}
HOOK = {
    "submit": AlpacaExecutionClient._submit_order,
    "modify": AlpacaExecutionClient._modify_order,
    "cancel": AlpacaExecutionClient._cancel_order,
}


def _refusal(node, kind):
    return {
        "submit": node.generate_order_rejected,
        "modify": node.generate_order_modify_rejected,
        "cancel": node.generate_order_cancel_rejected,
    }[kind]


def _cmd(kind):
    if kind == "submit":
        return NS(order=order(client_order_id=ClientOrderId("fixture-next")), params={})
    from nautilus_trader.model import Price

    price = Price.from_str("0.1235") if kind == "modify" else None
    return command(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None, price=price)


async def _park(node, kind, *, sent):
    """Start one house command and hold it before (unsent) or on (sent) the wire."""
    await node._submit_order(NS(order=order(), params={}))  # The house order to mutate.
    node.generate_order_rejected.reset_mock()
    venue = node.gateway.sdk
    release, entered = threading.Event(), threading.Event()
    if sent:

        def held(path, body):
            venue.requests.append((METHOD[kind].upper(), path, dict(body or {})))
            entered.set()
            release.wait(2)
            raise requests.ConnectionError("fixture reply lost")

        setattr(venue, METHOD[kind], held)
    else:
        node.gateway.pacer.bucket.tokens = 0
    await HOOK[kind](node, cast(Any, _cmd(kind)))
    (task,) = [t for t in node._actions if not t.done()]
    if sent:
        assert await asyncio.to_thread(entered.wait, 1)
    else:
        for _ in range(100):
            if node.gateway.pacer.queue:
                break
            await asyncio.sleep(0.001)
        assert node.gateway.pacer.queue
    return task, release


async def _leave(node, task, how, release=None):
    if how == "disconnect":
        if release is not None:  # Disconnect joins wire work: the reply arrives meanwhile.
            asyncio.get_running_loop().call_later(0.03, release.set)
        await asyncio.wait_for(node._disconnect(), 2)
    elif how == "task_cancel":  # Node stop or dispose cancelling supervised work.
        task.cancel()
    else:  # A timeout wrapper: wait_for cancels the task it times out.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 0.01)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)


async def _read(node, kind):
    node.cache.order.return_value = order(status=PENDING[kind])
    await node._generate_order_status_report(
        NS(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
    )


HOWS = ["disconnect", "task_cancel", "wait_for"]


def _unsent(kind, how):
    async def run():
        node = client()
        task, _ = await _park(node, kind, sent=False)
        await _leave(node, task, how)
        assert task.cancelled()
        _refusal(node, kind).assert_called_once()
        assert not any(r[0] == METHOD[kind].upper() for r in node.gateway.sdk.requests[1:])
        assert not node._cancels and not node._commands

    asyncio.run(run())


def _sent(kind, how):
    async def run():
        node = client()
        task, release = await _park(node, kind, sent=True)
        try:
            await _leave(node, task, how, release)
        finally:
            release.set()
        _refusal(node, kind).assert_not_called()  # Never rejected, never locally closed.
        if kind == "cancel":
            row_id = next(iter(node.gateway.sdk.rows))
            assert node._cancels == {row_id}  # Recorded at the send boundary.
            await asyncio.gather(*tuple(node.gateway.tasks))  # the DELETE's own call ends
            await _read(node, kind)  # The authoritative read restores the working order.
            node.generate_order_cancel_rejected.assert_called_once()
            assert not node._cancels

    asyncio.run(run())


def _first_step(kind):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        node.generate_order_rejected.reset_mock()
        await HOOK[kind](node, cast(Any, _cmd(kind)))  # Scheduled, never stepped.
        await node._disconnect()
        if kind == "submit":  # Never reported submitted: denied, not rejected.
            node.generate_order_denied.assert_called_once()
        else:
            node.generate_order_cancel_rejected.assert_called_once()
        assert [r[0] for r in node.gateway.sdk.requests] == ["POST"]

    asyncio.run(run())


def _teardown(kind):
    node = client()
    holder = {}

    async def run():
        holder["task"], _ = await _park(node, kind, sent=False)

    asyncio.run(run())  # Returns with the command parked: the runner cancels it.
    assert holder["task"].cancelled()
    _refusal(node, kind).assert_called_once()


def _closing(kind):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        node.generate_order_rejected.reset_mock()
        node._closing.set()
        with pytest.raises(RuntimeError, match="closing"):
            await HOOK[kind](node, cast(Any, _cmd(kind)))
        if kind == "submit":
            node.generate_order_denied.assert_called_once()
        else:
            node.generate_order_cancel_rejected.assert_called_once()

    asyncio.run(run())


def _api(status):
    from alpaca.common.exceptions import APIError

    response = requests.Response()
    response.status_code = status
    return APIError('{"message": "fixture"}', requests.HTTPError(response=response))


def _venue_4xx(kind):
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        node.generate_order_rejected.reset_mock()
        venue = node.gateway.sdk

        def refused(path, body):
            venue.requests.append((METHOD[kind].upper(), path, dict(body or {})))
            raise _api(422)

        setattr(venue, METHOD[kind], refused)
        await HOOK[kind](node, cast(Any, _cmd(kind)))
        await asyncio.gather(*tuple(node._actions))
        _refusal(node, kind).assert_called_once()
        assert not node._cancels

    asyncio.run(run())


def _unscheduled(kind):
    """No task exists, so no done callback can refuse it: the hook refuses (review e5max)."""

    async def run():
        node = client()
        if kind != "submit":
            await node._submit_order(NS(order=order(), params={}))
        sent = len(node.gateway.sdk.requests)

        def refuse(coroutine, name):
            raise RuntimeError("fixture: scheduling failed")

        node.create_task = refuse
        with pytest.raises(RuntimeError, match="scheduling failed"):
            await HOOK[kind](node, cast(Any, _cmd(kind)))
        refused = node.generate_order_denied if kind == "submit" else _refusal(node, kind)
        refused.assert_called_once()
        assert len(node.gateway.sdk.requests) == sent

    asyncio.run(run())


@pytest.mark.parametrize("how", HOWS)
def test_unsent_submit_leaving_by_cancellation_is_refused_once(how):
    _unsent("submit", how)


@pytest.mark.parametrize("how", HOWS)
def test_sent_submit_leaving_by_cancellation_is_never_rejected(how):
    _sent("submit", how)


def test_submit_cancelled_before_its_first_step_is_denied():
    _first_step("submit")


def test_runner_teardown_refuses_an_unsent_submit():
    _teardown("submit")


def test_task_destroyed_pending_at_loop_close_emits_no_refusal():
    node = client()
    loop = asyncio.new_event_loop()
    node.create_task = lambda coroutine, name: loop.create_task(coroutine)
    loop.run_until_complete(HOOK["submit"](node, cast(Any, _cmd("submit"))))
    loop.close()  # The task is destroyed pending; the process-level loop is gone.
    gc.collect()
    node.generate_order_denied.assert_not_called()
    node.generate_order_rejected.assert_not_called()
    assert not node.gateway.sdk.requests


def test_submit_hook_while_closing_is_denied():
    _closing("submit")


def test_a_venue_4xx_after_a_sent_submit_is_a_rejection():
    _venue_4xx("submit")


def test_a_submit_that_cannot_be_scheduled_is_denied_once():
    _unscheduled("submit")


def test_a_submit_refused_after_enqueue_never_reaches_the_wire():
    """Admission raises once the POST is queued (review of PR 552): the refusal withdraws it."""

    async def run():
        node, calls = client(), []
        counts = node.gateway.position_counts

        def raise_once():  # call 1: the pacer's reserve check; call 2: admission at drain
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("fixture: admission failed after enqueue")
            return counts()

        node.gateway.position_counts = raise_once
        await HOOK["submit"](node, cast(Any, _cmd("submit")))
        await asyncio.gather(*tuple(node._actions), return_exceptions=True)
        node.generate_order_rejected.assert_called_once()
        node.gateway.pacer.drain()  # any later drain, from any caller
        await asyncio.gather(*tuple(node.gateway.tasks))  # the wire call runs in a thread
        assert not node.gateway.sdk.requests, "a rejected submit reached the venue"
        assert not node.gateway.pacer.queue

    asyncio.run(run())


@pytest.mark.parametrize("how", HOWS)
def test_unsent_cancel_leaving_by_cancellation_is_refused_once(how):
    _unsent("cancel", how)


@pytest.mark.parametrize("how", HOWS)
def test_sent_cancel_leaving_by_cancellation_keeps_its_evidence(how):
    _sent("cancel", how)


def test_cancel_cancelled_before_its_first_step_is_refused():
    _first_step("cancel")


def test_runner_teardown_refuses_an_unsent_cancel():
    _teardown("cancel")


def test_cancel_hook_while_closing_is_refused():
    _closing("cancel")


def test_a_venue_4xx_after_a_sent_cancel_is_a_refusal():
    _venue_4xx("cancel")


def test_an_acknowledged_delete_keeps_its_evidence_until_the_venue_confirms():
    # Alpaca DELETE "Attempts to cancel an Open Order" (204): an attempt, not a cancel.
    async def run():
        node = client()
        await node._submit_order(NS(order=order(side=OrderSide.BUY), params={}))
        await node._cancel_order(_cmd("cancel"))
        assert node.gateway.sdk.requests[-1][0] == "DELETE"
        row_id = next(iter(node.gateway.sdk.rows))
        assert node._cancels == {row_id}  # The 204 did not resolve it.
        await _read(node, "cancel")  # The fake venue canceled it: confirmed terminal.
        assert not node._cancels
        node.generate_order_cancel_rejected.assert_not_called()

    asyncio.run(run())


def test_a_cancel_that_cannot_be_scheduled_is_refused_once():
    _unscheduled("cancel")


def _refused_after_enqueue(kind):
    """The mutation's admission raises once it is queued: refused once, never sent."""

    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        admitted, calls = node._mutation_admitted, []

        def raise_at_drain(command, row):  # call 1: the command's read; call 2: the pacer
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("fixture: admission failed after enqueue")
            return admitted(command, row)

        node._mutation_admitted = raise_at_drain
        node.gateway.pacer.bucket.tokens = 0  # queued behind the pacer
        await HOOK[kind](node, cast(Any, _cmd(kind)))
        for _ in range(50):
            await asyncio.sleep(0.001)
            if node.gateway.pacer.queue:
                break
        node.gateway.pacer.bucket.tokens = node.gateway.pacer.bucket.burst
        node.gateway.pacer.drain()
        await asyncio.gather(*tuple(node._actions), return_exceptions=True)
        node.gateway.pacer.drain()  # any later drain, from any caller
        await asyncio.gather(*tuple(node.gateway.tasks))
        assert len(calls) == 2
        _refusal(node, kind).assert_called_once()
        assert not [r for r in node.gateway.sdk.requests if r[0] == METHOD[kind].upper()]
        assert not node.gateway.pacer.queue

    asyncio.run(run())


def test_a_cancel_refused_after_enqueue_never_reaches_the_wire():
    _refused_after_enqueue("cancel")


@pytest.mark.parametrize("exit_by", ["cancelled_sent", "venue_4xx"])
def test_modify_specific_exits_keep_or_release_the_proposal(exit_by):
    # Unsent, first-step, closing and teardown exits are the same code as submit/cancel.
    async def run():
        node = client()
        if exit_by == "venue_4xx":
            await node._submit_order(NS(order=order(), params={}))
            node.gateway.sdk.patch = lambda path, body: (_ for _ in ()).throw(_api(422))
            await node._modify_order(_cmd("modify"))
            node.generate_order_modify_rejected.assert_called_once()
            assert not node._proposals  # The venue refused: the reservation is released.
            return
        task, release = await _park(node, "modify", sent=True)
        try:
            await _leave(node, task, "disconnect", release)
        finally:
            release.set()
        node.generate_order_modify_rejected.assert_not_called()
        assert list(node._proposals) == ["fixture-entry-r1"]  # Kept for an answer.
        await _read(node, "modify")  # The order is unchanged at the venue: restored.
        node.generate_order_modify_rejected.assert_called_once()
        assert not node._proposals

    asyncio.run(run())


def test_a_modify_that_cannot_be_scheduled_is_refused_once():
    _unscheduled("modify")


def test_a_modify_refused_after_enqueue_never_reaches_the_wire():
    _refused_after_enqueue("modify")

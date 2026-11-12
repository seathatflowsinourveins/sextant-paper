"""One command resolves exactly once, whichever authoritative answer comes first (E5 u10).

Review e5r3-99ac5bf P2: a read, a stream event or the venue's response settles the live
command it answers; a later answer for the same command emits nothing and is counted.
"""

import asyncio
import threading
from types import SimpleNamespace as NS
from typing import Any, cast

from nautilus_trader.model import ClientOrderId, OrderStatus

from .test_alpaca_client import client
from .test_alpaca_execution import order
from .test_alpaca_exits import HOOK, METHOD, _api, _cmd


def _held_then_422(venue, kind, *, applied):
    entered, release = threading.Event(), threading.Event()
    original = getattr(venue, METHOD[kind])

    def held(path, body):
        if applied:
            original(path, body)  # The venue holds the order while the reply is pending.
        else:
            venue.requests.append((METHOD[kind].upper(), path, dict(body or {})))
        entered.set()
        release.wait(2)
        raise _api(422)

    setattr(venue, METHOD[kind], held)
    return entered, release


async def _first_answer_then_422(kind, answer):
    node = client()
    if kind != "submit":
        await node._submit_order(NS(order=order(), params={}))
    node.generate_order_rejected.reset_mock()
    venue = node.gateway.sdk
    entered, release = _held_then_422(venue, kind, applied=kind == "submit")
    await HOOK[kind](node, cast(Any, _cmd(kind)))
    (task,) = [t for t in node._actions if not t.done()]
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await answer(node)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        release.set()
    return node


def test_a_submit_the_venue_already_shows_is_not_rejected_by_a_late_422():
    async def run():
        async def read(node):
            report = await node._generate_order_status_report(
                NS(client_order_id=ClientOrderId("fixture-next"), venue_order_id=None)
            )
            assert report is not None

        node = await _first_answer_then_422("submit", read)
        node.generate_order_rejected.assert_not_called()
        assert node.late_answers == 1

    asyncio.run(run())


def test_a_read_during_a_running_patch_settles_nothing_and_its_own_later_read_refuses_once():
    # PR 569 rule: a read issued while the PATCH runs answers nothing. Its 422 is ambiguous
    # (E5 u12: PATCH documents no refusal), so a read issued after its call ended answers it.
    async def run():
        async def read(node):
            node.cache.order.return_value = order(status=OrderStatus.PENDING_UPDATE)
            await node._generate_order_status_report(
                NS(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
            )
            node.generate_order_modify_rejected.assert_not_called()

        node = await _first_answer_then_422("modify", read)
        node.generate_order_modify_rejected.assert_not_called()
        await node._generate_order_status_report(QUERY)
        node.generate_order_modify_rejected.assert_called_once()
        assert node.late_answers == 0 and not node._proposals

    asyncio.run(run())


def test_a_cancel_the_stream_refused_is_not_rejected_again_by_a_late_422():
    async def run():
        async def stream(node):
            row = next(iter(node.gateway.sdk.rows.values()))
            await node._on_trade_update(
                {
                    "stream": "trade_updates",
                    "data": {
                        "event": "order_cancel_rejected",
                        "order": row,
                        "timestamp": row["updated_at"],
                    },
                }
            )
            node.generate_order_cancel_rejected.assert_called_once()

        node = await _first_answer_then_422("cancel", stream)
        node.generate_order_cancel_rejected.assert_called_once()
        assert node.late_answers == 1

    asyncio.run(run())


# PR 569 gate review: answers belong to one command attempt, never to (kind, order).
QUERY = NS(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)


def _event(event, row):
    data = {"event": event, "order": row, "timestamp": row["updated_at"]}
    return {"stream": "trade_updates", "data": data}


async def _working(node):
    await node._submit_order(NS(order=order(), params={}))
    venue = node.gateway.sdk
    node.cache.order.return_value = order(status=OrderStatus.PENDING_CANCEL)
    venue.delete = lambda path, body: venue.requests.append(("DELETE", path, {}))  # 204
    return next(iter(venue.rows.values()))


async def _sent(node):
    await HOOK["cancel"](node, cast(Any, _cmd("cancel")))
    await asyncio.gather(*tuple(node._actions))  # the DELETE is on the wire


def test_a_retried_cancel_is_refused_on_its_own_answer():
    async def run():
        node = client()
        row = await _working(node)
        await _sent(node)
        await node._generate_order_status_report(QUERY)  # still working: attempt 1 refused
        await node._on_trade_update(_event("order_cancel_rejected", row))  # 1's, late
        await _sent(node)  # attempt 2
        await node._on_trade_update(_event("order_cancel_rejected", row))
        assert node.generate_order_cancel_rejected.call_count == 2 and not node._cancels

    asyncio.run(run())


def test_a_read_issued_before_a_delete_never_answers_it():
    async def run():
        node = client()
        row = await _working(node)
        entered, release, call = asyncio.Event(), asyncio.Event(), node._call

        async def late(kind, method, path, data=None, **kwargs):
            if method == "get" and not entered.is_set():
                entered.set()
                await release.wait()
            return await call(kind, method, path, data, **kwargs)

        node._call = late
        read = asyncio.create_task(node._generate_order_status_report(QUERY))
        await asyncio.wait_for(entered.wait(), 1)
        await _sent(node)  # its DELETE is newer than the read
        release.set()
        await read
        node.generate_order_cancel_rejected.assert_not_called()
        assert node._cancels == {row["id"]}

    asyncio.run(run())


def test_a_late_422_after_a_terminal_event_refuses_nothing():
    async def run():
        async def canceled(node):
            row = next(iter(node.gateway.sdk.rows.values()))
            await node._on_trade_update(_event("canceled", row | {"status": "canceled"}))

        node = await _first_answer_then_422("cancel", canceled)
        node.generate_order_cancel_rejected.assert_not_called()

    asyncio.run(run())


# PR 569, second gate review: one unresolved cancel per order, answered only by its own answer.
def test_a_late_refusal_owed_to_a_settled_attempt_never_answers_the_next():
    async def run():
        node = client()
        row = await _working(node)
        await _sent(node)  # attempt A
        await node._generate_order_status_report(QUERY)  # working: A refused, its refusal owed
        await _sent(node)  # attempt B
        await node._on_trade_update(_event("order_cancel_rejected", row))  # A's, delayed
        node.generate_order_cancel_rejected.assert_called_once()
        assert node._cancels == {row["id"]}  # B is still unresolved

    asyncio.run(run())


def test_a_read_during_a_running_delete_settles_nothing():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        node.cache.order.return_value = order(status=OrderStatus.PENDING_CANCEL)
        entered, release = threading.Event(), threading.Event()
        venue.delete = lambda path, body: (entered.set(), release.wait(2), None)[2]
        await HOOK["cancel"](node, cast(Any, _cmd("cancel")))
        assert await asyncio.to_thread(entered.wait, 1)  # the DELETE is running
        await node._generate_order_status_report(QUERY)  # the order still shows new
        release.set()
        await asyncio.gather(*tuple(node._actions))  # 204
        node.generate_order_cancel_rejected.assert_not_called()
        assert node._cancels == {next(iter(venue.rows))}

    asyncio.run(run())


def test_a_second_cancel_while_one_is_unresolved_is_not_sent():
    async def run():
        node = client()
        row = await _working(node)
        venue = node.gateway.sdk
        venue.delete = lambda path, body: venue.requests.append(
            ("DELETE", path, row.update(status="pending_cancel"))
        )
        await _sent(node)
        await _sent(node)  # Coinbase: a re-cancel risks CancelRejected flipping the order
        assert [r[0] for r in venue.requests].count("DELETE") == 1
        node.generate_order_cancel_rejected.assert_not_called()

    asyncio.run(run())


def test_an_unpaid_refusal_debt_leaves_the_next_attempt_to_its_own_read():
    async def run():
        node = client()
        row = await _working(node)
        await _sent(node)
        await node._generate_order_status_report(QUERY)  # A refused; A's refusal never comes
        await _sent(node)  # attempt B
        await node._on_trade_update(_event("order_cancel_rejected", row))  # B's, absorbed
        assert node._cancels == {row["id"]}
        await node._generate_order_status_report(QUERY)  # B's own read settles it
        assert node.generate_order_cancel_rejected.call_count == 2 and not node._cancels

    asyncio.run(run())


def test_a_read_issued_before_a_patch_never_answers_it():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        node.cache.order.return_value = order(status=OrderStatus.PENDING_UPDATE)
        venue.patch = lambda path, body: venue.requests.append(("PATCH", path, {}))
        entered, release, call = asyncio.Event(), asyncio.Event(), node._call

        async def late(kind, method, path, data=None, **kwargs):
            if method == "get" and not entered.is_set():
                entered.set()
                await release.wait()
            return await call(kind, method, path, data, **kwargs)

        node._call = late
        read = asyncio.create_task(node._generate_order_status_report(QUERY))
        await asyncio.wait_for(entered.wait(), 1)
        await HOOK["modify"](node, cast(Any, _cmd("modify")))  # its PATCH is newer
        await asyncio.gather(*tuple(node._actions))
        release.set()
        await read
        node.generate_order_modify_rejected.assert_not_called()
        assert node._proposals

    asyncio.run(run())


def test_a_late_replace_refusal_owed_to_a_settled_patch_never_answers_the_next():
    async def run():
        node = client()
        await node._submit_order(NS(order=order(), params={}))
        venue = node.gateway.sdk
        row = next(iter(venue.rows.values()))
        node.cache.order.return_value = order(status=OrderStatus.PENDING_UPDATE)
        venue.patch = lambda path, body: venue.requests.append(("PATCH", path, {}))
        for _ in range(2):  # PATCH 1, settled by a read; then PATCH 2
            await HOOK["modify"](node, cast(Any, _cmd("modify")))
            await asyncio.gather(*tuple(node._actions))
            if not node.generate_order_modify_rejected.called:
                await node._generate_order_status_report(QUERY)  # unchanged: 1 refused, owed
        await node._on_trade_update(_event("order_replace_rejected", row))  # 1's, delayed
        node.generate_order_modify_rejected.assert_called_once()
        assert node._proposals  # PATCH 2 is still unresolved

    asyncio.run(run())

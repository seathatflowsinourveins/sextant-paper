import asyncio
from threading import Event
from types import SimpleNamespace

import requests
from nautilus_trader.model import ClientOrderId, OrderSide, OrderStatus, Quantity

from .test_alpaca_client import client
from .test_alpaca_execution import command, order


def test_disconnect_during_sent_delete_restores_working_order_on_read():
    async def run():
        node = client()
        exit_order = order(side=OrderSide.SELL, is_sell=True, filled_qty=Quantity.from_str("0"))
        await node._submit_order(SimpleNamespace(order=exit_order, params={}))
        venue = node.gateway.sdk
        entered, release = Event(), Event()

        def lose_delete_after_disconnect(path, body):
            venue.requests.append(("DELETE", path, dict(body or {})))
            entered.set()
            if not release.wait(2):
                raise AssertionError("fixture did not release the fake DELETE")
            raise requests.ConnectionError("fixture connection lost while disconnecting")

        venue.delete = lose_delete_after_disconnect
        node.cache.order.return_value = order(
            side=OrderSide.SELL,
            is_sell=True,
            filled_qty=Quantity.from_str("0"),
            status=OrderStatus.PENDING_CANCEL,
        )
        cancel = asyncio.create_task(
            node._cancel_order(
                command(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            asyncio.get_running_loop().call_later(0.03, release.set)
            await asyncio.wait_for(node._disconnect(), 1)
            result = await asyncio.gather(cancel, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            report = await node._generate_order_status_report(
                SimpleNamespace(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
            )
            assert next(iter(venue.rows.values()))["status"] == "new"
            assert report.order_status == OrderStatus.ACCEPTED
            print(
                f"DELETE sent; cancel markers={node._cancels}; venue=new; "
                f"cancel rejections={node.generate_order_cancel_rejected.call_count}"
            )
            node.generate_order_cancel_rejected.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(cancel, return_exceptions=True)
            await node.gateway.close()

    asyncio.run(run())

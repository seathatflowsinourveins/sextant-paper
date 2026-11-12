# Reviewer witness, unchanged in logic: formatting, import order and these pragma lines only.
# pyright: reportArgumentType=false
import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from nautilus_trader.model import ClientOrderId, OrderStatus

from sextant.alpaca.execution import IncompleteCoverage

from .test_alpaca_client import client
from .test_alpaca_execution import command, order


def test_a_read_and_late_venue_4xx_refuse_the_same_cancel_only_once():
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        entered, release = Event(), Event()
        node.cache.order.return_value = order(status=OrderStatus.PENDING_CANCEL)

        def refused_delete(path, body):
            venue.requests.append(("DELETE", path, dict(body or {})))
            entered.set()
            if not release.wait(2):
                raise AssertionError("fixture did not release DELETE")
            from requests import Response
            from requests.exceptions import HTTPError

            response = Response()
            response.status_code = 422
            raise APIError(
                {"code": 42210000, "message": "fixture refusal"}, HTTPError(response=response)
            )

        venue.delete = refused_delete
        cancel = asyncio.create_task(
            node._cancel_order(
                command(client_order_id=ClientOrderId("fixture-entry"), venue_order_id=None)
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            report = await node._generate_order_status_report(
                SimpleNamespace(
                    client_order_id=ClientOrderId("fixture-entry"),
                    venue_order_id=None,
                )
            )
            assert report.order_status == OrderStatus.ACCEPTED
            print(f"before 4xx: rejections={node.generate_order_cancel_rejected.call_count}")
            release.set()
            await cancel
            print(f"after 4xx: rejections={node.generate_order_cancel_rejected.call_count}")
            node.generate_order_cancel_rejected.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(cancel, return_exceptions=True)
            await node._disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("malformed", [{"qty": "5"}, None, "unexpected-row"])
def test_windowed_listing_retains_good_sibling_and_bad_payload(malformed):
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        good = next(iter(venue.rows.values()))
        original_get = venue.get

        def listed_rows(path, params):
            if path == "/orders":
                return [good, malformed]
            return original_get(path, params)

        venue.get = listed_rows
        try:
            with pytest.raises(IncompleteCoverage) as failure:
                await node._generate_order_status_reports(
                    SimpleNamespace(
                        open_only=False,
                        instrument_id=None,
                        start=1,
                        end=None,
                    )
                )
            assert len(failure.value.reports) == 1
            assert any(payload is malformed for payload, _ in failure.value.failures)
            assert not node.coverage_complete
        finally:
            await node._disconnect()

    asyncio.run(run())

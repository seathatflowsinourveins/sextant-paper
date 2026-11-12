import asyncio
from types import SimpleNamespace

import pytest

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import order


def test_malformed_order_row_keeps_good_sibling_and_payload():
    async def run():
        node = client()
        await node._submit_order(SimpleNamespace(order=order(), params={}))
        venue = node.gateway.sdk
        good = next(iter(venue.rows.values()))
        malformed = {"qty": "5"}
        original_get = venue.get

        def listed_rows(path, params):
            if path == "/orders":
                return [good, malformed]
            return original_get(path, params)

        venue.get = listed_rows
        try:
            with pytest.raises(client_module().IncompleteCoverage) as failure:
                await node._generate_order_status_reports(
                    SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
                )
            assert len(failure.value.reports) == 1
            assert failure.value.failures[0][0] == malformed
            assert not node.coverage_complete
        finally:
            await node._disconnect()

    asyncio.run(run())

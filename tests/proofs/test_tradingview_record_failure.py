"""LEAD-1904 P2: the review's four recorder/classifier injections, offline."""

import asyncio
import errno
import json

import httpx2
import pytest

from sextant.tradingview import account, stop_record


async def local_request(item, mode, error):
    if mode == "in_process":

        async def fail_local():
            raise error

        return await item.request(fail_local)
    gate = account.HTTPGate()

    def fail_http(request):
        raise error

    async with httpx2.AsyncClient(
        transport=account.GuardedHTTPTransport(httpx2.MockTransport(fail_http), gate),
        event_hooks={"request": [gate.request], "response": [gate.response]},
    ) as client:
        return await client.post("https://offline.invalid/mcp", json={"method": "tools/call"})


@pytest.mark.parametrize("mode", ["in_process", "http"])
def test_record_write_failure_preserves_the_original_exception(monkeypatch, mode):
    item = account.Account()
    original = httpx2.ReadTimeout("synthetic upstream failure")
    write_atomic = account.write_atomic
    capacity = len(json.dumps(item.snapshot())) + 64

    def broken_record_write(path, state):
        if len(json.dumps(state)) > capacity:
            raise OSError(errno.ENOSPC, "synthetic stop-record capacity exhausted")
        write_atomic(path, state)

    monkeypatch.setattr(account, "write_atomic", broken_record_write)
    monkeypatch.setattr(stop_record, "write_atomic", broken_record_write)
    caught = None
    try:
        asyncio.run(local_request(item, mode, original))
    except BaseException as error:
        caught = error
    state = item.snapshot()
    assert state["stopped"] is True and state["quota_stop"] is True
    assert state["calls"] == 1
    assert caught is original


@pytest.mark.parametrize("mode", ["in_process", "http"])
def test_record_classification_failure_preserves_the_original_exception(monkeypatch, mode):
    item = account.Account()
    original = httpx2.ReadTimeout("synthetic upstream failure")

    def broken_classifier(error):
        raise RuntimeError("synthetic classification failure")

    monkeypatch.setattr(account, "exception_rule", broken_classifier)
    caught = None
    try:
        asyncio.run(local_request(item, mode, original))
    except BaseException as error:
        caught = error
    state = item.snapshot()
    assert state["stopped"] is True and state["quota_stop"] is True
    assert state["calls"] == 1
    assert caught is original

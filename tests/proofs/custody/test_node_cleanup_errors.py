"""Worker teardown failures must fail the proof caller (8f4de96 max-gate P1)."""

import threading

import pytest

from . import test_alpaca_fill_history as history
from . import test_alpaca_inflight as inflight
from . import test_alpaca_node_owner as owner
from .alpaca_venue import Venue as FakeVenue

FAILURE = "MUTANT_STOP_FAILURE"


@pytest.fixture
def broken_disposal():
    events = []

    class Node:
        @staticmethod
        def build(*args, **kwargs):
            events.append(("build", threading.get_ident()))
            return Node()

        def add_strategy(self, strategy):
            pass

        async def run_async(self):
            events.append(("run_returned", threading.get_ident()))

        def dispose(self):
            events.append(("dispose", threading.get_ident()))
            raise RuntimeError(FAILURE)

        def __del__(self):
            events.append(("drop", threading.get_ident()))

    return Node, events


@pytest.mark.parametrize("module", [owner, inflight], ids=["owner", "inflight"])
def test_worker_disposal_failure_reaches_the_proof_caller(monkeypatch, broken_disposal, module):
    node, events = broken_disposal
    monkeypatch.setattr(module, "LiveNode", node)
    seen = module._run_node(FakeVenue()) if module is owner else module._run(FakeVenue(), "patch")
    assert seen.get("error") == FAILURE, (seen, events)
    assert [kind for kind, _ in events] == ["build", "run_returned", "dispose", "drop"]
    assert all(thread == events[0][1] != threading.get_ident() for _, thread in events)


def test_caller_thread_disposal_failure_still_propagates(monkeypatch, broken_disposal):
    node, events = broken_disposal
    monkeypatch.setattr(history, "LiveNode", node)
    with pytest.raises(RuntimeError, match=FAILURE):
        history._start(FakeVenue())
    assert [kind for kind, _ in events[:3]] == ["build", "run_returned", "dispose"]
    assert all(thread == threading.get_ident() for _, thread in events)

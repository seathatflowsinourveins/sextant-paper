"""E5 u12: an order is never rejected locally while it may be live at the venue.

After send, only the broker's documented refusals are definite (Alpaca reference pages, read
2026-11-11, updatedAt 2026-05-27): POST /v2/orders 403 "Buying power or shares is not
sufficient." and 422 "Input parameters are not recognized."; DELETE /v2/orders/{order_id} 422
"The order status is not cancelable."; PATCH /v2/orders/{order_id} documents only 200.
Everything else after send is ambiguous, an undocumented 4xx and a POST's 429 included, as
the official Coinbase client treats a submit's 429 (coinbase/src/execution.rs:1473-1478)
under the engine's failure contract (crates/live/src/execution/failure.rs:28-42). The engine
retains an unacknowledged submission rather than rejecting it
(SubmissionRecoveryPolicy.RETAIN_UNRESOLVED, crates/live/src/execution/submission.rs:45-50).
"""

import asyncio
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest
from nautilus_trader.live import SubmissionRecoveryPolicy

from .test_alpaca_client import client, client_module
from .test_alpaca_execution import order
from .test_alpaca_exits import HOOK, METHOD, _api, _cmd, _refusal

AMBIGUOUS = [429, 400, 404, 409]


def _answer(kind, status):
    async def run():
        node = client()
        if kind != "submit":
            await node._submit_order(NS(order=order(), params={}))
        node.generate_order_rejected.reset_mock()
        venue = node.gateway.sdk

        def answer(path, body):
            venue.requests.append((METHOD[kind].upper(), path, dict(body or {})))
            raise _api(status)

        setattr(venue, METHOD[kind], answer)
        await HOOK[kind](node, cast(Any, _cmd(kind)))
        await asyncio.gather(*tuple(node._actions), return_exceptions=True)
        return node

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (k, s)
        for k in ("submit", "modify", "cancel")
        for s in AMBIGUOUS
        if k == "submit" or s != 429
    ],
)
def test_a_429_or_undocumented_4xx_after_send_is_ambiguous(kind, status):
    node = _answer(kind, status)
    _refusal(node, kind).assert_not_called()  # it may be live: never a local refusal
    if kind == "cancel":
        assert node._cancels  # the attempt stays unresolved for its own answer
    if kind == "modify":
        assert node._proposals


def test_a_patch_4xx_is_ambiguous_the_broker_documents_no_refusal():
    node = _answer("modify", 422)
    node.generate_order_modify_rejected.assert_not_called()
    assert node._proposals


@pytest.mark.parametrize(("kind", "status"), [("submit", 403), ("submit", 422), ("cancel", 422)])
def test_a_documented_refusal_stays_definite(kind, status):
    node = _answer(kind, status)
    _refusal(node, kind).assert_called_once()


@pytest.mark.parametrize("kind", ["modify", "cancel"])
def test_a_429_on_a_replacement_or_a_cancel_is_a_refusal(kind):  # Gate e5u12-4b94349 P1.
    node = _answer(kind, 429)
    _refusal(node, kind).assert_called_once()
    assert not node._proposals and not node._cancels


def test_the_engine_retains_unresolved_submissions():
    build = client_module().execution_engine_config
    retain = SubmissionRecoveryPolicy.RETAIN_UNRESOLVED
    assert build(universe=["TEST.ALPACA"]).submission_recovery_policy == retain
    # The order node names the policy itself (node_order.py): the same config, no TypeError.
    assert build(universe=["TEST.ALPACA"], submission_recovery_policy=retain)
    with pytest.raises(ValueError, match="retain"):
        build(
            universe=["TEST.ALPACA"],
            submission_recovery_policy=SubmissionRecoveryPolicy.RESOLVE_LOCALLY,
        )

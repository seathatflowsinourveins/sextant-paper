"""Pin alpaca-py 0.44.0 entry wire/retry behavior without opening a connection."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from requests import Response
from requests.exceptions import InvalidJSONError, Timeout
from test_b_entry_reserve_review import buy
from test_b_entry_sdk import clients
from test_b_protector import api_order

from sextant.execution.pacer import ActionKind, PacerRefused
from sextant.paper.b_protector import TradingClientLike
from sextant.paper.b_protector_main import PacedTradingClient

FIXTURE = "review-fixture"


@pytest.mark.parametrize("status", [200, 429, 504])
def test_entry_http_attempt_is_one_wire_without_sleep_and_protection_is_separate(
    monkeypatch, status
):
    primary, entry, pacer = clients()
    original = primary._retry
    calls, protective, sleeps = [], [], []
    encoded = api_order(client_order_id="B001r-sdk", qty="5").model_dump_json().encode()

    def respond(request, **kwargs):
        calls.append(request.method)
        response = Response()
        response.status_code = status if len(calls) == 1 else 200
        response._content = encoded
        return response

    def protect(method, url, **kwargs):
        protective.append(method)
        response = Response()
        response.status_code, response._content = 204, b""
        return response

    monkeypatch.setattr(entry._session, "send", respond)
    monkeypatch.setattr(primary._session, "request", protect)
    monkeypatch.setattr("alpaca.common.rest.time.sleep", sleeps.append)
    wrapper = PacedTradingClient(cast(TradingClientLike, primary), pacer, entry_client=entry)
    if status == 200:
        wrapper.submit_order(buy(), kind=ActionKind.SUBMIT)
    else:
        with pytest.raises(PacerRefused, match="generation"):
            wrapper.submit_order(buy(), kind=ActionKind.SUBMIT)
    assert calls == ["POST"] and sleeps == []
    assert wrapper._entry_charges == {"B001r-review": status != 429}
    with pytest.raises(PacerRefused):
        wrapper.submit_order(buy(), kind=ActionKind.SUBMIT)
    assert calls == ["POST"] and sleeps == []
    assert pacer.wire_requests == 1 and pacer.wire_actions[ActionKind.SUBMIT] == 1
    assert pacer.queue == []
    wrapper.cancel_order_by_id("00000000-0000-4000-8000-000000000001")
    assert protective == ["DELETE"] and primary._retry == original
    assert calls == ["POST"] and sleeps == [] and pacer.wire_requests == 2


def test_unavailable_entry_client_refuses_without_wire():
    primary, _entry, pacer = clients()
    wrapper = PacedTradingClient(cast(TradingClientLike, primary), pacer)
    with pytest.raises(PacerRefused):
        wrapper.submit_order(buy(), kind=ActionKind.SUBMIT)
    assert pacer.wire_requests == 0


def test_main_keeps_protection_running_when_entry_sdk_loses_retry_attribute(tmp_path, monkeypatch):
    from test_b_entry_reserve_review import WitnessBroker
    from test_b_protector_main import write_config

    from sextant.paper import b_protector_main as module

    _primary, entry, _pacer = clients()
    del entry._retry
    calls, pages = [], []
    monkeypatch.setattr(
        module, "ProtectorStream", lambda *a, **kw: SimpleNamespace(run=lambda: calls.append("run"))
    )
    monkeypatch.setenv("ALPACA_PAPER_2_API_KEY", "review-fixture")
    monkeypatch.setenv("ALPACA_PAPER_2_SECRET_KEY", "review-fixture")
    monkeypatch.setenv("B_EXPECTED_ACCOUNT_SUFFIX", "ture")
    monkeypatch.delenv("B_EXPECTED_ACCOUNT_ID", raising=False)
    assert (
        module.main(
            ["--config", str(write_config(tmp_path, "b.json"))],
            client_factory=lambda **kw: WitnessBroker(),
            entry_factory=lambda **kw: cast(TradingClientLike, entry),
            data_client_factory=cast(Any, lambda **kw: None),
            on_page=pages.append,
        )
        == 0
    )
    assert calls == ["run"]
    assert [page.reason for page in pages] == ["entry_client_unavailable"]


@pytest.mark.parametrize("identity", [None, ""])
def test_missing_sdk_generated_identity_never_merges_three_independent_entry_charges(
    monkeypatch, identity
):
    primary, entry, pacer = clients()
    dispatches = []
    monkeypatch.setattr(entry._session, "send", lambda *a, **kw: dispatches.append(a))
    wrapper = PacedTradingClient(cast(TradingClientLike, primary), pacer, entry_client=entry)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        request = buy(symbol).model_copy(update={"client_order_id": identity})
        with pytest.raises(PacerRefused):
            wrapper.submit_order(request, kind=ActionKind.SUBMIT)
    assert dispatches == [] and wrapper._entry_charges == {} and pacer.wire_requests == 0


def test_json_preparation_failure_has_no_wire_or_charge_but_uncertain_dispatch_keeps_exposure(
    monkeypatch,
):
    primary, entry, pacer = clients()
    dispatches = []

    def send(request, **options):
        dispatches.append(request)
        raise Timeout("response lost after dispatch")

    monkeypatch.setattr(entry._session, "send", send)
    wrapper = PacedTradingClient(cast(TradingClientLike, primary), pacer, entry_client=entry)
    invalid = buy(identity="B001r-invalid-json").model_copy(update={"limit_price": Decimal("NaN")})
    with pytest.raises(InvalidJSONError):
        wrapper.submit_order(invalid, kind=ActionKind.SUBMIT)
    assert dispatches == [] and wrapper._entry_charges == {} and pacer.wire_requests == 0
    with pytest.raises(Timeout):
        wrapper.submit_order(buy(identity="B001r-uncertain"), kind=ActionKind.SUBMIT)
    assert len(dispatches) == 1 and pacer.wire_requests == 1
    assert wrapper._unsettled == {"B001r-uncertain"}

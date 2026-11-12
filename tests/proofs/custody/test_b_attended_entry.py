"""LEAD-1452 T1: native B Strategy against the existing offline node/venue fixtures."""

from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nautilus_trader.model import (
    AggressorSide,
    InstrumentId,
    Price,
    Quantity,
    QuoteTick,
    TradeId,
    TradeTick,
)

from sextant.paper import node_order

from .test_alpaca_exits import _api
from .test_alpaca_node_claims import _filled, _holding
from .test_alpaca_node_owner import _owner
from .test_b_order_node import TIMING, AccountVenue, Stand, argv, writes
from .test_b_order_node import expected as expected

IID = InstrumentId.from_str("ZA.ALPACA")


@pytest.fixture(autouse=True)
def owner_thread_gc():
    # Native PyO3 objects must be collected on the node's owner thread, before SDK workers.
    gc.collect()
    yield
    gc.collect()


def opted(tmp_path):
    return replace(
        node_order.parse([*argv(tmp_path), "--attended-test-entry", str(IID)]), duration_s=1
    )


def test_opt_in_takes_exactly_one_instrument_and_absence_is_off(tmp_path):
    assert node_order.parse(argv(tmp_path)).attended is None
    intent = opted(tmp_path).attended
    assert intent is not None and intent.instrument_id == IID
    with pytest.raises(SystemExit):
        node_order.parse([*argv(tmp_path), "--attended-test-entry"])


@pytest.mark.parametrize("name", ["OUTSIDE.ALPACA", "bad instrument"])
def test_unclaimed_or_invalid_instrument_refuses_before_a_submit(tmp_path, name):
    try:
        args = node_order.parse([*argv(tmp_path), "--attended-test-entry", name])
    except node_order.OrderModeRefused as refusal:
        assert refusal.reason == "attended_test_entry_instrument"
        return
    stand = Stand(AccountVenue())
    receipt, code = node_order.run(
        args,
        consumer=None,
        leases=node_order.take_leases,
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING,
    )
    body = json.loads(receipt.read_text())
    assert (code, body["reason"]) == (2, "attended_test_entry_instrument")
    assert stand.connected == 0 and not writes(stand.venue)


@pytest.mark.parametrize(
    "change",
    [{"value": None}, {"value": 1.5}, {"source": ""}, {"calibration_state": "UNAVAILABLE"}],
)
def test_no_quantity_authority_refuses(tmp_path, change):
    parts = argv(tmp_path)
    path = tmp_path / "node.json"
    rows = json.loads((node_order.Path(parts[parts.index("--node-parameters") + 1])).read_text())
    found = False
    for row in rows:
        if row["name"] == "attended_test_entry_qty":
            row.update(change)
            found = True
    assert found, "the attended quantity needs a consumed registry row"
    path.write_text(json.dumps(rows))
    parts[parts.index("--node-parameters") + 1] = str(path)
    with pytest.raises(node_order.OrderModeRefused, match="attended_test_entry_parameters"):
        node_order.parse([*parts, "--attended-test-entry", str(IID)])


class OutcomeVenue(AccountVenue):
    def __init__(self, outcome):
        super().__init__()
        self.outcome = outcome
        self.canceled: Callable[[dict], object] = lambda _row: None

    def delete(self, path, body):
        result = super().delete(path, body)
        self.canceled(deepcopy(self.rows[path.rsplit("/", 1)[1]]))
        return result

    def post(self, path, body):
        if body["client_order_id"].startswith("O-") and self.outcome in {"rejected", "unknown"}:
            self.requests.append(("POST", path, deepcopy(body)))
            if self.outcome == "unknown":
                raise TimeoutError("fixture: submit outcome unknown")
            raise _api(422)
        return super().post(path, body)


def exercise(tmp_path, monkeypatch, outcome="unfilled", enabled=True):
    """Use the actual build/run and gated Strategy; inject only venue events and quotes."""
    venue = OutcomeVenue(outcome)
    owner = _owner(venue, "owner-resting", symbol="ZB")
    original = deepcopy(owner)
    stand = Stand(venue)
    args = opted(tmp_path) if enabled else replace(node_order.parse(argv(tmp_path)), duration_s=1)
    if outcome == "partial":
        assert args.attended is not None
        args = replace(args, attended=replace(args.attended, quantity=Quantity.from_int(2)))
    seen, errors = {}, []
    create_data = stand.data.create

    def keep_data(**kwargs):
        data = create_data(**kwargs)
        seen["data"] = data
        return data

    stand.data.create = staticmethod(keep_data)
    verify = node_order.GatedBLifecycle._verify

    def quote(strategy, *, age=0, bid="0.1230", ask="0.1234"):
        now = strategy.clock.timestamp_ns()
        size = Quantity.from_int(100)
        return QuoteTick(
            IID, Price.from_str(bid), Price.from_str(ask), size, size, now - age, now - age
        )

    async def admitted(strategy):
        seen["strategy"] = strategy
        # Give the pre-admission hook every other prerequisite: only the gate may stop it.
        strategy.books[IID].instrument = strategy.cache.instrument(IID)
        for _ in range(3):
            strategy.on_quote(quote(strategy))
        assert len(writes(venue)) == 1  # the seeded owner POST only
        await verify(strategy)
        assert strategy.gate.open
        loop = asyncio.get_running_loop()

        async def canceled(row):
            await strategy.gate.clients[0]._on_trade_update(
                {
                    "stream": "trade_updates",
                    "data": {
                        "event": "canceled",
                        "order": row,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                }
            )

        # A DELETE ack is not terminal: the fake venue must deliver its stream cancel,
        # on the owner loop, exactly as a fill already does below.
        venue.canceled = lambda row: loop.call_soon_threadsafe(
            strategy.gate.clients[0].create_task, canceled(row), "attended-fixture-cancel"
        )

        async def events():
            try:
                for q in (quote(strategy, age=6_000_000_000), quote(strategy, bid="0.1300")):
                    strategy.on_quote(q)
                assert len(writes(venue)) == 1  # only the owner's seeded POST
                for _ in range(8):
                    strategy.on_quote(quote(strategy))
                    await asyncio.sleep(0.02)
                buys = [r for r in venue.rows.values() if r["side"] == "buy" and r is not owner]
                if outcome in {"partial", "filled"} and buys:
                    now = strategy.clock.timestamp_ns()
                    seen["data"]._handle_data(
                        TradeTick(
                            IID,
                            Price.from_str("0.1234"),
                            Quantity.from_int(100),
                            AggressorSide.BUY,
                            TradeId("fixture-print"),
                            now,
                            now,
                        )
                    )
                    await asyncio.sleep(0.02)
                    row = buys[0]
                    row.update(
                        status="partially_filled" if outcome == "partial" else "filled",
                        filled_qty="1",
                        filled_avg_price="0.1234",
                    )
                    stamp = datetime.now(UTC).isoformat()
                    await strategy.gate.clients[0]._on_trade_update(
                        {
                            "stream": "trade_updates",
                            "data": {
                                "event": "partial_fill" if outcome == "partial" else "fill",
                                "order": deepcopy(row),
                                "timestamp": stamp,
                                "execution_id": "fixture-fill",
                                "price": "0.1234",
                                "qty": "1",
                            },
                        }
                    )
                for _ in range(8):
                    strategy.on_quote(quote(strategy))
                    await asyncio.sleep(0.02)
                if outcome in {"partial", "filled"}:
                    assert strategy._held(strategy.books[IID]) == Decimal(1)
            except Exception as error:
                errors.append(error)

        strategy.gate.clients[0].create_task(events(), "attended-fixture")

    monkeypatch.setattr(node_order.GatedBLifecycle, "_verify", admitted)
    receipt, code = node_order.run(
        args,
        consumer=None,
        leases=node_order.take_leases,
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING | {"delay_post_stop_secs": 1},
    )
    venue.canceled = lambda _row: None  # release the fixture's callback/client/venue cycle
    assert errors == []
    assert code == (2 if outcome == "unknown" else 0), json.loads(receipt.read_text())
    if outcome == "unknown":
        assert "unresolved client order IDs" in json.loads(receipt.read_text())["reason"]
    assert owner == original, "owner order changed"
    return venue, seen["strategy"]


@pytest.mark.parametrize("outcome", ["unfilled", "partial", "filled", "rejected", "unknown"])
def test_admission_one_attempt_and_own_id_only_stop_cleanup(tmp_path, monkeypatch, outcome):
    outside = outcome == "filled"
    monkeypatch.setattr(
        node_order,
        "session_at",
        lambda _now: node_order.Session.POST if outside else node_order.Session.RTH,
    )
    venue, strategy = exercise(tmp_path, monkeypatch, outcome)
    buys = [
        r[2]
        for r in venue.requests
        if r[0] == "POST"
        and r[2].get("side") == "buy"
        and r[2].get("client_order_id") != "owner-resting"
    ]
    assert len(buys) == 1
    (buy,) = buys
    assert buy["type"] == "limit" and buy["time_in_force"] == "day"
    assert buy["symbol"] == "ZA" and buy["extended_hours"] is outside
    assert buy["limit_price"] == "0.1234" and buy["order_class"] == "simple"
    assert Decimal(buy["qty"]) == (2 if outcome == "partial" else 1)
    assert strategy._house(strategy.attended_order_id)
    assert strategy.books[IID].entry_id is None, "the attended order entered ordinary repricing"
    mutations = [r for r in venue.requests if r[0] in {"DELETE", "PATCH"}]
    if outcome in {"unfilled", "partial"}:
        own = next(r for r in venue.rows.values() if r["client_order_id"] == buy["client_order_id"])
        assert mutations == [("DELETE", f"/orders/{own['id']}", {})]
    else:
        assert mutations == []
    if outcome in {"partial", "filled"}:
        trails = [r for r in venue.rows.values() if r["type"] == "trailing_stop"]
        assert len(trails) == 1 and trails[0]["qty"] == "1" and trails[0]["status"] == "new"


def test_flag_absent_retains_the_protection_only_behavior(tmp_path, monkeypatch):
    venue, _ = exercise(tmp_path, monkeypatch, enabled=False)
    assert len(writes(venue)) == 1  # only the seeded owner order


def test_restart_with_the_test_holding_refuses_not_flat(tmp_path):
    venue = AccountVenue()
    _filled(venue, "O-20261111-150000-001-BTEST-1", "1", "2026-11-11T15:00:00Z").update(symbol="ZA")
    _holding(venue, "ZA", "1")
    seeded = len(writes(venue))
    stand = Stand(venue)
    receipt, code = node_order.run(
        opted(tmp_path),
        consumer=None,
        leases=node_order.take_leases,
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING,
    )
    body = json.loads(receipt.read_text())
    assert (code, body["reason"]) == (2, "not_flat")
    assert len(writes(venue)) == seeded

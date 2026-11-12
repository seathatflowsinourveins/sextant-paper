"""N3: the order-mode entry's own node (``build``/``run``, ``node.run()`` here) on the E5 fake
venue. Stand-ins: the data client and the execution client's venue (test_alpaca_node_owner.py's
wiring). The command's own shape is the default here: no ranking consumer, with the rank gate row
off (N3 part 3); a fixture consumer is passed only where named. The writer lease and the stream
reservation sit under two roots, the old writer's and the producer's (N3 part 4)."""

from __future__ import annotations

import gc
import json
import threading
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as Fixture
from typing import Any

import pytest
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.live import NodeState
from nautilus_trader.live.clients import (
    DataClientFactory,
    ExecutionClientFactory,
    MarketDataClient,
)
from nautilus_trader.model import (
    AccountBalance,
    AccountId,
    AccountType,
    Currency,
    Equity,
    InstrumentId,
    Money,
    OmsType,
    Price,
    Quantity,
    Venue,
)
from test_ranking import publisher
from test_ranking_consumer import consumer

from sextant.alpaca.execution import AlpacaExecutionClient
from sextant.alpaca.factories import AlpacaExecutionClientFactory
from sextant.alpaca.wire import AccountGateway
from sextant.execution.custody import AccountScope, CustodyError, WriterLease
from sextant.execution.pacer import AccountPacer
from sextant.paper import node_order
from sextant.paper.b_protector_main import EXPECTED_FULL_ENV, EXPECTED_SUFFIX_ENV
from sextant.paper.decision_record import DecisionLog
from sextant.ranking.registry import RANK_GATE_ENABLED
from sextant.tradingview.producer import SCOPE as MARKET
from sextant.tradingview.producer import reserve_stream

from .alpaca_venue import Venue as FakeVenue
from .conftest import controls
from .test_alpaca_node_claims import _filled, _holding
from .test_alpaca_node_owner import _owner

ROOT = Path(__file__).resolve().parents[3]
TIMING = dict(timeout_connection_secs=10, timeout_reconciliation_secs=10)
TIMING |= dict(timeout_portfolio_secs=5, timeout_disconnection_secs=2, delay_post_stop_secs=0)
SCOPE = AccountScope("ALPACA", "FIXTUREACCOUNT")
NUMBER = "PAFIXTURE0001"  # a fixture account number


def names(count: int) -> list[str]:
    out = []
    for index in range(count):
        word, index = "", index + 1
        while index:
            index, rest = divmod(index - 1, 26)
            word = chr(65 + rest) + word
        out.append("Z" + word)
    return out


def artifact(path: Path, symbols: list[str]) -> Path:
    """The ranked universe file in the K2 artifact's shape (baseline_recipe.load_universe)."""
    path.write_text(json.dumps({"rows": [{"symbol": name} for name in symbols]}))
    return path


class AccountVenue(FakeVenue):
    """The E5 fake plus GET /account, the read the old writer checks (b_protector_main)."""

    def get(self, path, params):
        if path == "/account":
            self.requests.append(("GET", path, params))
            return {"account_number": NUMBER, "equity": "100", "buying_power": "100"}
        return super().get(path, params)


@pytest.fixture(autouse=True)
def expected(monkeypatch):
    monkeypatch.setenv(EXPECTED_FULL_ENV, NUMBER)
    monkeypatch.delenv(EXPECTED_SUFFIX_ENV, raising=False)


def argv(tmp_path: Path, *, count: int = 3, slot: str = "2", **paths: Path) -> list[str]:
    account = tmp_path / "account.json"
    namespace = paths.pop("namespace", "BTEST")
    fields = dict(lane="alpaca-paper-b", account_id="FIXTUREACCOUNT", client_id="ALPACA")
    account.write_text(json.dumps(fields | {"namespace": str(namespace), "sessions": ["rth"]}))
    lanes = ROOT / "lanes"
    values = {
        "--account-config": account,
        "--universe-artifact": artifact(tmp_path / "universe.json", names(count)),
        "--b-parameters": paths.get("b", lanes / "paper/b-parameters.json"),
        "--entry-parameters": lanes / "paper/b-entry-parameters.json",
        "--exit-parameters": lanes / "paper/b-gain-lock.params.json",
        "--reentry-parameters": lanes / "paper/b-reentry-parameters.json",
        "--node-parameters": lanes / "paper/shared-node.params.json",
        "--custody-parameters": lanes / "custody-parameters.json",
        "--receipt-dir": tmp_path / "receipts",
        "--duration-s": paths.get("duration", "2"),
        "--api-key-env": "FIXTURE_KEY_NAME",
        "--api-secret-env": "FIXTURE_SECRET_NAME",
        "--paper-slot": slot,
        "--writer-lease-root": paths.get("writer_root", tmp_path / "locks"),
        "--stream-lease-root": paths.get("stream_root", tmp_path / "leases"),
        "--existing-streams": "0",
        "--lease-source": "fixture: no stream exists",
    }
    return [str(part) for flag, value in values.items() for part in (flag, value)]


def equity(iid: InstrumentId) -> Equity:
    return Equity(
        instrument_id=iid,
        raw_symbol=iid.symbol,
        currency=Currency.from_str("USD"),
        price_precision=4,
        price_increment=Price.from_str("0.0001"),
        lot_size=Quantity.from_str("1"),
        ts_event=0,
        ts_init=0,
    )


class Stand:
    """The two stand-in factories and what they saw."""

    def __init__(self, venue: FakeVenue) -> None:
        self.venue, self.subscribed, self.connected = venue, 0, 0
        stand = self

        class Data(MarketDataClient):
            async def _connect(self):
                for iid in self.config.instrument_provider.load_ids or ():
                    self._handle_instrument(equity(InstrumentId.from_str(iid)))

            async def _disconnect(self):
                pass

            async def _subscribe_trades(self, command):
                stand.subscribed += len(command.instrument_ids)

            async def _subscribe_quotes(self, command):
                pass

            async def _subscribe_bars(self, command):
                pass

            async def _subscribe(self, command):
                pass

        class Exec(AlpacaExecutionClient):  # test_alpaca_node_owner.py's wiring
            async def _connect(self):
                stand.connected += 1
                self.gateway = AccountGateway(
                    venue, AccountPacer(controls(), self.clock.timestamp_ns), self._position_counts
                )
                self.commission_for_fill = lambda _data: Money.from_str("0 USD")
                balance = AccountBalance(
                    Money.from_str("100 USD"), Money.from_str("0 USD"), Money.from_str("100 USD")
                )
                self.generate_account_state([balance], [], True, self.clock.timestamp_ns())

        class DataFactory(DataClientFactory):
            @staticmethod
            def create(**kwargs):
                return Data(**kwargs, venue=Venue("ALPACA"))

        class ExecFactory(AlpacaExecutionClientFactory):
            @staticmethod
            def create(**kwargs):  # type: ignore[override]
                return Exec(
                    **kwargs,
                    venue=Venue("ALPACA"),
                    account_id=AccountId("ALPACA-BTEST"),
                    account_type=AccountType.MARGIN,
                    oms_type=OmsType.NETTING,
                )

        self.data, self.exec = DataFactory, ExecFactory


def start(
    tmp_path, venue=None, *, count=3, exec_factory=None, leases=None, with_consumer=False, **kw
):
    """Parse, then run the entry's own node on this thread; (receipt body, code, stand)."""
    # rc8 permits one LiveNode per thread; failed report tracebacks can form client cycles.
    gc.collect()
    # A publisher, when used, comes before the node: its MessageBus must not replace the node's.
    ranked = consumer(publisher()) if with_consumer else None
    stand = Stand(venue or AccountVenue())
    args = node_order.parse(argv(tmp_path, count=count, **kw))
    try:
        receipt, code = node_order.run(
            args,
            consumer=ranked,
            leases=leases or node_order.take_leases,
            data_factory=stand.data,
            exec_factory=exec_factory or stand.exec,
            timing=TIMING,
        )
    finally:
        gc.collect()  # The command drops its node; collect cycles before any assertion.
    return json.loads(receipt.read_text()), code, stand


def writes(venue: FakeVenue) -> list[Any]:
    return [r for r in venue.requests if r[0] != "GET"]


@pytest.mark.parametrize("slot", ["1", "3"])
def test_slots_one_and_three_are_refused_before_anything(tmp_path, slot) -> None:
    with pytest.raises(node_order.OrderModeRefused, match="paper_slot"):
        node_order.parse(argv(tmp_path, slot=slot))
    assert node_order.main(argv(tmp_path, slot=slot)) == 2
    assert not (tmp_path / "locks").exists() and not (tmp_path / "receipts").exists()
    assert not (tmp_path / "leases").exists()


def test_rows_protection_needs_only_the_native_row_and_the_rest_are_named_off(tmp_path) -> None:
    rows = node_order.parse(argv(tmp_path)).rows
    assert rows.native > 0 and rows.inflight_ms == 0
    assert rows.steps["protect"].startswith("on:")
    assert rows.width is None and "not streamed" in rows.steps["tighten"]  # 5-minute rows today
    for step in ("gain_lock", "reentry"):
        assert rows.steps[step].startswith("off:"), rows.steps[step]
    assert rows.entry is not None
    assert rows.entry["chase_cap_fraction"] == Decimal("0.0020")
    assert "rank gate row is off" in rows.steps["entry"]
    broken = [r for r in json.loads((ROOT / "lanes/paper/b-parameters.json").read_text())]
    broken = [r for r in broken if r["name"] != "native_trail_fraction"]
    (tmp_path / "b.json").write_text(json.dumps(broken))
    with pytest.raises(node_order.OrderModeRefused, match="registries"):
        node_order.parse(argv(tmp_path, b=tmp_path / "b.json"))


def test_with_the_rank_gate_off_the_command_needs_no_consumer(tmp_path) -> None:
    # P2 (b_lifecycle.py BLifecycleConfig): no consumer while the gate row is off. The held
    # lease stops the real command before any client could connect.
    old = WriterLease(SCOPE, tmp_path / "locks", "b-protector")
    old.acquire()
    try:
        assert node_order.main(argv(tmp_path)) == 2
    finally:
        old.close()
    body = json.loads((tmp_path / "receipts" / node_order.RECEIPT).read_text())
    assert (body["phase"], body["reason"]) == ("refused", "writer_lease")
    assert body["engine"]["cache_database"] is None and "not recovered" in body["restart"]


@pytest.mark.parametrize("value", [True, 0])  # only the bool False is off; 0 == False is not
def test_a_gate_row_that_is_on_still_needs_the_consumer(tmp_path, monkeypatch, value) -> None:
    monkeypatch.setattr(node_order, "RANK_GATE_ENABLED", replace(RANK_GATE_ENABLED, value=value))
    body, code, stand = start(tmp_path)
    assert (code, body["reason"]) == (2, "ranking_consumer") and stand.connected == 0
    assert not (tmp_path / "locks").exists() and not (tmp_path / "leases").exists()


def test_protection_only_starts_with_no_consumer_on_a_flat_account(tmp_path) -> None:
    venue = AccountVenue()
    body, code, stand = start(tmp_path, venue)
    assert (code, body["phase"], body["reason"]) == (0, "stopped", None), body
    assert stand.subscribed == 3 and not writes(venue)  # the Strategy's own on_start ran
    assert body["steps"]["protect"].startswith("on:")
    entry = body["steps"]["entry"]  # the Strategy's own missing list, with the consumer named
    assert entry.startswith("off: the Strategy refuses entries") and "ranking_consumer" in entry


def test_a_held_writer_lease_refuses_and_nothing_connects(tmp_path) -> None:
    old = WriterLease(SCOPE, tmp_path / "locks", "b-protector")  # the old writer's own scope
    old.acquire()
    try:
        body, code, stand = start(tmp_path)
    finally:
        old.close()
    assert (code, body["reason"]) == (2, "writer_lease") and stand.connected == 0


def test_an_execution_client_other_than_the_paper_one_is_refused(tmp_path) -> None:
    class Other(ExecutionClientFactory):
        @staticmethod
        def create(**kwargs):
            raise AssertionError("never built")

    body, code, stand = start(tmp_path, exec_factory=Other)
    assert (code, body["reason"]) == (2, "execution_client") and stand.connected == 0
    free(tmp_path / "locks", SCOPE)  # the refusal released the lease


def test_an_old_writer_prefix_that_overlaps_the_house_prefix_is_refused(tmp_path) -> None:
    body, code, _ = start(tmp_path, namespace="O")
    assert (code, body["reason"]) == (2, "house_prefix")


def test_a_claim_list_over_one_startup_flush_is_refused_before_connect(tmp_path) -> None:
    body, code, stand = start(tmp_path, count=node_order.QUEUE + 1)
    assert (code, body["reason"]) == (2, "claim_list") and stand.connected == 0
    assert "1025 names" in body["detail"]


def test_a_flat_account_starts_the_strategy_and_a_stop_releases_the_lease(tmp_path, capfd) -> None:
    venue = AccountVenue()
    body, code, stand = start(tmp_path, venue, count=node_order.QUEUE, with_consumer=True)
    logging_sync_to_disk()
    out = capfd.readouterr()
    assert (code, body["phase"], body["reason"]) == (0, "stopped", None), body
    assert body["gate"] == {
        "open_positions": 0,
        "open_house_orders": 0,
        "withheld_holdings": 0,
        "unresolved_replacements": 0,
    }
    assert stand.subscribed == node_order.QUEUE  # the Strategy's own on_start ran
    assert body["claims"]["count"] == node_order.QUEUE
    assert "ranking_consumer" not in body["steps"]["entry"]  # the fixture consumer was passed
    assert "command queue is full" not in out.out + out.err  # LEAD-0336: a full queue fails
    assert not writes(venue)
    free(tmp_path / "locks", SCOPE)
    free(tmp_path / "leases", MARKET)


def free(root: Path, scope: AccountScope) -> None:
    lease = WriterLease(scope, root, "after")
    lease.acquire()  # raises CustodyError while any holder remains
    lease.close()


@pytest.mark.parametrize("case", ["holding", "house_order", "outside"])
def test_a_non_flat_account_stops_before_any_command(tmp_path, case) -> None:
    venue = AccountVenue()
    if case == "holding":
        _filled(venue, "owner-filled", "9", "2026-11-10T15:00:00Z").update(symbol="ZA")
        _holding(venue, "ZA", "9")
    elif case == "house_order":
        _owner(venue, "BTEST-trail-ZA-1-1", symbol="ZA", side="sell")
    else:
        _holding(venue, "OTHER", "5")
    seeded = len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    reason = {"holding": "not_flat", "house_order": "house_order_open"}
    assert (code, body["phase"]) == (2, "refused")
    assert body["reason"] == reason.get(case, "holding_outside_claims")
    assert stand.subscribed == 0 and body["refused_sends"] == 0
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "nothing was sent"


# Terminal rows outside claims are withheld for the gate's lineage check; LEAD-1618
# refuses an unloaded working row before the gate, without changing its refusal list.
# Closed set: nautilus_trader d53849b crates/model/src/enums.rs:1489-1500.


# "replaced" ends only its own row: it starts only through a resolved chain (below); a bare
# replaced row with no successor link refuses (test_a_replacement_cycle_or_missing_link_refuses).
@pytest.mark.parametrize("status", ["canceled", "filled", "expired", "rejected"])
def test_a_terminal_old_writer_order_outside_the_claims_does_not_refuse(tmp_path, status) -> None:
    venue = AccountVenue()
    _owner(venue, "BTEST-trail-OUTX-1-1", symbol="OUTX", side="sell", status=status)
    seeded = len(venue.requests)
    body, code, _ = start(tmp_path, venue)
    assert (code, body["phase"], body["reason"]) == (0, "stopped", None), body
    assert body["gate"]["open_house_orders"] == 0
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "nothing was sent"


@pytest.mark.parametrize("status", ["new", "pending_cancel", "done_for_day"])
def test_a_working_old_writer_order_outside_the_claims_still_refuses(tmp_path, status) -> None:
    venue = AccountVenue()
    _owner(venue, "BTEST-trail-OUTX-1-1", symbol="OUTX", side="sell", status=status)
    seeded = len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    assert (code, body["phase"]) == (2, "failed")
    assert body["reconciliation"]["orders"]["reason"] == (
        "report instrument is unavailable for non-terminal order"
    )
    assert body["gate"] == {} and stand.subscribed == 0
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "nothing was sent"


# A replaced row ends only its own order: Alpaca links the successor through replaced_by and
# replaces (alpaca-py cc4cb3b7 alpaca/trading/models.py:223-224), and a replace may give the
# successor a new client id (requests.py ReplaceOrderRequest.client_order_id, :300). The gate's
# review W2 (cutterm-b73c48d-codex-r1): a working successor outside the old prefix went uncounted.


def replace_on_venue(venue: FakeVenue, row: dict[str, Any], client_order_id: str) -> dict:
    """The fake venue's own PATCH and confirm: the old row is replaced, the successor is new."""
    venue.patch(f"/orders/{row['id']}", {"client_order_id": client_order_id, "trail": "1.5"})
    return venue.rows[venue.confirm()["id"]]


def old_writer_chain(venue: FakeVenue, ends: int) -> list[dict[str, Any]]:
    rows = [_owner(venue, "BTEST-trail-OUTX-1-1", symbol="OUTX", side="sell", status="new")]
    for n in range(ends):
        rows.append(replace_on_venue(venue, rows[-1], f"replacement-outside-{n}"))
    return rows


def test_gate_w2_a_working_successor_outside_the_old_prefix_refuses() -> None:
    parent = dict(id="p", client_order_id="BTEST-old-OUTX", status="replaced", replaced_by="c")
    child = dict(id="c", client_order_id="replacement-outside-old-prefix", status="new")
    client = Fixture(withheld=[(parent, "replaced"), (child, "unavailable")], withheld_positions=[])
    gate = node_order.Gate("BTEST-", clients=[client])
    reasons = gate.check(Fixture(positions_open=lambda: [], orders_open=lambda: []))
    assert "house_order_open" in reasons and gate.counts["open_house_orders"] == 1


@pytest.mark.parametrize("ends", [1, 2])
def test_a_replaced_old_writer_order_with_a_working_successor_refuses(tmp_path, ends) -> None:
    venue = AccountVenue()
    old_writer_chain(venue, ends)
    seeded = len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    assert (code, body["phase"]) == (2, "failed")
    assert body["reconciliation"]["orders"]["reason"] == (
        "report instrument is unavailable for non-terminal order"
    )
    assert stand.subscribed == 0
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "nothing was sent"


def test_a_chain_of_two_replacements_ending_terminal_starts(tmp_path) -> None:
    venue = AccountVenue()
    old_writer_chain(venue, 2)[-1]["status"] = "canceled"
    seeded = len(venue.requests)
    body, code, _ = start(tmp_path, venue)
    assert (code, body["phase"], body["reason"]) == (0, "stopped", None), body
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "nothing was sent"


def test_a_successor_absent_from_the_listing_refuses(tmp_path) -> None:
    venue = AccountVenue()
    del venue.rows[old_writer_chain(venue, 1)[-1]["id"]]
    body, code, stand = start(tmp_path, venue)
    assert (code, body["phase"], body["reason"]) == (2, "refused", "replacement_unresolved")
    assert body["gate"]["unresolved_replacements"] == 1 and stand.subscribed == 0


def test_a_replacement_cycle_or_missing_link_refuses() -> None:
    a = dict(id="a", client_order_id="BTEST-a", status="replaced", replaced_by="b")
    b = dict(id="b", client_order_id="other-b", status="replaced", replaced_by="a")
    lost = dict(id="x", client_order_id="BTEST-x", status="replaced")
    rows = [(a, "replaced"), (b, "replaced"), (lost, "replaced")]
    gate = node_order.Gate("BTEST-", clients=[Fixture(withheld=rows, withheld_positions=[])])
    reasons = gate.check(Fixture(positions_open=lambda: [], orders_open=lambda: []))
    assert reasons == ["replacement_unresolved"] and gate.counts["unresolved_replacements"] == 2


# The delta gate's P1 (cutterm2-56c8ba4-codex-r1): a populated replaced_by is followed on any
# status, so a terminal-looking row in the middle of a chain never ends it early.


def gate_over(*chain: tuple[str, str, str | None]) -> node_order.Gate:
    """A Gate over withheld rows (id, status, replaced_by); the first has the old prefix."""
    rows = []
    for n, (ident, status, link) in enumerate(chain):
        prefix = "BTEST-" if n == 0 else "other-"
        row = dict(id=ident, client_order_id=f"{prefix}{ident}", status=status)
        if link is not None:
            row["replaced_by"] = link
        rows.append((row, "fixture"))
    gate = node_order.Gate("BTEST-", clients=[Fixture(withheld=rows, withheld_positions=[])])
    gate.reasons = gate.check(Fixture(positions_open=lambda: [], orders_open=lambda: []))
    return gate


@pytest.mark.parametrize(
    "chain",
    [
        # the gate's reproducer: replaced -> canceled (linked) -> new
        (("p", "replaced", "m"), ("m", "canceled", "c"), ("c", "new", None)),
        # (a) a working middle with a link, ending canceled
        (("p", "replaced", "m"), ("m", "new", "c"), ("c", "canceled", None)),
        (("p", "replaced", "m"), ("m", "accepted", "c"), ("c", "canceled", None)),
        # (b) a filled middle with a link, ending working
        (("p", "replaced", "m"), ("m", "filled", "c"), ("c", "new", None)),
        # (c) four rows, the only working one third
        (
            ("p", "replaced", "a"),
            ("a", "canceled", "b"),
            ("b", "new", "d"),
            ("d", "canceled", None),
        ),
        # (d) the old-prefix row itself terminal, linked to a working row
        (("p", "canceled", "c"), ("c", "new", None)),
    ],
    ids=["gate-reproducer", "a-new-middle", "a-accepted-middle", "b", "c", "d"],
)
def test_a_working_row_anywhere_on_a_linked_chain_counts_once(chain) -> None:
    gate = gate_over(*chain)
    assert gate.reasons == ["house_order_open"], gate.counts
    assert gate.counts["open_house_orders"] == 1 and gate.counts["unresolved_replacements"] == 0


def test_a_linked_terminal_chain_with_no_working_row_starts_and_a_dangling_link_refuses() -> None:
    ended = gate_over(("p", "replaced", "m"), ("m", "canceled", "c"), ("c", "filled", None))
    assert ended.reasons == [] and ended.counts["open_house_orders"] == 0
    dangling = gate_over(("p", "replaced", "m"), ("m", "canceled", "gone"))
    assert dangling.reasons == ["replacement_unresolved"]


def test_a_canceled_middle_still_linked_to_a_working_successor_refuses_on_the_venue(tmp_path):
    """The gate's FakeVenue variant: two PATCH and confirm replacements, then the middle row's
    status reads canceled while it still links to the working tail."""
    venue = AccountVenue()
    chain = old_writer_chain(venue, 2)
    chain[1]["status"] = "canceled"  # replaced_by stays populated
    seeded = len(venue.requests)
    body, code, stand = start(tmp_path, venue)
    assert (code, body["phase"]) == (2, "failed")
    assert body["reconciliation"]["orders"]["reason"] == (
        "report instrument is unavailable for non-terminal order"
    )
    assert body["gate"] == {} and stand.subscribed == 0
    assert all(r[0] == "GET" for r in venue.requests[seeded:]), "nothing was sent"


def test_the_unchunked_flush_at_the_directory_size_logs_a_full_queue_and_fails(
    tmp_path, capfd, owner_thread_gc
):
    """The engine fact at LEAD-0336's 5,200, built past the refusal: the flush overflows the
    execution client's queue and shutdown_on_error aborts before the Strategy runs."""
    pub = publisher()
    stand = Stand(AccountVenue())
    args = node_order.parse(argv(tmp_path, count=3))
    ids = tuple(InstrumentId.from_str(f"{name}.ALPACA") for name in names(5200))
    args = replace(args, universe=ids)
    node, strategy = node_order.build(
        args,
        consumer=consumer(pub),
        data_factory=stand.data,
        exec_factory=stand.exec,
        timing=TIMING,
    )
    handle = node.handle()
    timer = threading.Timer(20, handle.stop)  # bounds the run if the start succeeds
    try:
        timer.start()
        node.run()
        state, gate_checked = handle.state, strategy.gate.checked
    finally:
        timer.cancel()
        timer.join()
        try:
            node.dispose()
        finally:
            del node, strategy
            owner_thread_gc()
    # NT d53849b crates/common/src/python/logging.rs:303-315: acknowledged flush.
    logging_sync_to_disk()
    out = capfd.readouterr()
    text = out.out + out.err
    full = sum(
        "[ERROR]" in line and "instrument update failed" in line and "queue is full" in line
        for line in text.splitlines()
    )
    assert full == 5200 - node_order.QUEUE, full  # LEAD-0336 measured 4,176
    assert "Shutdown signal received during startup" in text
    assert not gate_checked and stand.subscribed == 0
    assert state == NodeState.STOPPED


def test_both_leases_are_held_for_the_run_and_a_second_holder_is_refused(tmp_path) -> None:
    args = node_order.parse(argv(tmp_path))
    with ExitStack() as held:
        node_order.take_leases(held, args)
        for root, scope in ((tmp_path / "locks", SCOPE), (tmp_path / "leases", MARKET)):
            with pytest.raises(CustodyError):
                free(root, scope)
        with ExitStack() as second, pytest.raises(node_order.OrderModeRefused, match="writer"):
            node_order.take_leases(second, args)
    free(tmp_path / "locks", SCOPE)
    free(tmp_path / "leases", MARKET)


def test_an_account_other_than_the_expected_one_stops_before_any_command(
    tmp_path, monkeypatch, capfd
) -> None:
    monkeypatch.setenv(EXPECTED_FULL_ENV, "PAOTHER0002")
    venue = AccountVenue()
    body, code, stand = start(tmp_path, venue)
    logging_sync_to_disk()
    out = capfd.readouterr()
    assert (code, body["phase"], body["reason"]) == (2, "refused", "account_mismatch"), body
    assert stand.subscribed == 0 and not writes(venue)
    assert ("GET", "/account", {}) in venue.requests  # the account it reached was read
    for text in (out.out + out.err, json.dumps(body)):  # neither number is printed or kept
        assert NUMBER not in text and "PAOTHER0002" not in text


def test_no_expected_account_refuses_before_any_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(EXPECTED_FULL_ENV)
    body, code, stand = start(tmp_path)
    assert (code, body["reason"]) == (2, "account_mismatch") and stand.connected == 0
    assert not (tmp_path / "locks").exists() and not (tmp_path / "leases").exists()


def test_a_holding_outside_the_claims_while_running_is_paged_once_per_name(tmp_path, capfd) -> None:
    rows = json.loads((ROOT / "lanes/paper/b-parameters.json").read_text())
    for row in rows:  # a one-second poll for the proof; the committed row is the authority
        row["value"] = "1" if row["name"] == "reconcile_interval_s" else row["value"]
    (tmp_path / "b.json").write_text(json.dumps(rows))
    venue = AccountVenue()
    later = threading.Timer(0.5, lambda: [_holding(venue, s, "5") for s in ("OUTA", "OUTB")])
    try:
        later.start()
        body, code, _ = start(tmp_path, venue, b=tmp_path / "b.json", duration="4")
    finally:
        later.cancel()
        later.join()
    logging_sync_to_disk()
    err = capfd.readouterr().err.splitlines()
    lines = [line for line in err if "holding_outside_claims_running" in line]
    assert (code, body["phase"]) == (0, "stopped"), body
    assert body["outside_holdings_paged"] == 2
    assert sorted(line.rsplit(" ", 1)[1] for line in lines) == ["OUTA", "OUTB"]  # once per name
    assert not writes(venue)


# N3 part 4 (a): each lease excludes against its own current holder, under its own root.


def hold(holder: str, tmp_path: Path, held: ExitStack) -> None:
    """The old writer's lease under its own locks directory (b_protector_main.py:370-372), or the
    prestage producer's reservation under its own leases directory (prestage.py:799-803)."""
    if holder == "writer":
        old = WriterLease(SCOPE, tmp_path / "locks", "b-protector")
        old.acquire()
        held.callback(old.close)
    else:
        source = "fixture: the producer opens no stream"
        held.enter_context(
            reserve_stream(tmp_path / "leases", paper_slot=2, existing_streams=0, source=source)
        )


@pytest.mark.parametrize(
    ("holder", "reason"), [("writer", "writer_lease"), ("stream", "market_data_lease")]
)
def test_each_lease_refuses_against_its_own_holder_under_its_own_root(
    tmp_path, holder, reason
) -> None:
    with ExitStack() as held:
        hold(holder, tmp_path, held)
        body, code, stand = start(tmp_path)
    assert (code, body["phase"], body["reason"]) == (2, "refused", reason)
    assert stand.connected == 0
    assert body["lease"]["writer_root"] == str(tmp_path / "locks")
    assert body["lease"]["stream_root"] == str(tmp_path / "leases")
    free(tmp_path / "locks", SCOPE)  # the refusal released whatever the node had taken
    free(tmp_path / "leases", MARKET)


def test_one_shared_root_loses_the_stream_refusal(tmp_path) -> None:
    """Why there are two roots: with the producer's reservation under its own directory, a
    node given the old writer's directory for both leases is not refused and starts."""
    venue = AccountVenue()
    with ExitStack() as held:
        hold("stream", tmp_path, held)
        body, code, _ = start(tmp_path, venue, stream_root=tmp_path / "locks")
    assert (code, body["phase"]) == (0, "stopped"), body  # market_data_lease refusal is lost


@pytest.mark.parametrize("flag", ["--writer-lease-root", "--stream-lease-root"])
def test_each_lease_root_is_required_with_no_fallback(tmp_path, flag) -> None:
    parts = argv(tmp_path)
    index = parts.index(flag)
    with pytest.raises(SystemExit):
        node_order.parse(parts[:index] + parts[index + 2 :])
    with pytest.raises(SystemExit):
        node_order.parse([*parts, "--lease-root", str(tmp_path / "locks")])  # the old flag


# N3 part 4 (b): any other start failure leaves a receipt and releases both leases.


def test_an_exception_from_build_writes_a_failed_receipt_and_releases_the_leases(
    tmp_path, monkeypatch
) -> None:
    def broken(**_kwargs):
        raise ValueError("fixture: the Strategy config refused")

    monkeypatch.setattr(node_order, "BLifecycleConfig", broken)
    body, code, stand = start(tmp_path)
    assert (code, body["phase"], body["reason"]) == (2, "failed", "ValueError")
    assert "fixture: the Strategy config refused" in body["detail"] and stand.connected == 0
    free(tmp_path / "locks", SCOPE)
    free(tmp_path / "leases", MARKET)


# N3 part 4 (c): the Strategy's records and pages are kept in the run's receipt directory.


def journal_rows(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "receipts" / node_order.JOURNAL
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_strategy_records_of_a_run_reach_the_journal_file(tmp_path) -> None:
    body, code, _ = start(tmp_path)
    assert (code, body["phase"]) == (0, "stopped"), body
    refused = [row for row in journal_rows(tmp_path) if row["kind"] == "entry_refused"]
    assert len(refused) == 1 and "ranking_consumer" in refused[0]["missing"]
    assert body["journal"] == {"file": node_order.JOURNAL, "errors": 0}


def test_a_page_raised_by_the_strategy_reaches_the_journal_file(tmp_path) -> None:
    args = node_order.parse(argv(tmp_path))
    args = replace(args, universe=(InstrumentId.from_str("ZA.ALPACA"),))
    log = DecisionLog(tmp_path / "receipts" / node_order.JOURNAL)
    config = node_order.strategy_config(args, consumer=None, journal=node_order.journal_into(log))
    strategy = node_order.GatedBLifecycle(config)
    book = next(iter(strategy.books.values()))
    strategy._page(book, 1, "width_unavailable")
    log.close()
    pages = [row for row in journal_rows(tmp_path) if row["kind"] == "page"]
    assert pages == [
        {"kind": "page", "ts": 1, "instrument": "ZA.ALPACA", "reason": "width_unavailable"}
    ]
    assert strategy.journal_errors == 0

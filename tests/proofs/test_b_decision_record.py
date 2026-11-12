"""Proofs for B's durable exit-decision record.

The record is the run's receipt for "which rule fired": one append-only JSON line per exit
decision, written off the hot path, naming the trigger, the quote the trigger came from, the
completing observations, the order it submitted and the quantity bookkeeping. The last test
replays the 2026-11-09 after-hours high from the recorded tape shape.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alpaca.trading.requests import LimitOrderRequest

from sextant.paper import decision_record as record_module
from sextant.paper.decision_record import (
    Book,
    DecisionLog,
    ExitRecord,
    HighWater,
    Observation,
    TapeSpan,
    TriggerSource,
    number,
    stamp,
)

# The 2026-11-09 after-hours high that B-EXIT-1959 missed: bid 38.65 at 21:59:15.543085Z.
HIGH_NS = 1_794_261_555_543_085_000
AT_NS = HIGH_NS + 1_000_000_000


def adopt_record(**overrides: Any) -> ExitRecord:
    fields: dict[str, Any] = {
        "action": "adopt",
        "at_ns": AT_NS,
        "symbol": "ASTS",
        "trigger": Decimal("37.68"),
        "source": TriggerSource(
            kind="tape_high",
            level=Decimal("37.68"),
            at_ns=HIGH_NS,
            bid=Decimal("38.65"),
            ask=Decimal("38.70"),
            tape=TapeSpan(
                start_ns=HIGH_NS - 60_000_000_000,
                end_ns=HIGH_NS,
                feed="sip",
                pages=2,
                status="ok",
            ),
        ),
        "high": HighWater(value=Decimal("38.65"), kind="bid", at_ns=HIGH_NS),
        "observations": (
            Observation(
                at_ns=AT_NS,
                price=Decimal("37.19"),
                kind="trade",
                count=3,
                noise=Decimal("0.01"),
                spread=Decimal("0.01"),
            ),
        ),
        "order": {
            "client_order_id": "B001-outside-ASTS-1794261556-1",
            "side": "sell",
            "quantity": Decimal("55"),
            "limit_price": Decimal("37.10"),
            "extended_hours": True,
        },
        "book": Book(
            position=Decimal("55"),
            reserved=Decimal("0"),
            available=Decimal("55"),
            quantity=Decimal("55"),
        ),
        "noise": Decimal("0.01"),
        "noise_detail": {"one_tick": "0.01", "spread": "0.01", "floor": "0.01"},
        "outcome": "submitted",
    }
    fields.update(overrides)
    return ExitRecord(**fields)


def leaves(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaves(item)
    else:
        yield value


def test_stamp_is_iso_utc_with_nanoseconds() -> None:
    assert stamp(HIGH_NS) == "2026-11-09T21:59:15.543085000Z"
    assert stamp(0) == "1970-01-01T00:00:00.000000000Z"


def test_number_is_an_exact_decimal_string_and_refuses_a_float() -> None:
    assert number(Decimal("37.68375")) == "37.68375"
    assert number(Decimal("55")) == "55"
    assert number(None) is None
    with pytest.raises(TypeError):
        number(55.0)  # type: ignore[arg-type]


def test_record_is_json_with_decimal_strings_and_no_float() -> None:
    body = adopt_record().as_record()

    assert body["record"] == 1
    assert body["action"] == "adopt"
    assert body["at"] == "2026-11-09T21:59:16.543085000Z"
    assert body["trigger"] == "37.68"
    assert body["trigger_source"]["kind"] == "tape_high"
    assert body["trigger_source"]["level"] == "37.68"
    assert body["trigger_source"]["quote"] == {
        "at": "2026-11-09T21:59:15.543085000Z",
        "bid": "38.65",
        "ask": "38.70",
    }
    assert body["trigger_source"]["tape"]["status"] == "ok"
    assert body["high_water"] == {
        "value": "38.65",
        "kind": "bid",
        "at": "2026-11-09T21:59:15.543085000Z",
    }
    assert body["observations"] == [
        {
            "at": "2026-11-09T21:59:16.543085000Z",
            "price": "37.19",
            "kind": "trade",
            "count": 3,
            "noise": "0.01",
            "spread": "0.01",
        }
    ]
    assert body["order"]["quantity"] == "55"
    assert body["book"]["available"] == "55"
    assert body["outcome"] == "submitted"
    assert json.loads(json.dumps(body)) == body
    assert all(not isinstance(leaf, float) for leaf in leaves(body))


def test_record_refuses_a_float_anywhere_in_the_body() -> None:
    record = adopt_record(order={"client_order_id": "B001-outside-ASTS-1", "quantity": 55.0})
    with pytest.raises(TypeError, match="must not be a float"):
        record.as_record()


def test_log_appends_one_line_per_decision_and_fsyncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsyncs: list[int] = []
    monkeypatch.setattr(record_module, "full_fsync", lambda fd: fsyncs.append(fd))
    path = tmp_path / "state" / "b-decisions.jsonl"

    log = DecisionLog(path)
    try:
        log.append(adopt_record())
        log.append(adopt_record(action="cancel", trigger=None))
    finally:
        log.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["action"] for line in lines] == ["adopt", "cancel"]
    assert len(fsyncs) == 2
    assert all(fsyncs)


def test_an_unopenable_receipt_pages_and_keeps_the_exit(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    pages: list[tuple[str, str | None, str]] = []
    log = DecisionLog(
        blocker / "state" / "b-decisions.jsonl",
        on_page=lambda reason, symbol, detail: pages.append((reason, symbol, detail)),
    )
    deadline = time.monotonic() + 5
    while not log.stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert log.stopped
    log.append(adopt_record())
    log.append(adopt_record())
    log.close()

    assert [page[0] for page in pages] == ["decision_record_write_failed"] * 3
    assert pages[0][2] == "NotADirectoryError"
    assert [page[2] for page in pages[1:]] == ["writer stopped", "writer stopped"]
    assert all(page[1] is None for page in pages)


def test_log_writes_off_the_calling_thread(tmp_path: Path) -> None:
    seen: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def writer(line: str) -> None:
        seen.append(threading.get_ident())
        started.set()
        release.wait(2.0)

    log = DecisionLog(tmp_path / "decisions.jsonl", writer=writer)
    try:
        log.append(adopt_record())
        assert started.wait(1.0)
        assert seen and seen[0] != threading.get_ident()
    finally:
        release.set()
        log.close()


def test_log_pages_one_write_failure_and_never_raises(tmp_path: Path) -> None:
    pages: list[tuple[str, str | None, str]] = []

    def writer(line: str) -> None:
        raise OSError("disk full")

    log = DecisionLog(
        tmp_path / "decisions.jsonl",
        on_page=lambda reason, symbol, detail: pages.append((reason, symbol, detail)),
        writer=writer,
    )
    try:
        log.append(adopt_record())
    finally:
        log.close()

    assert pages == [("decision_record_write_failed", None, "OSError")]


def test_log_drops_on_a_full_queue_with_a_page(tmp_path: Path) -> None:
    pages: list[tuple[str, str | None, str]] = []
    started = threading.Event()
    release = threading.Event()

    def writer(line: str) -> None:
        started.set()
        release.wait(2.0)

    log = DecisionLog(
        tmp_path / "decisions.jsonl",
        on_page=lambda reason, symbol, detail: pages.append((reason, symbol, detail)),
        writer=writer,
        queue_size=1,
    )
    try:
        log.append(adopt_record())
        assert started.wait(1.0)
        log.append(adopt_record(action="cancel"))
        log.append(adopt_record(action="release"))
    finally:
        release.set()
        log.close()

    assert pages == [("decision_record_write_failed", None, "queue full")]


def _returns_within(call: Any, limit: float = 1.0) -> bool:
    done = threading.Event()

    def run() -> None:
        call()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done.wait(limit)


def test_a_blocked_page_callback_never_holds_the_exit(tmp_path: Path) -> None:
    gate = threading.Event()
    page_started = threading.Event()

    def blocked_page(reason: str, symbol: str | None, detail: str) -> None:
        page_started.set()
        gate.wait(10.0)

    writer_started = threading.Event()
    writer_gate = threading.Event()

    def blocked_writer(line: str) -> None:
        writer_started.set()
        writer_gate.wait(10.0)

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    log = DecisionLog(
        tmp_path / "slow.jsonl", on_page=blocked_page, writer=blocked_writer, queue_size=1
    )
    stopped = DecisionLog(blocker / "state" / "b-decisions.jsonl", on_page=blocked_page)
    try:
        log.append(adopt_record())
        assert writer_started.wait(2.0)
        log.append(adopt_record())
        assert _returns_within(lambda: log.append(adopt_record())), "a full queue held the caller"
        assert page_started.wait(2.0)

        deadline = time.monotonic() + 5
        while not stopped.stopped and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stopped.stopped
        assert _returns_within(lambda: stopped.append(adopt_record())), "a stopped writer held"
    finally:
        gate.set()
        writer_gate.set()
        log.close()
        stopped.close()


def test_a_pager_start_failure_never_reaches_the_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages: list[tuple[str, str | None, str]] = []
    writer_started = threading.Event()
    writer_gate = threading.Event()

    def blocked_writer(line: str) -> None:
        writer_started.set()
        writer_gate.wait(10.0)

    original = threading.Thread.start

    def failing_start(thread: threading.Thread) -> None:
        if thread.name == "b-decision-record-pages":
            raise RuntimeError("can't start new thread")
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", failing_start)
    log = DecisionLog(
        tmp_path / "decisions.jsonl",
        on_page=lambda reason, symbol, detail: pages.append((reason, symbol, detail)),
        writer=blocked_writer,
        queue_size=1,
    )
    try:
        log.append(adopt_record())
        assert writer_started.wait(2.0)
        log.append(adopt_record())
        assert _returns_within(lambda: log.append(adopt_record())), "paging held the caller"
        assert log.page_counts[1] == 1
    finally:
        writer_gate.set()
        log.close()
    assert pages == []


def test_append_never_starts_a_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer_started = threading.Event()
    writer_gate = threading.Event()

    def blocked_writer(line: str) -> None:
        writer_started.set()
        writer_gate.wait(10.0)

    log = DecisionLog(
        tmp_path / "decisions.jsonl",
        on_page=lambda reason, symbol, detail: None,
        writer=blocked_writer,
        queue_size=1,
    )
    monkeypatch.setattr(
        threading.Thread, "start", lambda thread: (_ for _ in ()).throw(RuntimeError("started"))
    )
    try:
        log.append(adopt_record())
        assert writer_started.wait(2.0)
        log.append(adopt_record())
        log.append(adopt_record())  # a start here would raise into this call
    finally:
        writer_gate.set()
        log.close()


def test_a_blocked_sink_bounds_the_page_backlog_and_counts_drops(tmp_path: Path) -> None:
    gate = threading.Event()
    page_started = threading.Event()
    seen: list[tuple[str, str | None, str]] = []

    def blocked_page(reason: str, symbol: str | None, detail: str) -> None:
        page_started.set()
        gate.wait(10.0)
        seen.append((reason, symbol, detail))

    writer_started = threading.Event()
    writer_gate = threading.Event()

    def blocked_writer(line: str) -> None:
        writer_started.set()
        writer_gate.wait(10.0)

    log = DecisionLog(
        tmp_path / "decisions.jsonl",
        on_page=blocked_page,
        writer=blocked_writer,
        queue_size=1,
    )
    try:
        log.append(adopt_record())
        assert writer_started.wait(2.0)
        log.append(adopt_record())
        assert _returns_within(lambda: log.append(adopt_record())), "a full queue held the caller"
        assert page_started.wait(2.0)

        def flood() -> None:
            record = adopt_record()
            for _ in range(10_000):
                log.append(record)

        assert _returns_within(flood, 10.0), "a page backlog grew without bound"
        backlog, drops = log.page_counts
        assert backlog == record_module.PAGE_QUEUE_SIZE
        assert drops == 10_000 - record_module.PAGE_QUEUE_SIZE
    finally:
        gate.set()
        writer_gate.set()
        log.close()

    reports = [page for page in seen if "page(s) dropped" in page[2]]
    assert len(reports) == 1
    assert reports[0][2] == f"{10_000 - record_module.PAGE_QUEUE_SIZE} page(s) dropped"


def test_a_refused_writer_start_closes_the_pager_without_joining_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = threading.Thread.start

    def refuse_writer(thread: threading.Thread) -> None:
        if thread.name == "b-decision-record":
            raise RuntimeError("can't start new thread")
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", refuse_writer)
    log = DecisionLog(tmp_path / "decisions.jsonl", on_page=lambda *_: None, queue_size=1)
    pager = log._pager
    assert log.stopped
    assert pager is not None
    log.close()  # a never-started writer must not be joined, and this must not raise
    log.close()  # a repeated close is harmless
    assert not pager.is_alive()


def test_a_full_page_backlog_still_stops_the_pager(tmp_path: Path) -> None:
    gate = threading.Event()
    page_started = threading.Event()

    def blocked_page(reason: str, symbol: str | None, detail: str) -> None:
        page_started.set()
        gate.wait(10.0)

    writer_started = threading.Event()
    writer_gate = threading.Event()

    def blocked_writer(line: str) -> None:
        writer_started.set()
        writer_gate.wait(10.0)

    log = DecisionLog(
        tmp_path / "decisions.jsonl",
        on_page=blocked_page,
        writer=blocked_writer,
        queue_size=1,
    )
    pager = log._pager
    assert pager is not None
    try:
        log.append(adopt_record())
        assert writer_started.wait(2.0)
        deadline = time.monotonic() + 5
        while not page_started.is_set() and time.monotonic() < deadline:
            log.append(adopt_record())
        assert page_started.is_set()
        while log.page_counts[0] < record_module.PAGE_QUEUE_SIZE and time.monotonic() < deadline:
            log.append(adopt_record())
        assert log.page_counts[0] == record_module.PAGE_QUEUE_SIZE

        writer_gate.set()
        log.close(timeout=0.02)  # the pager is blocked inside a page, the backlog is full
        gate.set()

        deadline = time.monotonic() + 5
        while pager.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not pager.is_alive()
        log.close()  # a repeated close is harmless
    finally:
        gate.set()
        writer_gate.set()

    assert not pager.is_alive()


def test_log_reopening_appends_and_never_rewrites(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    first = DecisionLog(path)
    first.append(adopt_record())
    first.close()
    second = DecisionLog(path)
    second.append(adopt_record(action="cancel"))
    second.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["action"] for line in lines] == ["adopt", "cancel"]
    assert all(line.startswith("{") and line.endswith("}") for line in lines)


# ---------------------------------------------------------------------------------------------
# The trail keeps the quote that raised its trigger and the run that completed the breach.
# ---------------------------------------------------------------------------------------------


from sextant.exits.policy import BreachRun, ExitParams  # noqa: E402
from sextant.paper.b_feed import QuoteObservation  # noqa: E402
from sextant.paper.outside_rth import (  # noqa: E402
    OutsideRthTrail,
    TriggerQuote,
    rebuild_trigger,
    rebuild_trigger_with_source,
)


def new_trail() -> OutsideRthTrail:
    return OutsideRthTrail(trail_fraction=Decimal("0.025"), params=ExitParams.starts())


def source_of(bid: str, ts_ns: int, level: str) -> TriggerQuote:
    return TriggerQuote(
        bid=Decimal(bid), ask=Decimal(bid) + Decimal("0.05"), ts_ns=ts_ns, level=Decimal(level)
    )


def test_live_ratchet_keeps_the_bid_that_raised_the_trigger() -> None:
    state = new_trail()

    assert state.observe_quote(
        bid=Decimal("38.65"), ask=Decimal("38.70"), last=None, ts_ns=HIGH_NS
    ) == Decimal("37.68")
    assert state.source == source_of("38.65", HIGH_NS, "37.68")

    # A lower bid leaves the ratchet, and the quote behind it, where they were.
    state.observe_quote(bid=Decimal("38.60"), ask=Decimal("38.70"), last=None, ts_ns=HIGH_NS + 1)
    assert state.trigger == Decimal("37.68")
    assert state.source == source_of("38.65", HIGH_NS, "37.68")

    # A higher bid moves both.
    state.observe_quote(bid=Decimal("38.80"), ask=Decimal("38.85"), last=None, ts_ns=HIGH_NS + 2)
    assert state.trigger == Decimal("37.83")
    assert state.source == source_of("38.80", HIGH_NS + 2, "37.83")


def test_adopted_level_names_the_row_it_came_from() -> None:
    state = new_trail()
    source = source_of("38.65", HIGH_NS, "37.68")

    state.adopt(Decimal("37.68"), AT_NS, source=source)
    assert state.trigger == Decimal("37.68")
    assert state.source == source

    # A raise replaces the trigger and the quote behind it (the caller guards the ratchet).
    raised = source_of("38.80", HIGH_NS + 1, "37.83")
    state.adopt(Decimal("37.83"), AT_NS + 1, source=raised)
    assert state.trigger == Decimal("37.83")
    assert state.source == raised


def test_breach_run_keeps_the_run_that_completed_the_breach() -> None:
    run = BreachRun()
    params = ExitParams.starts()
    trigger = Decimal("37.68")

    assert run.observe(Decimal("37.19"), trigger, AT_NS, params) is False
    assert run.observe(Decimal("37.20"), trigger, AT_NS + 500_000_000, params) is False
    assert run.observe(Decimal("37.10"), trigger, AT_NS + 1_000_000_000, params) is True
    assert list(run.seen) == [
        (AT_NS, Decimal("37.19")),
        (AT_NS + 500_000_000, Decimal("37.20")),
        (AT_NS + 1_000_000_000, Decimal("37.10")),
    ]

    # A print back above the trigger restarts the run, so the completing observations go too.
    assert run.observe(Decimal("37.70"), trigger, AT_NS + 2_000_000_000, params) is False
    assert list(run.seen) == []


def test_breach_run_keeps_at_most_sixty_four_observations() -> None:
    run = BreachRun()
    params = ExitParams.starts()
    trigger = Decimal("37.68")

    for index in range(100):
        assert run.observe(Decimal("37.00"), trigger, AT_NS + index, params) is False

    assert len(run.seen) == 64
    assert run.seen[0] == (AT_NS + 36, Decimal("37.00"))
    assert run.seen[-1] == (AT_NS + 99, Decimal("37.00"))


def test_rebuild_trigger_with_source_names_the_raising_row() -> None:
    rows = [
        QuoteObservation("ASTS", Decimal("38.50"), Decimal("38.55"), None, HIGH_NS - 2_000_000_000),
        QuoteObservation("ASTS", Decimal("38.65"), Decimal("38.70"), None, HIGH_NS),
    ]

    found = rebuild_trigger_with_source(
        rows, trail_fraction=Decimal("0.025"), params=ExitParams.starts()
    )

    assert found is not None
    assert (found.level, found.bid, found.ask, found.ts_ns) == (
        Decimal("37.68"),
        Decimal("38.65"),
        Decimal("38.70"),
        HIGH_NS,
    )
    assert rebuild_trigger(rows, trail_fraction=Decimal("0.025"), params=ExitParams.starts()) == (
        Decimal("37.68")
    )


# ---------------------------------------------------------------------------------------------
# The protector writes the record at every exit decision; no decision changes because of it.
# ---------------------------------------------------------------------------------------------


from test_b_protector_outside_rth import (  # noqa: E402
    NOW_NS,
    POST_NS,
    SEC,
    FakeClient,
    FakeHistory,
    api_order,
    api_position,
    api_update,
    b_account,
    b_params,
    drive_breach,
    start,
    tonight_client,
)

from sextant.paper.b_protector import Protector  # noqa: E402


def log_at(tmp_path: Path) -> tuple[DecisionLog, list[tuple[str, str | None, str]]]:
    pages: list[tuple[str, str | None, str]] = []
    log = DecisionLog(
        tmp_path / "b-decisions.jsonl",
        on_page=lambda reason, symbol, detail: pages.append((reason, symbol, detail)),
    )
    return log, pages


def read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def incident_history() -> FakeHistory:
    """The recorded after-hours tape: the 16:59:15 bid of 38.65 inside B's windows."""

    return FakeHistory(
        [
            QuoteObservation(
                "AAPL", Decimal("38.65"), Decimal("38.70"), None, 1_794_261_555_543_085_000
            )
        ]
    )


def test_the_recorded_after_hours_tape_names_the_38_65_bid_behind_37_68(tmp_path: Path) -> None:
    log, pages = log_at(tmp_path)
    p = Protector(
        tonight_client(),
        b_account(),
        b_params(),
        now_ns=lambda: POST_NS,
        feed="sip",
        history=incident_history(),
        decisions=log,
    )
    start(p)
    log.close()

    assert pages == []
    records = read_records(log.path)
    assert len(records) == 1
    record = records[0]
    assert record["action"] == "adopt"
    assert record["symbol"] == "AAPL"
    assert record["trigger"] == "37.68"
    assert record["trigger_source"] == {
        "kind": "tape_high",
        "level": "37.68",
        "quote": {
            "at": "2026-11-09T21:59:15.543085000Z",
            "bid": "38.65",
            "ask": "38.70",
        },
        "tape": {
            "window_start": "2026-11-09T21:00:00.000000000Z",
            "window_end": stamp(POST_NS),
            "feed": "sip",
            "pages": 1,
            "status": "ok",
        },
    }
    assert record["high_water"] == {
        "value": "38.65",
        "kind": "bid",
        "at": "2026-11-09T21:59:15.543085000Z",
    }


def test_a_release_records_the_trigger_its_high_and_the_completing_run(tmp_path: Path) -> None:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    log, pages = log_at(tmp_path)
    p = Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: NOW_NS,
        feed="sip",
        decisions=log,
    )
    p.reconcile()
    drive_breach(p)
    p.handle(api_update(trail, event="canceled"))
    log.close()

    assert pages == []
    records = read_records(log.path)
    assert [record["action"] for record in records] == ["adopt", "release"]
    release = records[1]
    assert release["at"] == stamp(NOW_NS + SEC)
    assert release["trigger"] == "99.45"
    assert release["trigger_source"] == {
        "kind": "native_stop",
        "level": "99.45",
        "stop_price": "99.45",
    }
    assert release["high_water"] == {
        "value": "99.45",
        "kind": "native_stop",
        "at": "2026-11-09T23:59:00.000000000Z",
        "detail": "the broker-held stop, the only high the resting trail reports",
    }
    assert [item["price"] for item in release["observations"]] == ["99.40", "99.40", "99.40"]
    assert [item["count"] for item in release["observations"]] == [1, 2, 3]
    assert {item["kind"] for item in release["observations"]} == {"trade"}
    assert release["noise"] == "0.01"
    assert release["noise_detail"] == {
        "one_tick": "0.01",
        "spread": "0.005",
        "floor": "0.01",
        "persist_count": "3",
        "persist_span_s": "1",
    }
    order = release["order"]
    assert order["side"] == "sell"
    assert order["quantity"] == "10"
    assert order["limit_price"] == "99.50"
    assert order["extended_hours"] is True
    assert order["submitted"] is True
    assert str(order["client_order_id"]).startswith("B001-outside-AAPL-")
    assert release["book"] == {
        "position": "10",
        "reserved": "0",
        "available": "10",
        "quantity": "10",
    }
    assert release["outcome"] == "released"


def test_a_cancelled_house_exit_records_its_reason(tmp_path: Path) -> None:
    client = FakeClient()
    client.positions = [api_position("12")]
    trail = api_order(qty="10", stop_price="97.50")
    client.orders = [trail]
    log, pages = log_at(tmp_path)
    p = Protector(client, b_account(), b_params(), now_ns=lambda: NOW_NS, feed="sip", decisions=log)
    p.reconcile()
    log.close()

    assert pages == []
    records = read_records(log.path)
    assert len(records) == 1
    cancel = records[0]
    assert cancel["action"] == "cancel"
    assert cancel["symbol"] == "AAPL"
    assert cancel["trigger"] == "97.50"
    assert cancel["note"] == "outside_rth_resize"
    assert cancel["outcome"] == "cancelled"
    assert cancel["order"]["quantity"] == "10"
    assert cancel["order"]["side"] == "sell"
    assert cancel["book"] == {
        "position": "12",
        "reserved": "0",
        "available": "12",
        "quantity": "10",
    }


def scenario(decisions: DecisionLog | None) -> tuple[list[dict[str, object]], list[str]]:
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = Protector(
        client,
        b_account(),
        b_params(),
        now_ns=lambda: NOW_NS,
        feed="sip",
        decisions=decisions,
    )
    p.reconcile()
    drive_breach(p)
    p.handle(api_update(trail, event="canceled"))
    return [order.model_dump() for order in client.submitted], [page.reason for page in p.pages]


def test_the_exit_decisions_are_identical_with_and_without_the_record(tmp_path: Path) -> None:
    log, _ = log_at(tmp_path)
    with_log = scenario(log)
    log.close()
    without_log = scenario(None)

    assert with_log == without_log
    assert len(read_records(log.path)) == 2


def test_a_write_failure_pages_and_never_blocks_or_changes_the_release(tmp_path: Path) -> None:
    def broken(line: str) -> None:
        raise OSError("the receipt disk is full")

    pages: list[tuple[str, str | None, str]] = []
    log = DecisionLog(
        tmp_path / "b-decisions.jsonl",
        writer=broken,
        on_page=lambda reason, symbol, detail: pages.append((reason, symbol, detail)),
    )
    client = FakeClient()
    client.positions = [api_position("10")]
    trail = api_order(stop_price="99.45")
    client.orders = [trail]
    p = Protector(client, b_account(), b_params(), now_ns=lambda: NOW_NS, feed="sip", decisions=log)
    p.reconcile()
    drive_breach(p)
    p.handle(api_update(trail, event="canceled"))
    log.close()

    assert ("decision_record_write_failed", None, "OSError") in pages
    assert p.pages == []
    limits = [order for order in client.submitted if isinstance(order, LimitOrderRequest)]
    assert len(limits) == 1
    assert Decimal(str(limits[0].limit_price)) == Decimal("99.50")

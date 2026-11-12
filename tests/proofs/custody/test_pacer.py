"""Catch priority inversion, window over-release and stale queued admission."""

from decimal import Decimal as D

import pytest

from sextant.execution.pacer import AccountPacer, ActionKind, PacerRefused

from .conftest import SEC, controls


def test_cancel_reduce_modify_query_submit_priority_and_fifo():
    clock = [0]
    p = AccountPacer(
        controls(request_budget=10, broker_limit=20, reserve_per_open_position=0), lambda: clock[0]
    )
    sent = []
    p.request(ActionKind.QUERY, lambda mark: (mark(), sent.append("first")))
    for kind, label in (
        (ActionKind.SUBMIT, "s1"),
        (ActionKind.QUERY, "q"),
        (ActionKind.MODIFY, "m"),
        (ActionKind.REDUCE, "r"),
        (ActionKind.CANCEL, "c1"),
        (ActionKind.CANCEL, "c2"),
    ):
        p.request(kind, lambda mark, label=label: (mark(), sent.append(label)))
    for _ in range(6):
        clock[0] += 7 * SEC
        p.drain()
    assert sent == ["first", "c1", "c2", "r", "m", "q", "s1"]
    assert p.wire_requests == 7


def test_no_rolling_window_exceeds_budget_and_each_list_member_costs_one():
    clock = [0]
    p = AccountPacer(controls(request_budget=20, broker_limit=25), lambda: clock[0])
    sends = []
    for _ in range(80):
        p.request(ActionKind.QUERY, lambda mark: (mark(), sends.append(clock[0])))
    for now in range(0, 240 * SEC, SEC // 10):
        clock[0] = now
        p.drain()
    assert len(sends) > 60
    assert max(sum(at <= t < at + 60 * SEC for t in sends) for at in sends) <= 20
    assert p.wire_requests == len(sends)


def test_pacer_rechecks_admission_after_wait_and_counts_only_wire_hook():
    clock, allowed = [0], [True]
    p = AccountPacer(
        controls(request_budget=10, broker_limit=20, reserve_per_open_position=0),
        lambda: clock[0],
    )
    sent = []
    p.request(ActionKind.QUERY, lambda mark: None)  # Work that sends nothing.
    p.request(
        ActionKind.SUBMIT, lambda mark: (mark(), sent.append("entry")), admission=lambda: allowed[0]
    )
    allowed[0] = False
    clock[0] = 10 * SEC
    p.drain()
    assert sent == []
    assert p.wire_requests == 0


def test_429_pauses_every_request_without_gifting_a_new_burst():
    clock = [0]
    p = AccountPacer(controls(request_budget=10, broker_limit=20), lambda: clock[0])
    sent = []
    p.on_rate_limited()
    p.request(ActionKind.CANCEL, lambda mark: (mark(), sent.append(clock[0])))
    clock[0] = SEC - 1
    p.drain()
    assert sent == []
    clock[0] = 7 * SEC
    p.drain()
    assert sent == [7 * SEC]
    assert p.wire_requests == 1


def test_unknown_reserves_refuse_entry_but_not_reducing_protection():
    clock = [0]
    p = AccountPacer(controls(reserve_per_open_position=None), lambda: clock[0])
    sent = []
    p.request(ActionKind.SUBMIT, lambda mark: (mark(), sent.append("entry")), open_positions=1)
    p.request(ActionKind.REDUCE, lambda mark: (mark(), sent.append("protect")), open_positions=1)
    assert sent == ["protect"]
    assert p.wire_requests == 1


def test_deferred_wire_hook_cannot_send_outside_its_paced_action():
    p = AccountPacer(controls(), lambda: 0)
    retained = []
    p.request(ActionKind.QUERY, lambda mark: retained.append(mark))
    with pytest.raises(ValueError, match="deferred"):
        retained[0]()
    assert p.wire_requests == 0


def test_restart_keeps_wire_requests_actions_and_fills_without_double_restore():
    p = AccountPacer(controls(), lambda: 0)
    p.request(ActionKind.SUBMIT, lambda mark: mark())
    p.request(ActionKind.QUERY, lambda mark: mark())
    p.record_fill()
    state = p.to_state("2026-11-06")
    assert state["new_orders"] == 1
    restored = AccountPacer(controls(), lambda: 0)
    restored.restore(state, day="2026-11-06")
    assert restored.wire_requests == 2
    assert restored.oer() == D("0.5")
    with pytest.raises(ValueError, match="restored"):
        restored.restore(state, day="2026-11-06")


def test_queued_entry_rechecks_current_position_protection_reserve():
    clock, positions, sent = [0], [0], []
    p = AccountPacer(controls(request_budget=20), lambda: clock[0])
    p.request(ActionKind.QUERY, lambda mark: mark())
    p.request(
        ActionKind.SUBMIT,
        lambda mark: (mark(), sent.append("entry")),
        position_counts=lambda: (positions[0], 0),
    )
    positions[0] = 1
    clock[0] = 4 * SEC
    p.drain()
    assert sent == []
    p.request(ActionKind.REDUCE, lambda mark: (mark(), sent.append("protection")))
    assert sent == ["protection"]


def test_five_entries_admit_at_the_180_18_registry_with_five_slots():
    clock, sent = [0], []
    p = AccountPacer(controls(slots=5, reserve_per_open_position=2), lambda: clock[0])
    for opened in range(5):
        p.request(
            ActionKind.SUBMIT,
            lambda mark, label=opened: (mark(), sent.append(label)),
            open_positions=opened,
        )
    assert sent == [0, 1, 2, 3, 4]
    assert p.wire_requests == 5


def test_ninth_entry_at_eight_open_is_refused_and_a_tenth_is_not_blocked():
    clock, sent = [0], []
    p = AccountPacer(controls(slots=5, reserve_per_open_position=2), lambda: clock[0])
    with pytest.raises(PacerRefused) as refused:
        p.request(
            ActionKind.SUBMIT,
            lambda mark: (mark(), sent.append("ninth")),
            open_positions=8,
        )
    assert refused.value.reason == "entry-reserve-exceeds-burst"
    p.request(
        ActionKind.SUBMIT,
        lambda mark: (mark(), sent.append("tenth")),
        open_positions=7,
    )
    assert sent == ["tenth"]


def test_twentieth_entry_admits_at_180_41_and_a_60s_window_stays_at_or_under_180():
    clock, sends = [0], []
    p = AccountPacer(
        controls(slots=20, reserve_per_open_position=2, burst_fraction="0.2278"),
        lambda: clock[0],
    )
    p.request(
        ActionKind.SUBMIT,
        lambda mark: (mark(), sends.append(clock[0])),
        open_positions=19,
    )
    assert len(sends) == 1
    for _ in range(200):
        clock[0] += SEC // 2
        p.request(ActionKind.QUERY, lambda mark: (mark(), sends.append(clock[0])))
    assert max(sum(at <= t < at + 60 * SEC for t in sends) for at in sends) <= 180
    assert p.wire_requests == len(sends)


def test_stale_entry_refused_at_drain_notifies_the_registration_owner():
    clock = [SEC]
    p = AccountPacer(controls(reserve_per_open_position=2), lambda: clock[0])
    refused, counts = [], [7]
    for _ in range(18):
        p.request(ActionKind.QUERY, lambda mark: mark())
    p.request(
        ActionKind.SUBMIT,
        lambda mark: mark(),
        position_counts=lambda: (counts[0], 0),
        refuse=refused.append,
    )
    assert p.queue and p.queue[0].kind == ActionKind.SUBMIT
    counts[0] = 8
    clock[0] += 10 * SEC
    p.drain()
    assert refused == ["entry-reserve-exceeds-burst"]
    assert p.queue == []

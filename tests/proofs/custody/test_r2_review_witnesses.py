"""Read-only R2 witnesses against assembled 30bec3b; fake venue only."""

import pytest

from .test_alpaca_node_owner import _owner
from .test_b_order_node import (
    AccountVenue,
    expected,  # noqa: F401 -- import the synthetic account fixture
    replace_on_venue,
    start,
    writes,
)


def test_terminal_old_writer_replacement_in_claims_is_admitted(tmp_path):
    venue = AccountVenue()
    parent = _owner(venue, "BTEST-old-root", symbol="ZA", side="sell")
    child = replace_on_venue(venue, parent, "owner-successor")
    child["status"] = "canceled"
    seeded = len(writes(venue))
    body, code, stand = start(tmp_path, venue)
    assert len(writes(venue)) == seeded, "the node must send nothing"
    actual = code, body["phase"], body["reason"], body["gate"]
    assert (code, body["phase"]) == (0, "stopped"), actual
    assert stand.subscribed > 0


def test_terminal_house_parent_with_working_cached_successor_refuses(tmp_path):
    venue = AccountVenue()
    parent = _owner(venue, "BTEST-old-root", symbol="ZA", side="sell", status="canceled")
    child = _owner(venue, "owner-successor", symbol="ZA", side="sell", status="new")
    parent["replaced_by"] = child["id"]
    child["replaces"] = parent["id"]
    seeded = len(writes(venue))
    body, code, stand = start(tmp_path, venue)
    assert len(writes(venue)) == seeded, "the node must send nothing"
    actual = code, body["phase"], body["reason"], body["gate"]
    assert (code, body["phase"], body["reason"]) == (2, "refused", "house_order_open"), actual
    assert stand.subscribed == 0


@pytest.mark.parametrize(
    ("tail", "phase", "reason", "opened", "unresolved"),
    [
        ("new", "refused", "house_order_open", 1, 0),
        ("canceled", "stopped", None, 0, 0),
        ("missing", "refused", "replacement_unresolved", 0, 1),
        ("cycle", "refused", "replacement_unresolved", 0, 1),
    ],
)
def test_cached_terminal_middle_preserves_lineage(
    tmp_path, tail, phase, reason, opened, unresolved
):
    venue = AccountVenue()
    parent = _owner(venue, "BTEST-root", symbol="ZA", side="sell", status="canceled")
    middle = _owner(venue, "owner-middle", symbol="ZA", side="sell", status="canceled")
    child = _owner(venue, "owner-tail", symbol="ZA", side="sell", status="canceled")
    parent["replaced_by"], middle["replaced_by"] = middle["id"], child["id"]
    if tail == "missing":
        del venue.rows[child["id"]]
    elif tail == "cycle":
        child["replaced_by"] = middle["id"]
    else:
        child["status"] = tail
    seeded = len(writes(venue))
    body, code, stand = start(tmp_path, venue)
    assert len(writes(venue)) == seeded, "the node must send nothing"
    assert (code, body["phase"], body["reason"]) == (
        0 if phase == "stopped" else 2,
        phase,
        reason,
    )
    assert body["gate"]["open_house_orders"] == opened
    assert body["gate"]["unresolved_replacements"] == unresolved
    assert bool(stand.subscribed) == (phase == "stopped")


def test_cached_house_successor_is_counted_once_across_house_ancestors(tmp_path):
    venue = AccountVenue()
    parents = [
        _owner(venue, name, symbol="ZA", side="sell", status="canceled")
        for name in ("BTEST-first", "BTEST-second")
    ]
    child = _owner(venue, "BTEST-successor", symbol="ZA", side="sell", status="new")
    for parent in parents:
        parent["replaced_by"] = child["id"]
    seeded = len(writes(venue))
    body, code, stand = start(tmp_path, venue)
    assert len(writes(venue)) == seeded, "the node must send nothing"
    assert (code, body["phase"], body["reason"]) == (2, "refused", "house_order_open")
    assert body["gate"]["open_house_orders"] == 1
    assert body["gate"]["unresolved_replacements"] == 0
    assert stand.subscribed == 0

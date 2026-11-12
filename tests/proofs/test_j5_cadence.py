"""Contract-first synthetic acceptance for lead 2026-11-08 19:49:24 item 1.

No controller runs: clocks, weekly usage, enablement, read-back and pager are fixtures.
"""

import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sextant.tradingview.inputs import _ns

ROOT = Path(__file__).parents[2]
REGISTRY = ROOT / "lanes/tradingview/j5-cadence.params.json"
NS = 1_000_000_000


def api():
    try:
        return importlib.import_module("sextant.tradingview.cadence")
    except ModuleNotFoundError:
        pytest.fail("The disabled owner-ruled J5 cadence policy is not implemented")


def at(clock="07:00:00", day="2026-11-09", offset="-05:00"):
    return datetime.fromisoformat(f"{day}T{clock}{offset}")


def policy(tmp_path, *, enabled=True, pages=None, baseline=None, landed=True):
    module = api()
    raw = json.loads(REGISTRY.read_text())
    for row in raw["rows"]:
        if row["name"] == "enabled":
            row.update(value=int(enabled), source="synthetic enable row, no rollout authority")
    path = tmp_path / "fixture-params.json"
    path.write_text(json.dumps(raw))
    return module.Cadence(
        tmp_path / "cadence-state",
        path,
        page=(pages.append if pages is not None else lambda _: None),
        landed_sha="f" * 40 if landed else "",
        enable_source="synthetic lead enables after landing",
        initial_membership={"NS Ranked": []} if baseline is None else baseline,
        initial_source="synthetic complete by-ID read-back",
    )


def unit(now, members=("NASDAQ:A",)):
    return {
        "schema": "sextant.j5-ranked/1",
        "status": "fresh",
        "display_only": True,
        "paper_slot": 2,
        "generated_at_ns": _ns(now),
        "expires_at_ns": _ns(now) + 2 * NS,
        "generation_sha256": "a" * 64,
        "watchlists": {"NS Ranked": list(members)},
    }


def claim(gate, now, members=("NASDAQ:A",), usage="30"):
    return gate.claim(
        unit(now, members),
        now=now,
        weekly_usage_pct=Decimal(usage),
        usage_source="synthetic Windows Claude weekly-usage receipt",
    )


def finish(gate, run, now, *, successful=True):
    gate.finish(
        run["run_id"],
        ended_at=now,
        successful=successful,
        receipt_source="synthetic complete J5 result/read-back",
    )


def test_j5_ships_enabled_with_landing_and_lead_enablement(tmp_path):
    assert REGISTRY.is_file(), "The owner-ruled cadence registry is missing"
    raw = json.loads(REGISTRY.read_text())
    enabled = next(r for r in raw["rows"] if r["name"] == "enabled")
    assert enabled["value"] == 1
    assert "LEAD-1219" in enabled["source"]
    assert raw["measurements"]["first_session_j5_run_time_ns"] is None
    disabled = policy(tmp_path, enabled=False)
    assert claim(disabled, at()) is None
    missing_landing = policy(tmp_path, landed=False)
    assert claim(missing_landing, at()) is None
    missing_landing.landed_sha = "f" * 40
    missing_landing.enable_source = ""
    assert claim(missing_landing, at()) is None


def display_unit(now, movers=("NASDAQ:A",), focus=("NASDAQ:A",)):
    """The loop's explicit two-list admission view; never passed off as a producer unit."""
    return {
        "schema": "sextant.display-admission/1",
        "status": "fresh",
        "display_only": True,
        "paper_slot": 2,
        "generated_at_ns": _ns(now),
        "expires_at_ns": _ns(now) + 2 * NS,
        "watchlists": {"NS movers": list(movers), "NS focus": list(focus)},
    }


def test_j5_display_admission_keeps_both_lists_and_claims_a_cross_list_move(tmp_path):
    module = api()
    assert module._members({"NS movers": ["NASDAQ:B", "NASDAQ:A"], "NS focus": ["NASDAQ:A"]}) == {
        "NS movers": ["NASDAQ:A", "NASDAQ:B"],
        "NS focus": ["NASDAQ:A"],
    }
    for bad in (
        {},
        {"NS Ranked": ["NASDAQ:A"], "NS focus": []},
        {"NS movers": ["NASDAQ:A"]},
        {"NS movers": ["NASDAQ:A"], "NS focus": [], "NS Ranked": []},
    ):
        with pytest.raises(ValueError):
            module._members(bad)
    now = at()
    gate = policy(tmp_path, baseline={"NS movers": ["NASDAQ:A", "NASDAQ:B"], "NS focus": []})
    moved = gate.claim(
        display_unit(now, ("NASDAQ:A",), ("NASDAQ:B",)),
        now=now,
        weekly_usage_pct=Decimal(30),
        usage_source="synthetic display admission",
    )
    assert moved and moved["members"] == {
        "NS movers": ["NASDAQ:A"],
        "NS focus": ["NASDAQ:B"],
    }
    finish(gate, moved, now + timedelta(seconds=1))
    later = at("08:00:00")
    empty = gate.claim(
        display_unit(later, (), ("NASDAQ:C",)),
        now=later,
        weekly_usage_pct=Decimal(30),
        usage_source="synthetic display admission",
    )
    assert empty is None


def test_j5_display_admission_with_the_revoked_label_is_refused(tmp_path):
    """A display view still labelled with the revoked third account claims nothing."""
    now = at()
    gate = policy(tmp_path, baseline={"NS movers": ["NASDAQ:A", "NASDAQ:B"], "NS focus": []})
    stale_label = display_unit(now, ("NASDAQ:A",), ("NASDAQ:B",))
    stale_label["paper_slot"] = 3
    assert (
        gate.claim(
            stale_label,
            now=now,
            weekly_usage_pct=Decimal(30),
            usage_source="synthetic display admission",
        )
        is None
    )


def test_j5_syncs_only_changed_membership_not_order_or_score(tmp_path):
    gate = policy(tmp_path, baseline={"NS Ranked": ["NASDAQ:B", "NASDAQ:A"]})
    assert claim(gate, at(), ("NASDAQ:A", "NASDAQ:B")) is None
    changed = claim(gate, at(), ("NASDAQ:C",))
    assert changed
    finish(gate, changed, at("07:00:03"))
    same = unit(at("08:00:00"), ("NASDAQ:C",))
    same["generation_sha256"] = "b" * 64
    same["ranking_generation"] = {"score": "999.123456789"}
    assert (
        gate.claim(same, now=at("08:00:00"), weekly_usage_pct=Decimal(30), usage_source="fixture")
        is None
    )


@pytest.mark.parametrize("clock", ("07:00:00", "10:27:30", "15:00:00", "15:57:30"))
def test_j5_onset_floor_is_150_seconds(tmp_path, clock):
    gate = policy(tmp_path)
    now = at(clock)
    run = claim(gate, now)
    assert run and run["floor_s"] == 150
    finish(gate, run, now + timedelta(seconds=1))
    assert claim(gate, now + timedelta(seconds=149), ("NASDAQ:B",)) is None
    # These late-window cases cross into the 300s window and must obey that new floor.
    later = now + timedelta(seconds=150)
    if later.time() in (at("10:30:00").time(), at("16:00:00").time()):
        assert claim(gate, later, ("NASDAQ:B",)) is None
        later = now + timedelta(seconds=300)
    assert claim(gate, later, ("NASDAQ:B",))


@pytest.mark.parametrize("clock", ("04:00:00", "06:00:00", "10:30:00", "16:00:00", "19:54:59"))
def test_j5_other_hours_floor_is_300_seconds(tmp_path, clock):
    gate = policy(tmp_path)
    now = at(clock)
    run = claim(gate, now)
    assert run and run["floor_s"] == 300
    finish(gate, run, now + timedelta(seconds=1))
    assert claim(gate, now + timedelta(seconds=299), ("NASDAQ:B",)) is None
    assert claim(gate, now + timedelta(seconds=300), ("NASDAQ:B",))


@pytest.mark.parametrize(
    "clock,floor",
    [
        ("03:59:59", None),
        ("04:00:00", 300),
        ("06:59:59", 300),
        ("07:00:00", 150),
        ("10:29:59", 150),
        ("10:30:00", 300),
        ("14:59:59", 300),
        ("15:00:00", 150),
        ("15:59:59", 150),
        ("16:00:00", 300),
        ("19:59:59", 300),
        ("20:00:00", None),
        ("23:59:59", None),
        ("00:00:00", None),
    ],
)
def test_j5_et_window_boundaries_and_overnight_wait(tmp_path, clock, floor):
    gate = policy(tmp_path)
    assert gate.floor(at(clock), Decimal(30)) == floor
    result = claim(gate, at(clock))
    assert (result is not None) == (floor is not None)


def test_j5_uses_eastern_dst_and_the_native_session_calendar(tmp_path):
    gate = policy(tmp_path)
    for clock, day, offset in (
        ("07:00:00", "2026-03-06", "-05:00"),
        ("07:00:00", "2026-03-09", "-04:00"),
    ):
        instant = at(clock, day, offset)
        utc = instant.astimezone(datetime.fromisoformat("2026-01-01T00:00:00+00:00").tzinfo)
        assert gate.floor(utc, Decimal(30)) == 150
    assert gate.floor(at(day="2026-11-08"), Decimal(30)) is None


def test_j5_usage_above_80_percent_pages_and_backs_off_to_900_seconds(tmp_path):
    pages = []
    gate = policy(tmp_path, pages=pages)
    assert gate.floor(at(), Decimal("80")) == 150
    run = claim(gate, at(), usage="80.0001")
    assert run and run["floor_s"] == 900 and len(pages) == 1
    assert "80" in pages[0] and "900" in pages[0]
    finish(gate, run, at("07:00:03"))
    assert claim(gate, at("07:14:59"), ("NASDAQ:B",), usage="81") is None
    assert len(pages) == 1
    restarted = policy(tmp_path, pages=pages)
    assert claim(restarted, at("07:15:00"), ("NASDAQ:B",), usage="81")
    assert len(pages) == 1


def test_j5_usage_pages_even_without_membership_change_and_requires_a_real_receipt(tmp_path):
    pages = []
    gate = policy(tmp_path, pages=pages, baseline={"NS Ranked": ["NASDAQ:A"]})
    assert claim(gate, at(), usage="81") is None and len(pages) == 1
    assert gate.claim(unit(at()), now=at(), weekly_usage_pct=None, usage_source="") is None
    assert (
        gate.claim(unit(at()), now=at(), weekly_usage_pct=Decimal("NaN"), usage_source="fixture")
        is None
    )
    assert gate.claim(unit(at()), now=at(), weekly_usage_pct=Decimal("81"), usage_source="") is None


def test_j5_admission_serializes_processes_and_refuses_overlaps(tmp_path):
    gate = policy(tmp_path)
    run = claim(gate, at())
    assert run
    assert claim(gate, at("07:30:00"), ("NASDAQ:B",)) is None
    script = """
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys
from sextant.tradingview.cadence import Cadence
gate=Cadence(Path(sys.argv[1]),Path(sys.argv[2]),page=lambda _:None,landed_sha='f'*40,
             enable_source='synthetic',initial_membership={'NS Ranked':[]},initial_source='fixture')
now=datetime.fromisoformat('2026-11-09T08:00:00-05:00')
doc={'schema':'sextant.j5-ranked/1','status':'fresh','display_only':True,'paper_slot':3,
     'generated_at_ns':0,'expires_at_ns':9999999999999999999,
     'watchlists':{'NS Ranked':['NASDAQ:C']}}
assert gate.claim(doc,now=now,weekly_usage_pct=Decimal(30),usage_source='fixture') is None
print('overlap_refused')
"""
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "cadence-state"),
            str(tmp_path / "fixture-params.json"),
        ],
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "overlap_refused"


def test_j5_failed_or_abandoned_runs_are_not_retried_after_restart(tmp_path):
    gate = policy(tmp_path)
    run = claim(gate, at())
    assert run
    assert claim(policy(tmp_path), at("08:00:00")) is None  # Process died with an active receipt.
    finish(gate, run, at("07:00:03"), successful=False)
    restarted = policy(tmp_path)
    assert claim(restarted, at("09:00:00")) is None
    assert claim(restarted, at("09:00:00"), ("NASDAQ:NEW",)) is None


def test_j5_first_session_runtime_is_a_recorded_field_not_a_measurement_now(tmp_path):
    gate = policy(tmp_path)
    run = claim(gate, at())
    assert run
    before = json.loads(gate.state_path.read_text())
    assert before["first_session_j5_run_time_ns"] is None
    finish(gate, run, at("07:00:03"))
    measured = json.loads(gate.state_path.read_text())
    assert measured["first_session_j5_run_time_ns"] == 3 * NS
    assert measured["first_session_date"] == "2026-11-09"
    assert measured["run_receipt_source"].startswith("synthetic")
    second = claim(gate, at("07:03:00"), ("NASDAQ:B",))
    assert second
    finish(gate, second, at("07:03:07"))
    later = json.loads(gate.state_path.read_text())
    assert later["first_session_j5_run_time_ns"] == 3 * NS
    assert later["last_j5_run_time_ns"] == 7 * NS
    assert json.loads(REGISTRY.read_text())["measurements"]["first_session_j5_run_time_ns"] is None


@pytest.mark.parametrize(
    "bad", ("missing", "source", "calibration", "float", "duplicate", "overnight")
)
def test_j5_registry_requires_every_sourced_uncalibrated_row(tmp_path, bad):
    gate = policy(tmp_path)
    raw = json.loads(gate.registry_path.read_text())
    if bad == "missing":
        raw["rows"].pop()
    elif bad == "duplicate":
        raw["rows"].append(raw["rows"][0])
    elif bad == "overnight":
        next(r for r in raw["rows"] if r["name"] == "overnight_enabled")["value"] = 1
    else:
        key, value = {
            "source": ("source", ""),
            "calibration": ("calibration_state", "CALIBRATED"),
            "float": ("value", 150.0),
        }[bad]
        raw["rows"][0][key] = value
    gate.registry_path.write_text(json.dumps(raw))
    with pytest.raises((TypeError, ValueError)):
        api().Cadence(tmp_path / "other", gate.registry_path, page=lambda _: None)


@pytest.mark.parametrize(
    "bad", ("stale", "future", "revoked_slot3", "unavailable", "unknown_members", "alias")
)
def test_j5_never_admits_stale_or_invalid_producer_input(tmp_path, bad):
    gate = policy(tmp_path)
    now = at()
    doc = unit(now)
    if bad == "stale":
        doc["expires_at_ns"] = _ns(now)
    elif bad == "future":
        doc["generated_at_ns"] = _ns(now) + 1
    elif bad == "revoked_slot3":
        doc["paper_slot"] = 3
    elif bad == "unavailable":
        doc["status"] = "unavailable"
    elif bad == "unknown_members":
        doc["watchlists"] = None
    else:
        doc["watchlists"] = {"NS Ranked": ["NASDAQ:A", "NASDAQ:A"]}
    assert gate.claim(doc, now=now, weekly_usage_pct=Decimal(30), usage_source="fixture") is None


def test_j5_usage_page_is_not_suppressed_by_unavailable_rank_data(tmp_path):
    pages = []
    gate = policy(tmp_path, pages=pages)
    doc = unit(at())
    doc.update(status="unavailable", watchlists=None)
    assert gate.claim(doc, now=at(), weekly_usage_pct=Decimal(81), usage_source="fixture") is None
    assert len(pages) == 1


def test_j5_pager_failure_latches_admission_and_preserves_pending_page(tmp_path):
    gate = policy(tmp_path)

    def failed_page(_):
        raise OSError("synthetic pager failure")

    gate.page = failed_page
    assert claim(gate, at(), usage="81") is None
    saved = json.loads(gate.state_path.read_text())
    assert saved["failed"] and saved["page_pending"] and saved["active"] is None
    assert claim(policy(tmp_path), at("08:00:00")) is None


def test_j5_completion_requires_matching_outcome_and_nonregressing_clock(tmp_path):
    gate = policy(tmp_path)
    run = claim(gate, at())
    assert run
    with pytest.raises(ValueError, match="matching"):
        finish(gate, {"run_id": "foreign"}, at("07:00:03"))
    with pytest.raises(ValueError, match="regressed"):
        finish(gate, run, at("06:59:59"))
    assert claim(policy(tmp_path), at("08:00:00")) is None
    finish(gate, run, at("07:00:03"))


def test_j5_policy_changes_need_reviewed_state_migration(tmp_path):
    gate = policy(tmp_path)
    assert claim(gate, at())
    raw = json.loads(gate.registry_path.read_text())
    next(r for r in raw["rows"] if r["name"] == "onset_floor_s")["value"] = 151
    gate.registry_path.write_text(json.dumps(raw))
    changed = api().Cadence(
        tmp_path / "cadence-state",
        gate.registry_path,
        page=lambda _: None,
        landed_sha="f" * 40,
        enable_source="fixture",
    )
    with pytest.raises(ValueError, match="migration"):
        claim(changed, at("08:00:00"))

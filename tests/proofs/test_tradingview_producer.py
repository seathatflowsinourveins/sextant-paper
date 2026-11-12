"""Synthetic live seam proofs; fixtures grant no stream or deployment authority.

Adapted from ea95075's producer proofs and main's actual ranking fixture.
M2@abe8432's two-clock witnesses supply the archive/market counterfactual.
"""

import builtins
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.trading.client import TradingClient
from nautilus_trader.model import TradeTick
from test_ranking import DAY, NS, START, publisher, tick

from sextant.data import alpaca_history
from sextant.execution import state
from sextant.execution.custody import CustodyError
from sextant.ranking.generation import Coverage, CoverageState, RankingGeneration, generation_digest
from sextant.ranking.registry import RegistryRow
from sextant.tradingview.producer import (
    SCOPE,
    Clocks,
    LiveProducer,
    reserve_stream,
    snapshot_pacer,
    snapshot_request,
)

AT = START + 360 * NS + 1
ACQUIRED = START + 80 * 86400 * NS
MAPPING = Clocks(START - NS, ACQUIRED, "synthetic verified listing map")
ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def no_online_or_credentials(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline proof attempted a client, credential lookup or connection")

    for client in (StockDataStream, StockHistoricalDataClient, TradingClient):
        monkeypatch.setattr(client, "__init__", refuse)
    monkeypatch.setattr(alpaca_history, "environment_credentials", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    original_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)) and any(
            name in str(file) for name in ("with-keys.sh", "/.aws/", "credentials.json", "keys.sh")
        ):
            refuse()
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)


@pytest.fixture
def live(tmp_path):
    with reserve_stream(
        tmp_path / "leases",
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; synthetic stop receipt",
    ) as lease:
        yield LiveProducer(publisher(), lease, sip_authority="synthetic SIP entitlement only")


def observation(live, second, price="0.50", name="PENNY", *, acquired=ACQUIRED, **facts):
    raw = tick(second, price, name)
    archived = TradeTick(
        raw.instrument_id,
        raw.price,
        raw.size,
        raw.aggressor_side,
        raw.trade_id,
        raw.ts_event,
        acquired,
    )
    clocks = facts.pop("clocks", Clocks(raw.ts_init, acquired, "synthetic stream receipt"))
    return live.on_trade(
        archived,
        clocks=clocks,
        now=facts.pop("now", raw.ts_init),
        price_eligible=facts.pop("price_eligible", True),
        volume_eligible=facts.pop("volume_eligible", True),
        conditions=facts.pop("conditions", ()),
        mask_source=facts.pop("mask_source", "synthetic masks"),
        **facts,
    )


def warm(live, *, acquired=ACQUIRED):
    for second in (0, 60, 120, 180, 240, 300, 310, 320, 330, 340, 350, 360):
        for name in live.publisher.universe:
            observation(live, second, "0.55" if second > 300 else "0.50", name, acquired=acquired)


def unit(live, *, now=AT, symbol_map=None, mapping_clocks=MAPPING):
    return live.build_unit(
        now=now,
        mapping_clocks=mapping_clocks,
        symbol_map=symbol_map
        if symbol_map is not None
        else {name: f"NASDAQ:{name}" for name in live.publisher.universe},
    )


def test_real_generation_keeps_scores_unknowns_full_universe_and_display_only(live, tmp_path):
    warm(live)
    assert live.heartbeat(at=AT, received_ns=AT)
    result = unit(live)
    assert result["status"] == "fresh", result
    native = RankingGeneration.from_json(result["ranking_generation"])
    assert len(native.rows) == 3
    assert result["watchlists"] == {"NS Ranked": ["NASDAQ:ADR", "NASDAQ:LARGE"]}
    assert all(row.score == Decimal("0.105") for row in native.rows)
    assert all(
        row.focus_eligible is False for row in native.rows
    )  # Entry flags never filter display.
    assert generation_digest(native) == result["generation_sha256"]
    assert result["params_hash"] == live.publisher.registry.params_hash
    assert result["display_only"] is True and result["paper_slot"] == 2
    assert "NS B-protected" not in result["watchlists"]
    assert (
        live.publish_unit(
            tmp_path / "latest.json",
            now=AT,
            symbol_map={name: f"NASDAQ:{name}" for name in live.publisher.universe},
            mapping_clocks=MAPPING,
        )
        == result
    )
    assert json.loads((tmp_path / "latest.json").read_text()) == result


def test_two_clocks_use_market_availability_not_archive_initialization(live):
    warm(live)
    live.heartbeat(at=AT, received_ns=AT)
    result = unit(live)
    control = publisher()
    for second in (0, 60, 120, 180, 240, 300, 310, 320, 330, 340, 350, 360):
        for name in control.universe:
            control.on_trade(tick(second, "0.55" if second > 300 else "0.50", name))
    expected = control.publish(now=AT)
    assert expected and result["generation_sha256"] == generation_digest(expected)
    clocks = result["provenance"]["trades"]["ADR"]
    assert clocks["ACQUIRED_AT"] == clocks["native_ts_init"] == ACQUIRED > AT
    assert clocks["MARKET_AVAILABLE_AT"] == AT
    before = json.dumps(result, sort_keys=True)
    observation(live, 361, "0.56")
    assert json.dumps(result, sort_keys=True) == before  # A captured unit cannot drift in memory.


def test_future_market_input_cannot_change_an_earlier_publication(live):
    warm(live)
    before = live.publisher.states.copy()
    assert not observation(live, 361, "9.00", now=AT)
    assert live.publisher.states == before and live.error is None
    live.heartbeat(at=AT, received_ns=AT)
    assert unit(live)["status"] == "fresh"


@pytest.mark.parametrize("condition", ("G", "U", "Z", "6", "M", "9"))
def test_late_and_closing_conditions_update_neither_measure(live, condition):
    observation(live, 0)
    before = live.publisher.states["PENNY"]
    observation(live, 1, "9.00", conditions=(condition,))
    assert live.publisher.states["PENNY"] == before


def test_odd_lot_masks_are_separate_and_out_of_order_poisoning_has_no_fallback(live):
    observation(live, 0)
    observation(live, 1, "9.00", price_eligible=False, volume_eligible=True, conditions=("I",))
    row = live.publisher.states["PENNY"].latest
    assert row and row.observations["price"] == Decimal("0.50")
    assert row.observations["dollar_volume"] == Decimal("95.00")
    with pytest.raises(ValueError, match="market clocks"):
        observation(live, 0)
    assert unit(live)["watchlists"] is None


@pytest.mark.parametrize("field,value", [("mask_source", ""), ("price_eligible", 1)])
def test_unknown_condition_authority_refuses_and_invalidates(live, field, value):
    with pytest.raises(ValueError):
        observation(live, 0, **{field: value})
    assert live.error and unit(live)["watchlists"] is None


def test_snapshot_preserves_decimal_bytes_without_counting_volume_or_inventing_close(live):
    warm(live)
    before = live.publisher.states.copy()
    raw = (
        '{"ADR":{"latestTrade":{"p":999.999999999,"s":900},'
        '"dailyBar":{"v":9000,"vw":1.234567891,"n":37},"prevDailyBar":{"c":999}}}'
    )
    assert live.on_snapshot(raw, clocks=Clocks(AT, ACQUIRED, "synthetic REST receipt"), now=AT)
    assert live.snapshot and live.snapshot["response_json"] == raw
    assert live.publisher.states == before
    assert live.publisher.references["ADR"].prior_close == Decimal("0.50")
    future = Clocks(AT + 1, ACQUIRED, "future synthetic snapshot")
    assert not live.on_snapshot(raw, clocks=future, now=AT)
    assert live.snapshot["MARKET_AVAILABLE_AT"] == AT


@pytest.mark.parametrize("raw", ["{}", '{"FOREIGN":{}}', '{"ADR":{"p":NaN}}', "[]"])
def test_invalid_snapshot_does_not_leave_a_valid_fallback(live, raw):
    with pytest.raises(ValueError):
        live.on_snapshot(raw, clocks=Clocks(AT, ACQUIRED, "fixture"), now=AT)
    assert unit(live)["watchlists"] is None


def test_missing_heartbeat_and_stale_inputs_never_clear_or_reuse_a_list(live, tmp_path):
    warm(live)
    assert unit(live)["reason"] == "heartbeat_missing"
    live.heartbeat(at=AT, received_ns=AT)
    first = unit(live)
    assert first["status"] == "fresh"
    throttled = unit(live, now=AT + NS // 2)
    assert throttled["generation_sha256"] == first["generation_sha256"]
    assert throttled["generated_at_ns"] == first["generated_at_ns"]
    assert throttled["expires_at_ns"] == first["expires_at_ns"]
    later = AT + 3 * NS
    live.heartbeat(at=later, received_ns=later)
    result = live.publish_unit(
        tmp_path / "latest.json",
        now=later,
        mapping_clocks=MAPPING,
        symbol_map={name: f"NASDAQ:{name}" for name in live.publisher.universe},
    )
    assert result["status"] == "unavailable" and result["watchlists"] is None
    assert json.loads((tmp_path / "latest.json").read_text())["watchlists"] is None


def test_one_busy_symbol_cannot_refresh_another_projected_symbols_price(live):
    warm(live)
    live.heartbeat(at=AT, received_ns=AT)
    assert unit(live)["status"] == "fresh"
    observation(live, 363, "0.56", "PENNY")
    now = START + 363 * NS + 1
    live.heartbeat(at=now, received_ns=now)
    assert unit(live, now=now)["reason"] == "projected symbol input stale"


@pytest.mark.parametrize("cause", ("disconnect", "correction", "cancel_error", "missing_prefix"))
def test_data_gap_or_revision_requires_recovered_epoch(live, cause):
    warm(live)
    live.heartbeat(at=AT, received_ns=AT)
    assert unit(live)["status"] == "fresh"
    live.invalidate(cause)
    assert unit(live)["watchlists"] is None
    with pytest.raises(ValueError):
        observation(live, 361)


@pytest.mark.parametrize(
    "kind", ("unknown", "degraded", "missing", "mixed_source", "score", "partial")
)
def test_main_reader_refuses_incomplete_or_fabricated_generations(live, monkeypatch, kind):
    warm(live)
    live.heartbeat(at=AT, received_ns=AT)
    if kind in {"unknown", "degraded"}:
        live.publisher.set_coverage(Coverage(CoverageState(kind.upper()), False))
    elif kind == "missing":
        live.publisher.states = {
            name: replace(s, latest=None) for name, s in live.publisher.states.items()
        }
    else:
        original = live.publisher.publish

        def altered(*, now):
            record = original(now=now)
            assert record
            first = record.rows[0]
            if kind == "score":
                first = replace(first, score=Decimal("999"))
            elif kind == "partial":
                first = replace(first, partial=True)
            else:
                reasons = list(first.reasons)
                reasons[0] = replace(reasons[0], detail='{"feed":"boats"}')
                first = replace(first, reasons=tuple(reasons))
            return replace(record, rows=(first, *record.rows[1:]))

        monkeypatch.setattr(live.publisher, "publish", altered)
    assert unit(live)["watchlists"] is None


@pytest.mark.parametrize(
    "mapping", [{}, {"ADR": "ALPACA:ADR"}, {"ADR": "NASDAQ:X", "LARGE": "NASDAQ:X"}]
)
def test_missing_routing_or_alias_mapping_never_guesses_a_listing(live, mapping):
    warm(live)
    live.heartbeat(at=AT, received_ns=AT)
    assert unit(live, symbol_map=mapping)["watchlists"] is None


def test_future_mapping_and_bad_heartbeat_cannot_authorize_a_display(live):
    warm(live)
    live.heartbeat(at=AT, received_ns=AT)
    assert unit(live, mapping_clocks=Clocks(AT + 1, ACQUIRED, "future"))["watchlists"] is None
    assert not live.heartbeat(at=AT + 1, received_ns=AT)
    assert unit(live)["watchlists"] is None


def test_durable_publication_failure_preserves_the_previous_complete_file(
    live, tmp_path, monkeypatch
):
    warm(live)
    live.heartbeat(at=AT, received_ns=AT)
    path = tmp_path / "latest.json"
    kwargs = dict(
        now=AT,
        symbol_map={s: f"NASDAQ:{s}" for s in live.publisher.universe},
        mapping_clocks=MAPPING,
    )
    live.publish_unit(path, **kwargs)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(state.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic"):
        live.publish_unit(path, **kwargs)
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == {tmp_path / "leases", path}


@pytest.mark.parametrize("slot", (1, 3, 4, True, "2"))
def test_slot_three_and_every_other_slot_refuse_before_a_lease(tmp_path, slot):
    with pytest.raises(ValueError, match="slot 3 is revoked"):
        with reserve_stream(
            tmp_path / "absent",
            paper_slot=slot,
            existing_streams=0,
            source="B's writer opens no market-data stream; fixture",
        ):
            pytest.fail("wrong slot admitted")
    assert not (tmp_path / "absent").exists()


def test_slot_two_reserves_the_ranking_host_market_data_stream(tmp_path):
    source = "B's writer opens no market-data stream; slot 2's one stream is free"
    with reserve_stream(
        tmp_path / "leases", paper_slot=2, existing_streams=0, source=source
    ) as lease:
        assert lease.held
        assert lease.scope == SCOPE


def test_existing_external_stream_and_concurrent_process_refuse_a_second_stream(tmp_path):
    with pytest.raises(ValueError, match="second stream"):
        with reserve_stream(
            tmp_path,
            paper_slot=2,
            existing_streams=1,
            source="B's writer opens no market-data stream; fixture",
        ):
            pytest.fail("second stream admitted")
    with reserve_stream(
        tmp_path,
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ):
        script = """
from pathlib import Path
import sys
from sextant.execution.custody import CustodyError
from sextant.tradingview.producer import reserve_stream
try:
    source = "B's writer opens no market-data stream; fixture"
    with reserve_stream(
        Path(sys.argv[1]), paper_slot=2, existing_streams=0, source=source
    ):
        raise AssertionError('second reservation admitted')
except CustodyError:
    print('second_stream_refused')
"""
        done = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "src")},
            timeout=30,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "second_stream_refused"
    with reserve_stream(
        tmp_path,
        paper_slot=2,
        existing_streams=0,
        source="B's writer opens no market-data stream; fixture",
    ) as lease:
        assert lease.held


def test_release_and_unknown_entitlement_revoke_the_seam(live):
    with pytest.raises(ValueError, match="entitlement"):
        LiveProducer(publisher(), live.lease, sip_authority="")
    live.lease.close()
    assert unit(live)["watchlists"] is None
    assert not live.heartbeat(at=AT, received_ns=AT)
    with pytest.raises(ValueError):
        snapshot_request(live.lease, ("ADR",))


def rate(value=200):
    return RegistryRow(
        "snapshot_requests_per_minute",
        "requests/minute",
        value,
        "synthetic shared allocation; official Basic ceiling",
        "UNCALIBRATED",
        DAY,
    )


def test_native_snapshot_request_and_shared_pacer_stay_inside_documented_budget(live):
    clock = [0.0]
    sent = []

    def sleep(delay):
        clock[0] = round(clock[0] + delay, 9)

    snapshot_pacer(live.lease, rate(), clock=lambda: clock[0], sleep=sleep)
    for _ in range(201):
        request = snapshot_request(live.lease, ("ADR", "PENNY"))
        sent.append(clock[0])
        assert request.feed and request.feed.value == "sip"
        assert request.symbol_or_symbols == ["ADR", "PENNY"]
    assert sum(at < 60 for at in sent) == 200 and sent[-1] >= 60
    with pytest.raises(CustodyError, match="one shared pacer"):
        snapshot_pacer(live.lease, rate(), clock=lambda: clock[0], sleep=sleep)
    with pytest.raises(ValueError):
        snapshot_request(live.lease, ("ADR", "ADR"))


@pytest.mark.parametrize("value", (0, 201, 10000, True, 2.5))
def test_snapshot_allocation_cannot_borrow_an_unverified_higher_quota(live, value):
    with pytest.raises(ValueError, match="allocation"):
        snapshot_pacer(live.lease, rate(value), clock=lambda: 0.0, sleep=lambda _: None)


def test_no_deployment_registry_or_cadence_scheduler_is_created():
    source = (ROOT / "src/sextant/tradingview/producer.py").read_text()
    assert "StockDataStream(" not in source and "StockHistoricalDataClient(" not in source
    assert "TradingClient(" not in source and "environ" not in source
    assert not (ROOT / "src/sextant/ranking/live.py").exists()
    assert not (ROOT / "src/sextant/tradingview/display_lists.py").exists()
    assert not (ROOT / "src/sextant/tradingview/display_support.py").exists()


# These witnesses catch a shared REST/stream scope, weakened exclusion/admission,
# or release before the producer has finished using its bound pacer.
def _reservation(kind, root):
    from sextant.tradingview import producer

    if kind == "rest":
        return producer.reserve_rest_display(root, paper_slot=2)
    return reserve_stream(root, paper_slot=2, existing_streams=0, source="fixture inventory")


def _process_reservation(root, kind, *, inventory=None):
    script = """
import json
from pathlib import Path
import socket
import sys
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.trading.client import TradingClient
from sextant.execution.custody import AccountScope, CustodyError, WriterLease
from sextant.tradingview import producer

def refuse(*args, **kwargs):
    raise AssertionError('reservation attempted client construction or network')

for client in (StockDataStream, StockHistoricalDataClient, TradingClient):
    client.__init__ = refuse
socket.socket.connect = refuse
socket.create_connection = refuse
root, kind, inventory = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
try:
    if kind == 'old':
        # The pre-migration producer's exact fixed scope and primitive at the base pin.
        lease = WriterLease(AccountScope('alpaca-market-data', 'paper-slot-2'), root, 'rank-live')
        lease.acquire()
        lease.close()
    else:
        if kind == 'rest':
            reservation = producer.reserve_rest_display(root, paper_slot=2)
        else:
            kwargs = {'existing_streams': 0, 'source': 'fixture inventory'}
            if inventory is not None:
                kwargs = inventory
            reservation = producer.reserve_stream(root, paper_slot=2, **kwargs)
        with reservation as lease:
            assert lease.held
    print('acquired')
except CustodyError:
    print('excluded')
except (ValueError, TypeError):
    print('invalid inventory')
"""
    done = subprocess.run(
        [sys.executable, "-c", script, str(root), kind, json.dumps(inventory)],
        capture_output=True,
        text=True,
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "src"), "HOME": str(root.parent)},
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("first,second", [("rest", "stream"), ("stream", "rest")])
def test_rest_and_one_stream_coexist_in_both_process_acquisition_orders(tmp_path, first, second):
    with _reservation(first, tmp_path) as lease:
        assert _process_reservation(tmp_path, second) == "acquired"
        assert lease.held
        assert _process_reservation(tmp_path, first) == "excluded"


@pytest.mark.parametrize("kind", ["rest", "stream"])
def test_each_scope_excludes_its_second_process_across_release_and_restart(tmp_path, kind):
    for _ in range(2):
        with _reservation(kind, tmp_path) as lease:
            assert _process_reservation(tmp_path, kind) == "excluded"
        assert not lease.held
        assert _process_reservation(tmp_path, kind) == "acquired"


def test_rest_display_seam_does_not_hold_the_stream_reservation(tmp_path):
    with _reservation("rest", tmp_path) as lease:
        display = LiveProducer(publisher(), lease, sip_authority="fixture entitlement")
        assert display.lease is lease
        assert _process_reservation(tmp_path, "stream") == "acquired"
        with _reservation("stream", tmp_path):
            assert _process_reservation(tmp_path, "stream") == "excluded"


@pytest.mark.parametrize(
    "inventory",
    [
        {"source": "fixture inventory"},
        {"existing_streams": None, "source": "fixture inventory"},
        {"existing_streams": "unknown", "source": "fixture inventory"},
        {"existing_streams": -1, "source": "fixture inventory"},
        {"existing_streams": True, "source": "fixture inventory"},
        {"existing_streams": 1, "source": "fixture inventory"},
        {"existing_streams": 0, "source": ""},
        {"existing_streams": 0, "source": " "},
        {"existing_streams": 0},
    ],
)
def test_rest_holder_does_not_replace_sourced_zero_stream_admission(tmp_path, inventory):
    with _reservation("rest", tmp_path):
        assert _process_reservation(tmp_path, "stream", inventory=inventory) == "invalid inventory"
        assert _process_reservation(tmp_path, "stream") == "acquired"


def test_rest_exception_releases_its_pacer_without_releasing_the_stream(tmp_path):
    with _reservation("stream", tmp_path) as stream:
        stream_pacer = object()
        stream.bind_pacer(stream_pacer)
        rest = None
        with pytest.raises(RuntimeError, match="fixture failure"):
            with _reservation("rest", tmp_path) as rest:
                rest.bind_pacer(object())
                with pytest.raises(CustodyError):
                    rest.bind_pacer(object())
                assert _process_reservation(tmp_path, "rest") == "excluded"
                raise RuntimeError("fixture failure")
        assert rest is not None and not rest.held and rest.pacer is None
        assert stream.held and stream.pacer is stream_pacer
        assert _process_reservation(tmp_path, "rest") == "acquired"
        assert _process_reservation(tmp_path, "stream") == "excluded"


@pytest.mark.parametrize("slot", [1, 3, 4, True, "2", None])
def test_rest_wrong_account_refuses_before_creating_the_lock_root(tmp_path, slot):
    from sextant.tradingview import producer

    root = tmp_path / "absent"
    with pytest.raises(ValueError):
        with producer.reserve_rest_display(root, paper_slot=slot):
            pytest.fail("wrong account admitted")
    assert not root.exists()


def test_old_and_new_producers_do_not_exclude_each_other_during_migration(tmp_path):
    # A deployed old producer uses the unchanged stream scope. Migration therefore
    # requires an attended stop/release before the new producer is started.
    with _reservation("rest", tmp_path):
        assert _process_reservation(tmp_path, "old") == "acquired"
    with _reservation("stream", tmp_path):
        assert _process_reservation(tmp_path, "rest") == "acquired"

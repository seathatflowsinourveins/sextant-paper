"""TV-LOOP: one owner-consented process, one display sync per J5 admission.

The loop shape follows northstar-trading@8256c16c
src/northstar_trading/tradingview/__main__.py:163-390: one consent context, each
cycle rereads the newest complete unit, and a refused input leaves the lists alone.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx2
import pytest
from mcp import Client
from test_tradingview import (
    FAKE_REFRESH,
    FRESH_ACCESS,
    FRESH_REFRESH,
    NOW,
    ROTATED_ACCESS,
    ROTATED_REFRESH,
    MemoryLogin,
    Peer,
    answer,
    assert_no_secret,
    change,
    literal,
    oauth_service,
    stale_login,
    token_body,
    unit,
    wired_session,
)

from sextant.tradingview import sync as sync_module
from sextant.tradingview.auth import sensitive_logging
from sextant.tradingview.cadence import Cadence
from sextant.tradingview.inputs import _ns, read_research_inputs
from sextant.tradingview.sync import OfficialTools, SyncError

ROOT = Path(__file__).parents[2]
REGISTRY = ROOT / "lanes/tradingview/j5-cadence.params.json"
NO_USAGE = "synthetic: no Windows Claude surface in this process"


def loop():
    try:
        return importlib.import_module("sextant.tradingview.loop")
    except ModuleNotFoundError:
        pytest.fail("The owner-consented TradingView display loop is not implemented")


def registry(tmp_path: Path, *, enabled: bool = True) -> Path:
    raw = json.loads(REGISTRY.read_text())
    for row in raw["rows"]:
        if row["name"] == "enabled":
            row.update(value=int(enabled), source="synthetic TV-LOOP enablement row")
    path = tmp_path / "fixture-params.json"
    path.write_text(json.dumps(raw))
    return path


def cadence(tmp_path: Path, *, baseline, pages=None, enabled: bool = True) -> Cadence:
    return Cadence(
        tmp_path / "state",
        registry(tmp_path, enabled=enabled),
        page=pages.append if pages is not None else (lambda _: None),
        landed_sha="f" * 40,
        enable_source="synthetic lead enables after landing",
        initial_membership=baseline,
        initial_source="synthetic complete by-ID read-back",
    )


class Clock:
    def __init__(self, at: datetime = NOW):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def sleeper(clock: Clock, delays: list[float]):
    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        clock.advance(seconds)

    return sleep


def readback(movers=("NASDAQ:AAPL", "NASDAQ:MSFT"), focus=("NASDAQ:MSFT",)):
    return {
        "NS movers": {
            "stored": sorted(movers),
            "rejected": [],
            "corrective_removals": [],
        },
        "NS focus": {"stored": sorted(focus), "rejected": [], "corrective_removals": []},
    }


def applied(*, readback_doc=None, operations: int = 1):
    return {
        "applied": True,
        "operations": [{"tool": "add_to_watchlist", "arguments": {}}] * operations,
        "readback": readback_doc if readback_doc is not None else readback(),
        "unmapped": [],
    }


def state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state" / "j5-cadence.json").read_text())


def serve(tmp_path, module, directory, gate, *, clock, delays, pages, sync, cycles):
    return asyncio.run(
        module.serve(
            tools=object(),
            cadence=gate,
            research_dir=directory,
            max_age=1200,
            clock=clock,
            sleep=sleeper(clock, delays),
            page=pages.append,
            weekly_usage_pct=Decimal(0),
            usage_source=NO_USAGE,
            sync=sync,
            cycles=cycles,
        )
    )


def test_loop_admission_view_carries_both_lists_and_the_generation_clocks(tmp_path):
    module = loop()
    inputs = read_research_inputs(unit(tmp_path / "unit"), now=NOW)
    doc = module._admission(inputs)
    assert doc["schema"] == "sextant.display-admission/1"
    assert doc["status"] == "fresh" and doc["display_only"] is True
    assert doc["paper_slot"] == 2
    assert doc["generated_at_ns"] == _ns("2026-11-06T14:29:00+00:00")
    assert doc["expires_at_ns"] == _ns(inputs.expires_at)
    assert doc["watchlists"] == {
        "NS movers": ["NASDAQ:AAPL", "NASDAQ:MSFT"],
        "NS focus": ["NASDAQ:MSFT"],
    }


def test_loop_syncs_a_changed_pair_once_and_finishes_success(tmp_path):
    module = loop()
    directory = unit(tmp_path / "unit")
    clock, delays, pages, syncs = Clock(), [], [], []
    gate = cadence(
        tmp_path,
        baseline={"NS movers": ["NASDAQ:MSFT"], "NS focus": []},
        pages=pages,
    )

    async def sync(tools, inputs, *, apply):
        syncs.append(apply)
        return applied()

    result = serve(
        tmp_path,
        module,
        directory,
        gate,
        clock=clock,
        delays=delays,
        pages=pages,
        sync=sync,
        cycles=2,
    )
    assert result == 0
    assert syncs == [True]
    assert delays == [150]
    assert pages == []
    stored = state(tmp_path)
    assert stored["active"] is None and stored["failed"] is False
    assert stored["confirmed"] == {
        "NS movers": ["NASDAQ:AAPL", "NASDAQ:MSFT"],
        "NS focus": ["NASDAQ:MSFT"],
    }
    assert stored["first_session_j5_run_time_ns"] == 0
    assert stored["run_receipt_source"].startswith("sync_research applied 1 operations")


def test_loop_does_not_sync_an_unchanged_pair(tmp_path):
    module = loop()
    directory = unit(tmp_path / "unit")
    clock, delays, pages, syncs = Clock(), [], [], []
    gate = cadence(
        tmp_path,
        baseline={"NS movers": ["NASDAQ:AAPL", "NASDAQ:MSFT"], "NS focus": ["NASDAQ:MSFT"]},
        pages=pages,
    )

    async def sync(tools, inputs, *, apply):
        syncs.append(apply)
        return applied()

    result = serve(
        tmp_path,
        module,
        directory,
        gate,
        clock=clock,
        delays=delays,
        pages=pages,
        sync=sync,
        cycles=1,
    )
    assert result == 0
    assert syncs == [] and delays == [] and pages == []
    assert not (tmp_path / "state" / "j5-cadence.json").exists()


def test_default_page_reaches_the_owner_inside_the_consent_session(tmp_path, capsys):
    module = loop()
    directory = unit(tmp_path / "unit")
    change(directory, "top100.json", status="stale")
    clock, delays = Clock(), []
    gate = cadence(tmp_path, baseline={"NS movers": [], "NS focus": []})

    async def sync(tools, inputs, *, apply):
        raise AssertionError("a stale unit never syncs")

    with sensitive_logging():
        result = asyncio.run(
            module.serve(
                tools=object(),
                cadence=gate,
                research_dir=directory,
                max_age=1200,
                clock=clock,
                sleep=sleeper(clock, delays),
                page=module._page,
                weekly_usage_pct=Decimal(0),
                usage_source=NO_USAGE,
                sync=sync,
                cycles=1,
            )
        )
    assert result == 0
    assert delays == []
    assert "lists unchanged" in capsys.readouterr().err


def test_loop_pages_once_for_a_stale_unit_and_leaves_the_lists(tmp_path):
    module = loop()
    directory = unit(tmp_path / "unit")
    change(directory, "top100.json", status="stale")
    clock, delays, pages, syncs = Clock(), [], [], []
    gate = cadence(tmp_path, baseline={"NS movers": [], "NS focus": []}, pages=pages)

    async def sync(tools, inputs, *, apply):
        syncs.append(apply)
        return applied()

    result = serve(
        tmp_path,
        module,
        directory,
        gate,
        clock=clock,
        delays=delays,
        pages=pages,
        sync=sync,
        cycles=2,
    )
    assert result == 0
    assert syncs == [] and delays == [150]
    assert len(pages) == 1 and "lists unchanged" in pages[0]
    assert not (tmp_path / "state" / "j5-cadence.json").exists()


def test_loop_latches_a_failed_sync_and_stops(tmp_path, capsys):
    module = loop()
    directory = unit(tmp_path / "unit")
    clock, delays, pages = Clock(), [], []
    gate = cadence(
        tmp_path,
        baseline={"NS movers": ["NASDAQ:MSFT"], "NS focus": []},
        pages=pages,
    )

    async def sync(tools, inputs, *, apply):
        raise SyncError("fixture MCP refused or interrupted")

    result = serve(
        tmp_path,
        module,
        directory,
        gate,
        clock=clock,
        delays=delays,
        pages=pages,
        sync=sync,
        cycles=5,
    )
    assert result == 2 and delays == []
    assert len(pages) == 1
    assert "Display sync stopped" in capsys.readouterr().out
    stored = state(tmp_path)
    assert stored["active"] is None and stored["failed"] is True
    assert "sync failed at" in stored["run_receipt_source"]
    assert "fixture MCP refused" in stored["run_receipt_source"]


def test_loop_latches_an_unverified_apply_result(tmp_path):
    module = loop()
    directory = unit(tmp_path / "unit")
    clock, delays, pages = Clock(), [], []
    gate = cadence(
        tmp_path,
        baseline={"NS movers": ["NASDAQ:MSFT"], "NS focus": []},
        pages=pages,
    )

    async def sync(tools, inputs, *, apply):
        return {"applied": False, "operations": [], "readback": {}, "unmapped": []}

    result = serve(
        tmp_path,
        module,
        directory,
        gate,
        clock=clock,
        delays=delays,
        pages=pages,
        sync=sync,
        cycles=5,
    )
    assert result == 2 and len(pages) == 1
    stored = state(tmp_path)
    assert stored["failed"] is True and stored["active"] is None
    assert "apply returned no verified read-back" in stored["run_receipt_source"]


def test_loop_exits_at_the_display_window_close(tmp_path, capsys):
    module = loop()
    directory = unit(tmp_path / "unit")
    clock = Clock(datetime.fromisoformat("2026-11-06T20:00:00-05:00"))
    delays, pages, syncs = [], [], []
    gate = cadence(tmp_path, baseline={"NS movers": [], "NS focus": []}, pages=pages)

    async def sync(tools, inputs, *, apply):
        syncs.append(apply)
        return applied()

    result = serve(
        tmp_path,
        module,
        directory,
        gate,
        clock=clock,
        delays=delays,
        pages=pages,
        sync=sync,
        cycles=1,
    )
    assert result == 0
    assert syncs == [] and delays == [] and pages == []
    assert "window is closed" in capsys.readouterr().out


def test_loop_waits_for_the_0400_display_open(tmp_path):
    module = loop()
    gate = cadence(tmp_path, baseline={"NS movers": [], "NS focus": []})
    early = datetime.fromisoformat("2026-11-06T03:00:00-05:00")
    opened = datetime.fromisoformat("2026-11-06T04:00:00-05:00")
    assert module._pre_open(early, gate.params) == opened
    saturday = datetime.fromisoformat("2026-11-07T03:00:00-05:00")
    assert module._pre_open(saturday, gate.params) is None
    after_close = datetime.fromisoformat("2026-11-06T20:30:00-05:00")
    assert module._pre_open(after_close, gate.params) is None


def test_run_loop_refuses_a_disabled_or_unlanded_registry_without_a_session(tmp_path):
    module = loop()
    directory = unit(tmp_path / "unit")
    opened = []

    @asynccontextmanager
    async def client(authorize):
        opened.append(authorize)
        yield object()

    async def read_current(tools):
        return {"NS movers": frozenset(), "NS focus": frozenset()}

    disabled = registry(tmp_path, enabled=False)
    assert (
        module.run_loop(
            research_dir=directory,
            authorize=True,
            registry=disabled,
            state_root=tmp_path / "state",
            landed_sha="f" * 40,
            open_client=client,
            read_current=read_current,
        )
        == 2
    )
    assert opened == []
    enabled = registry(tmp_path, enabled=True)
    assert (
        module.run_loop(
            research_dir=directory,
            authorize=True,
            registry=enabled,
            state_root=tmp_path / "state",
            landed_sha="",
            open_client=client,
            read_current=read_current,
        )
        == 2
    )
    assert opened == []


def test_run_loop_seeds_the_initial_membership_from_the_by_id_readback(tmp_path):
    module = loop()
    directory = unit(tmp_path / "unit")
    clock, delays, pages, syncs = Clock(), [], [], []
    opened = []

    @asynccontextmanager
    async def client(authorize):
        opened.append(authorize)
        yield object()

    async def read_current(tools):
        return {"NS movers": frozenset({"NASDAQ:MSFT"}), "NS focus": frozenset()}

    async def sync(tools, inputs, *, apply):
        syncs.append(apply)
        return applied()

    result = module.run_loop(
        research_dir=directory,
        authorize=True,
        registry=registry(tmp_path),
        state_root=tmp_path / "state",
        landed_sha="f" * 40,
        clock=clock,
        sleep=sleeper(clock, delays),
        page=pages.append,
        weekly_usage_pct=Decimal(0),
        usage_source=NO_USAGE,
        open_client=client,
        read_current=read_current,
        sync=sync,
        cycles=1,
    )
    assert result == 0
    assert opened == [True] and syncs == [True]
    stored = state(tmp_path)
    assert stored["confirmed"] == {
        "NS movers": ["NASDAQ:AAPL", "NASDAQ:MSFT"],
        "NS focus": ["NASDAQ:MSFT"],
    }


def test_restart_compares_against_the_startup_readback_not_the_saved_pair(tmp_path):
    module = loop()
    directory = unit(tmp_path / "unit")
    clock, delays, pages, syncs = Clock(NOW + timedelta(seconds=151)), [], [], []
    opened = []

    saved = cadence(tmp_path, baseline={"NS movers": [], "NS focus": []})
    stored = saved.claim(
        module._admission(read_research_inputs(directory, now=NOW)),
        now=NOW,
        weekly_usage_pct=Decimal(0),
        usage_source=NO_USAGE,
    )
    assert stored is not None
    saved.finish(stored["run_id"], ended_at=NOW, successful=True, receipt_source="synthetic")
    assert state(tmp_path)["confirmed"] == {
        "NS movers": ["NASDAQ:AAPL", "NASDAQ:MSFT"],
        "NS focus": ["NASDAQ:MSFT"],
    }

    @asynccontextmanager
    async def client(authorize):
        opened.append(authorize)
        yield object()

    async def read_current(tools):
        # The broker lost AAPL after the saved session; the fresh unit still wants it.
        return {"NS movers": frozenset({"NASDAQ:MSFT"}), "NS focus": frozenset()}

    async def sync(tools, inputs, *, apply):
        syncs.append(apply)
        return applied()

    result = module.run_loop(
        research_dir=directory,
        authorize=True,
        registry=registry(tmp_path),
        state_root=tmp_path / "state",
        landed_sha="f" * 40,
        clock=clock,
        sleep=sleeper(clock, delays),
        page=pages.append,
        weekly_usage_pct=Decimal(0),
        usage_source=NO_USAGE,
        open_client=client,
        read_current=read_current,
        sync=sync,
        cycles=1,
    )
    assert result == 0
    assert opened == [True]
    assert syncs == [True]


def test_read_memberships_reads_the_complete_by_id_inventory():
    peer = Peer()

    async def members():
        async with Client(peer.server) as client:
            tools = OfficialTools(client, interval=0, now=lambda: NOW)
            return await sync_module.read_memberships(tools)

    assert asyncio.run(members()) == {
        "NS movers": frozenset({"NASDAQ:MSFT", "NYSE:IBM"}),
        "NS focus": frozenset(),
    }
    del peer.lists["2"]
    assert asyncio.run(members())["NS focus"] == frozenset()
    peer.lists["4"] = {"id": "4", "name": "NS movers", "symbols": []}

    async def duplicate():
        error = None
        async with Client(peer.server) as client:
            tools = OfficialTools(client, interval=0, now=lambda: NOW)
            # SDK task groups wrap errors raised through __aexit__.
            try:
                await sync_module.read_memberships(tools)
            except sync_module.SyncError as exc:
                error = exc
        if error:
            raise error

    with pytest.raises(sync_module.SyncError, match="Duplicate reserved watchlist name"):
        asyncio.run(duplicate())


def test_run_loop_syncs_the_peer_through_the_real_sdk_once(tmp_path):
    module = loop()
    peer = Peer()
    directory = unit(tmp_path / "unit")
    clock, delays, pages = Clock(), [], []

    result = module.run_loop(
        research_dir=directory,
        authorize=True,
        registry=registry(tmp_path),
        state_root=tmp_path / "state",
        landed_sha="f" * 40,
        clock=clock,
        sleep=sleeper(clock, delays),
        page=pages.append,
        weekly_usage_pct=Decimal(0),
        usage_source=NO_USAGE,
        open_client=lambda authorize: Client(peer.server),
        cycles=1,
    )

    assert result == 0
    assert pages == []
    assert peer.lists["1"]["symbols"] == ["NASDAQ:MSFT", "NASDAQ:AAPL"]
    assert peer.lists["2"]["symbols"] == ["NASDAQ:MSFT"]
    assert delays == []
    assert state(tmp_path)["confirmed"] == {
        "NS movers": ["NASDAQ:AAPL", "NASDAQ:MSFT"],
        "NS focus": ["NASDAQ:MSFT"],
    }


def _latched(tmp_path, directory, *, successful: bool):
    path = registry(tmp_path)
    gate = Cadence(
        tmp_path / "state",
        path,
        page=lambda _: None,
        landed_sha="f" * 40,
        enable_source="synthetic lead enables after landing",
        initial_membership={"NS movers": [], "NS focus": []},
        initial_source="synthetic complete by-ID read-back",
    )
    run = gate.claim(
        loop()._admission(read_research_inputs(directory, now=NOW)),
        now=NOW,
        weekly_usage_pct=Decimal(0),
        usage_source=NO_USAGE,
    )
    assert run is not None
    gate.finish(
        run["run_id"],
        ended_at=NOW + timedelta(seconds=1),
        successful=successful,
        receipt_source="synthetic completion receipt",
    )
    return path


def test_run_loop_refuses_a_latched_run_without_opening_a_session(tmp_path, capsys):
    module = loop()
    directory = unit(tmp_path / "unit")
    path = _latched(tmp_path, directory, successful=False)
    opened, reads = [], []

    @asynccontextmanager
    async def client(authorize):
        opened.append(authorize)
        yield object()

    async def read_current(tools):
        reads.append(True)
        return {"NS movers": frozenset(), "NS focus": frozenset()}

    result = module.run_loop(
        research_dir=directory,
        authorize=True,
        registry=path,
        state_root=tmp_path / "state",
        landed_sha="f" * 40,
        clock=Clock(),
        open_client=client,
        read_current=read_current,
        cycles=1,
    )
    assert result == 2
    assert opened == [] and reads == []
    assert "reconciliation" in capsys.readouterr().out


def test_run_loop_refuses_state_from_another_registry_without_a_session(tmp_path, capsys):
    module = loop()
    directory = unit(tmp_path / "unit")
    path = _latched(tmp_path, directory, successful=True)
    raw = json.loads(path.read_text())
    for row in raw["rows"]:
        if row["name"] == "enabled":
            row["source"] += " (reviewed change)"
    path.write_text(json.dumps(raw))
    opened = []

    @asynccontextmanager
    async def client(authorize):
        opened.append(authorize)
        yield object()

    async def read_current(tools):
        raise AssertionError("a changed registry must refuse before the read-back")

    result = module.run_loop(
        research_dir=directory,
        authorize=True,
        registry=path,
        state_root=tmp_path / "state",
        landed_sha="f" * 40,
        clock=Clock(),
        open_client=client,
        read_current=read_current,
        cycles=1,
    )
    assert result == 2
    assert opened == []
    assert "migration" in capsys.readouterr().out


def test_loop_cli_requires_a_research_directory(capsys):
    cli = importlib.import_module("sextant.tradingview.__main__")
    assert cli.main(["--loop"]) == 2
    assert "--research-dir" in capsys.readouterr().out


def test_run_loop_stop_prints_the_same_safe_diagnostic_as_one_cycle(tmp_path, capsys):
    module = loop()
    directory = unit(tmp_path / "unit")

    @asynccontextmanager
    async def client(authorize):
        raise SyncError(
            "TradingView session stopped; re-inventory before retry.",
            label="OAuthTokenError",
            stage="token",
            detail="HTTP 400 at token endpoint",
        )
        yield  # pragma: no cover - the raise above refuses the consent session

    result = module.run_loop(
        research_dir=directory,
        authorize=True,
        registry=registry(tmp_path),
        state_root=tmp_path / "state",
        landed_sha="f" * 40,
        open_client=client,
    )

    assert result == 2
    out = capsys.readouterr().out
    assert out == (
        f"{module._STOP} Safe diagnostic: OAuthTokenError at token (HTTP 400 at token endpoint).\n"
    )
    assert "TradingView session stopped" not in out


# Addition 7 (issue #470 row 14): a scheduled loop may use the stored login, never a browser.
class SdkClock:
    """The pinned SDK reads ``time.time()`` for token expiry (mcp/client/auth/oauth2.py:186-192,
    mcp/shared/auth_utils.py:68-80); the loop's own clock is the separate ``Clock``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        import mcp.client.auth.oauth2 as oauth2
        import mcp.shared.auth_utils as auth_utils

        self.now = time.time()
        fake = SimpleNamespace(time=lambda: self.now)
        monkeypatch.setattr(oauth2, "time", fake)
        monkeypatch.setattr(auth_utils, "time", fake)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def stored_loop(tmp_path, monkeypatch, *, login, refresh, sync):
    """run_loop through the real official_client with the stored login and no --authorize."""
    auth = wired_session(monkeypatch, oauth_service(login.events, refresh=refresh), login.events)
    clock, delays, pages = Clock(), [], []

    async def read_current(tools):
        return {"NS movers": frozenset(), "NS focus": frozenset()}

    result = loop().run_loop(
        research_dir=unit(tmp_path / "unit"),
        authorize=False,
        login=login,
        registry=registry(tmp_path),
        state_root=tmp_path / "state",
        landed_sha="f" * 40,
        clock=clock,
        sleep=sleeper(clock, delays),
        page=pages.append,
        weekly_usage_pct=Decimal(0),
        usage_source=NO_USAGE,
        open_client=auth.official_client,
        read_current=read_current,
        sync=sync,
        cycles=1,
    )
    return result, pages


def test_the_loop_uses_the_stored_login_and_writes_each_rotation_back(tmp_path, monkeypatch, capfd):
    sdk = SdkClock(monkeypatch)
    events: list[str] = []
    login = stale_login(events)
    rotations = iter([(ROTATED_ACCESS, ROTATED_REFRESH), (FRESH_ACCESS, FRESH_REFRESH)])
    presented: list[str] = []
    between: list[str] = []

    def refresh(request):
        presented.append(parse_qs(request.content.decode())["refresh_token"][0])
        access, token = next(rotations)
        return httpx2.Response(200, json=token_body(access, token, expires_in=600))

    async def sync(tools, inputs, *, apply):
        assert login.document is not None
        between.append(login.document.tokens.refresh_token)
        sdk.advance(3600)  # the session's access token is now past its lifetime
        await tools.client.request()
        return applied()

    result, pages = stored_loop(tmp_path, monkeypatch, login=login, refresh=refresh, sync=sync)
    assert result == 0
    assert pages == []
    assert "consent" not in events
    # The startup refresh's rotation was in the store before the tick refreshed with it.
    assert presented == [FAKE_REFRESH, ROTATED_REFRESH]
    assert between == [ROTATED_REFRESH]
    assert login.document is not None
    assert login.document.tokens.refresh_token == FRESH_REFRESH
    out, err = capfd.readouterr()
    assert_no_secret(out + err, tmp_path)  # includes the cadence state and its receipts


@pytest.mark.parametrize("case", ["missing", "refused", "outage"])
def test_the_loop_stops_with_the_owner_step_and_pages(tmp_path, monkeypatch, capfd, case):
    sdk = SdkClock(monkeypatch)
    events: list[str] = []
    login = MemoryLogin(events) if case == "missing" else stale_login(events)
    queue = {
        "missing": [],
        "refused": [answer(400, {"error": "invalid_grant"})],
        "outage": [answer(200, token_body(ROTATED_ACCESS, ROTATED_REFRESH, 600)), answer(503, {})],
    }[case]

    def refresh(request):
        return queue.pop(0)(request)

    async def sync(tools, inputs, *, apply):
        sdk.advance(3600)
        await tools.client.request()
        return applied()

    result, pages = stored_loop(tmp_path, monkeypatch, login=login, refresh=refresh, sync=sync)
    expected = literal(
        {
            "missing": "LOGIN_NEEDS_CONSENT",
            "refused": "LOGIN_REFUSED",
            "outage": "LOGIN_UNAVAILABLE",
        }[case]
    )
    out, err = capfd.readouterr()
    assert result == 2
    assert expected in pages
    assert expected in out
    assert "consent" not in events
    if case == "outage":
        # The outage changed nothing in the store; the admitted run is latched as before.
        assert login.document is not None
        assert login.document.tokens.refresh_token == ROTATED_REFRESH
        assert state(tmp_path)["failed"] is True
    else:
        assert login.document is None
    assert_no_secret(out + err, tmp_path)


def test_loop_cli_passes_the_stored_login_and_keeps_authorize_unchanged(tmp_path, monkeypatch):
    cli = importlib.import_module("sextant.tradingview.__main__")
    seen: list[dict] = []

    class Login:
        pass

    def run_loop(**options):
        seen.append(options)
        return 0

    monkeypatch.setattr(cli, "StoredLogin", Login)
    monkeypatch.setattr(cli, "run_loop", run_loop)
    assert cli.main(["--loop", "--keep-login", "--research-dir", str(tmp_path)]) == 0
    assert cli.main(["--loop", "--authorize", "--research-dir", str(tmp_path)]) == 0
    assert [entry["authorize"] for entry in seen] == [False, True]
    assert isinstance(seen[0]["login"], Login)
    assert seen[1]["login"] is None

"""One owner-consented display sync process, admitted once per J5 cadence tick.

The shape follows northstar-trading@8256c16c
src/northstar_trading/tradingview/__main__.py:163-390 (one consent context, each
cycle rereads the newest unit, a refused input leaves the lists alone) and
src/northstar_trading/tradingview/research.py:725-760 (strictly future schedule
slots, no catch-up). Floors and the 04:00-20:00 ET window come from the reviewed
lanes/tradingview/j5-cadence.params.json, never from this module.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sextant.tradingview.auth import official_client
from sextant.tradingview.cadence import Cadence
from sextant.tradingview.inputs import (
    DISPLAY_PAPER_SLOT,
    ET,
    NAMES,
    InputError,
    ResearchInputs,
    _calendar,
    _ns,
    read_research_inputs,
)
from sextant.tradingview.stored_login import StoredLogin
from sextant.tradingview.sync import (
    LOGIN_STOPS,
    OfficialTools,
    SyncError,
    read_memberships,
    sync_research,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY = ROOT / "lanes/tradingview/j5-cadence.params.json"
DEFAULT_STATE_ROOT = Path.home() / "sextant-data" / "tradingview-j5"
NO_CLAUDE_USAGE = (
    "no Windows Claude surface in this process; cloud MCP sync consumes no subscription usage"
)
_STOP = (
    "Display sync stopped. Re-inventory before retry; earlier writes may have succeeded. "
    "Owner browser consent is required unless a stored sign-in is kept (--authorize and "
    "--keep-login); the default saves nothing."
)


def _page(reason: str) -> None:
    """One page per episode on a channel the consent session cannot silence.

    auth.sensitive_logging() disables logging at CRITICAL around the owner
    consent session, so an ERROR record would be dropped. stderr is written
    directly, as northstar-trading@8256c16c __main__.py:328 prints a refused unit.
    """
    print(reason, file=sys.stderr, flush=True)


def _registry_receipt(path: Path) -> tuple[bool, str]:
    raw = json.loads(path.read_text())
    rows = raw["rows"]
    if raw["schema"] != "j5-cadence.registry/1" or not isinstance(rows, list):
        raise ValueError("unknown cadence registry")
    enabled = [row for row in rows if isinstance(row, dict) and row.get("name") == "enabled"]
    if len(enabled) != 1:
        raise ValueError("one enabled row required")
    row = enabled[0]
    if type(row.get("value")) is not int or row["value"] not in {0, 1}:
        raise ValueError("enabled flag required")
    source = row.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("sourced enable row required")
    return bool(row["value"]), source


def _landed_sha(root: Path) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else ""


def _pre_open(now: datetime, params: Mapping[str, int]) -> datetime | None:
    """Today's display open when the clock is before it on an XNYS session day."""
    local = now.astimezone(ET)
    start = params["display_start_minute_et"]
    if local.hour * 60 + local.minute >= start:
        return None
    try:
        if not _calendar(local.year).is_session(local.date()):
            return None
    except InputError:
        return None
    return local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)


def _admission(inputs: ResearchInputs) -> dict:
    """Explicit two-list admission view of one complete display unit."""
    return {
        "schema": "sextant.display-admission/1",
        "status": "fresh",
        "display_only": True,
        "paper_slot": DISPLAY_PAPER_SLOT,
        "generated_at_ns": _ns(inputs.generation["as_of"]),
        "expires_at_ns": _ns(inputs.expires_at),
        "watchlists": {name: sorted(inputs.memberships[name]) for name in NAMES},
    }


def _state_refusal(cadence: Cadence) -> bool:
    """Refuse existing state that needs a reviewed migration or reconciliation."""
    try:
        saved = json.loads(cadence.state_path.read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        print("J5 cadence state is unreadable; reviewed reconciliation is required.")
        return True
    if saved.get("params_hash") != cadence.params_hash:
        print(
            "J5 cadence state came from another reviewed registry; a reviewed "
            "migration is required before the loop starts."
        )
        return True
    if (
        saved.get("active") is not None
        or saved.get("failed") is True
        or saved.get("page_pending") is True
    ):
        print(
            "J5 cadence state holds an unresolved run or page; finish the "
            "reviewed reconciliation before restarting."
        )
        return True
    return False


async def serve(
    *,
    tools: OfficialTools,
    cadence: Cadence,
    research_dir: Path,
    max_age: int,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    page: Callable[[str], None],
    weekly_usage_pct: Decimal,
    usage_source: str,
    sync: Callable[..., Awaitable[dict]] = sync_research,
    cycles: int | None = None,
) -> int:
    """Reread, admit, sync and sleep; a refused unit never becomes an empty list."""
    ticks = 0
    last_refusal: str | None = None
    while True:
        now = clock()
        floor = cadence.floor(now, weekly_usage_pct)
        if floor is None:
            opens = _pre_open(now, cadence.params)
            if opens is None:
                print(
                    "J5 display window is closed; start a new owner-consented process "
                    "at the next 04:00 ET session open."
                )
                return 0
            await sleep(max(0.0, (opens - now).total_seconds()))
            continue
        ticks += 1
        try:
            inputs = read_research_inputs(research_dir, now=now, max_age=max_age)
        except InputError as error:
            reason = str(error)
            if reason != last_refusal:
                page(f"J5 display sync skipped: {reason}; lists unchanged.")
                last_refusal = reason
        else:
            last_refusal = None
            run = cadence.claim(
                _admission(inputs),
                now=now,
                weekly_usage_pct=weekly_usage_pct,
                usage_source=usage_source,
            )
            if run is not None:
                try:
                    result = await sync(tools, inputs, apply=True)
                    if (
                        not isinstance(result, dict)
                        or result.get("applied") is not True
                        or set(result.get("readback") or ()) != set(NAMES)
                    ):
                        raise SyncError("apply returned no verified read-back")
                except Exception as error:
                    ended = clock()
                    detail = str(error) if isinstance(error, SyncError) else type(error).__name__
                    receipt = f"sync failed at {ended.isoformat()}: {detail}"[:500]
                    cadence.finish(
                        run["run_id"],
                        ended_at=ended,
                        successful=False,
                        receipt_source=receipt,
                    )
                    page(
                        "J5 display sync failed after admission; the run is latched "
                        "for reviewed reconciliation."
                    )
                    print(_STOP)
                    return 2
                ended = clock()
                stored = result["readback"]
                receipt = (
                    f"sync_research applied {len(result['operations'])} operations; "
                    f"by-ID read-back NS movers={len(stored['NS movers']['stored'])}, "
                    f"NS focus={len(stored['NS focus']['stored'])} at {ended.isoformat()}"
                )
                try:
                    cadence.finish(
                        run["run_id"],
                        ended_at=ended,
                        successful=True,
                        receipt_source=receipt,
                    )
                except ValueError:
                    page(
                        "J5 sync completed but its completion receipt was refused; "
                        "the active run is latched for reviewed reconciliation."
                    )
                    print(json.dumps(result, ensure_ascii=False))
                    return 2
                print(json.dumps(result, ensure_ascii=False))
        if cycles is not None and ticks >= cycles:
            return 0
        delay = cadence.floor(clock(), weekly_usage_pct)
        if delay is not None:
            await sleep(delay)


def run_loop(
    *,
    research_dir: Path,
    authorize: bool,
    login: StoredLogin | None = None,
    registry: Path = DEFAULT_REGISTRY,
    state_root: Path = DEFAULT_STATE_ROOT,
    max_age: int = 1200,
    weekly_usage_pct: Decimal = Decimal(0),
    usage_source: str = NO_CLAUDE_USAGE,
    landed_sha: str | None = None,
    enable_source: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    page: Callable[[str], None] = _page,
    open_client: Callable[..., Any] = official_client,
    read_current: Callable[..., Awaitable[dict[str, frozenset[str]]]] = read_memberships,
    sync: Callable[..., Awaitable[dict]] = sync_research,
    cycles: int | None = None,
) -> int:
    """Preflight without a session, then own exactly one consent context.

    With ``login`` and no ``authorize`` the context is the stored sign-in: the SDK
    refreshes it in-process as ticks need and writes each rotation back to the store;
    it never opens a browser, and a stored-login stop pages its owner step.
    """
    try:
        enabled, enabled_source = _registry_receipt(registry)
    except (OSError, ValueError, KeyError, TypeError):
        print("J5 cadence registry unavailable or invalid; no loop started.")
        return 2
    if not enabled:
        print("J5 display loop is disabled in the reviewed registry; no loop started.")
        return 2
    sha = landed_sha if landed_sha is not None else _landed_sha(ROOT)
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        print("J5 display loop requires the landed commit SHA; no run admitted.")
        return 2
    source = enable_source if enable_source is not None else enabled_source
    try:
        preflight = Cadence(state_root, registry, page=page, landed_sha=sha, enable_source=source)
    except (OSError, ValueError):
        print("J5 cadence registry unavailable or invalid; no loop started.")
        return 2
    if _state_refusal(preflight):
        return 2

    async def run() -> int:
        # Without the store the call is today's, so --loop --authorize is unchanged.
        opened = open_client(authorize) if login is None else open_client(authorize, login)
        async with opened as client:
            tools = OfficialTools(client, now=clock)
            try:
                current = await read_current(tools)
                if set(current) != set(NAMES) or any(
                    not isinstance(value, frozenset) for value in current.values()
                ):
                    raise ValueError("complete by-ID read-back required")
            except Exception:
                # Refuse before the first compare: unknown inventory never seeds a clear.
                print(_STOP)
                return 2
            gate = Cadence(
                state_root,
                registry,
                page=page,
                landed_sha=sha,
                enable_source=source,
                initial_membership={name: sorted(current[name]) for name in NAMES},
                initial_source=f"complete by-ID read-back at {clock().isoformat()}",
            )
            if _state_refusal(gate):
                return 2
            try:
                gate.reconcile(
                    {name: sorted(current[name]) for name in NAMES},
                    source=f"startup by-ID read-back at {clock().isoformat()}",
                )
            except (OSError, ValueError):
                print(
                    "J5 cadence state could not reconcile with the startup read-back; "
                    "a reviewed reconciliation is required."
                )
                return 2
            return await serve(
                tools=tools,
                cadence=gate,
                research_dir=research_dir,
                max_age=max_age,
                clock=clock,
                sleep=sleep,
                page=page,
                weekly_usage_pct=weekly_usage_pct,
                usage_source=usage_source,
                sync=sync,
                cycles=cycles,
            )

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print(
            "J5 display loop interrupted; any active admitted run stays latched "
            "for reviewed reconciliation."
        )
        return 130
    except Exception as error:
        safe = f" Safe diagnostic: {error.stop_tail()}." if isinstance(error, SyncError) else ""
        if isinstance(error, SyncError) and str(error) in LOGIN_STOPS:
            page(str(error))
        print(f"{_STOP}{safe}")
        return 2

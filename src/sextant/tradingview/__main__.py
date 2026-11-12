"""Owner-run display sync: one cycle, the owner-consented loop, or a preset receipt."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sextant.tradingview.auth import official_client
from sextant.tradingview.inputs import InputError, read_research_inputs
from sextant.tradingview.loop import (
    DEFAULT_REGISTRY,
    DEFAULT_STATE_ROOT,
    NO_CLAUDE_USAGE,
    _page,
    run_loop,
)
from sextant.tradingview.presets import preset_receipt
from sextant.tradingview.stored_login import StoredLogin, StoredLoginError
from sextant.tradingview.sync import (
    LOGIN_STOPS,
    LOGIN_STORE_UNAVAILABLE,
    OfficialTools,
    SyncError,
    sync_research,
)

STOP = (
    "Display sync stopped. Re-inventory before retry; earlier writes may have succeeded. "
    "Owner browser consent is required unless a stored sign-in is kept (--authorize and "
    "--keep-login); the default saves nothing."
)
SIGNED_IN = "TradingView sign-in kept in the encrypted store; no list was read or changed."
SIGN_IN_NEEDS_KEEP_LOGIN = (
    "A sign-in-only run (no --research-dir) must keep the sign-in: run --authorize "
    "--keep-login. No session opened."
)
SIGN_IN_CHANGES_NO_LIST = (
    "A sign-in-only run reads and changes no list; --apply needs --research-dir. No session opened."
)


def _stopped(error: SyncError) -> int:
    """One fixed stdout line; a stored-login stop also pages on the loop's stderr channel."""
    print(f"{STOP} Safe diagnostic: {error.stop_tail()}.")
    if str(error) in LOGIN_STOPS:
        _page(str(error))
    return 2


def _sign_in(login: StoredLogin) -> int:
    """Open the session once, so the SDK runs the owner's consent and stores it; no list call."""

    async def run() -> None:
        async with official_client(True, login):
            pass

    try:
        asyncio.run(run())
        kept = login.present()
    except SyncError as error:
        return _stopped(error)
    except StoredLoginError:
        kept = False
    except Exception:
        # SDK/HTTP exceptions may embed credentials; never emit their args/tracebacks.
        print(STOP)
        return 2
    if not kept:
        return _stopped(SyncError(LOGIN_STORE_UNAVAILABLE, stage="authorize"))
    print(SIGNED_IN)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-dir", type=Path)
    parser.add_argument("--max-age", type=int, default=1200)
    parser.add_argument("--authorize", action="store_true", help="Owner browser consent")
    parser.add_argument(
        "--keep-login",
        action="store_true",
        help="Keep and rotate the owner's encrypted stored sign-in (D-23)",
    )
    parser.add_argument(
        "--forget-login",
        action="store_true",
        help="Remove the stored sign-in and exit, without opening a session",
    )
    parser.add_argument("--apply", action="store_true", help="Apply named-list membership changes")
    parser.add_argument(
        "--presets", type=Path, help="Offline receipt for the reviewed Pine directory"
    )
    parser.add_argument(
        "--loop", action="store_true", help="One admitted display sync per J5 cadence tick"
    )
    parser.add_argument("--cadence-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--cadence-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--weekly-usage-pct", type=Decimal, default=Decimal(0))
    parser.add_argument("--usage-source", default=NO_CLAUDE_USAGE)
    parser.add_argument("--landed-sha", default=None)
    parser.add_argument("--enable-source", default=None)
    args = parser.parse_args(argv)
    if args.forget_login:
        try:
            removed = StoredLogin().forget()
        except StoredLoginError:
            print("Stored TradingView sign-in could not be removed; the store refused the request.")
            return 2
        print(
            "Stored TradingView sign-in removed."
            if removed
            else "No stored TradingView sign-in was present."
        )
        return 0
    if args.presets:
        try:
            print(
                json.dumps(
                    {"artifacts": preset_receipt(args.presets), "installed": False},
                    ensure_ascii=False,
                )
            )
            return 0
        except Exception:
            print("Local Pine receipt unavailable; no session opened or installation attempted.")
            return 2
    if args.loop:
        if args.research_dir is None:
            print("Provide --research-dir for the newest complete display unit")
            return 2
        return run_loop(
            research_dir=args.research_dir,
            authorize=args.authorize,
            login=StoredLogin() if args.keep_login else None,
            registry=args.cadence_registry,
            state_root=args.cadence_root,
            max_age=args.max_age,
            weekly_usage_pct=args.weekly_usage_pct,
            usage_source=args.usage_source,
            landed_sha=args.landed_sha,
            enable_source=args.enable_source,
        )
    if args.research_dir is None and args.authorize:
        # Sign-in only: the freshness guard on a display unit does not apply to a run
        # that reads no unit, so the owner can sign in while no fresh unit exists.
        if not args.keep_login:
            print(SIGN_IN_NEEDS_KEEP_LOGIN)
            return 2
        if args.apply:
            print(SIGN_IN_CHANGES_NO_LIST)
            return 2
        return _sign_in(StoredLogin())
    try:
        if args.research_dir is None:
            raise InputError("Provide --research-dir for one complete producer unit")
        login = StoredLogin() if args.keep_login else None
        inputs = read_research_inputs(
            args.research_dir, now=datetime.now(UTC), max_age=args.max_age
        )

        async def run():
            async with official_client(args.authorize, login) as client:
                # SDK task groups wrap an error escaping the transport's __aexit__,
                # which would hide a fixed diagnostic behind the generic stop.
                try:
                    return await sync_research(OfficialTools(client), inputs, apply=args.apply)
                except SyncError as error:
                    return error

        outcome = asyncio.run(run())
        if isinstance(outcome, SyncError):
            raise outcome
        print(json.dumps(outcome, ensure_ascii=False))
        return 0
    except InputError:
        print("Research input unavailable; no session opened and lists unchanged.")
        return 2
    except SyncError as error:
        # Only fixed literals; the message and any upstream text never reach stdout.
        return _stopped(error)
    except Exception:
        # SDK/HTTP exceptions may embed credentials; never emit their args/tracebacks.
        print(STOP)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Owner-ruled J5 admission for the owner-consented display loop; no timer.

Lead 2026-11-08 19:49:24 #1 and the 2026-11-09 TV-LOOP ruling. Native RegistryRow
metadata, XNYS/ET helpers, WriterLease and atomic state writer. The loop supplies
one fresh unit per tick; all clocks, landing/enable and completion receipts are
supplied by its process.
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from sextant.execution.custody import AccountScope, CustodyError, WriterLease
from sextant.execution.state import write_atomic
from sextant.ranking.baseline import segment_start
from sextant.ranking.registry import RegistryRow
from sextant.tradingview.inputs import (
    DISPLAY_PAPER_SLOT,
    ET,
    NAMES,
    QUALIFIED,
    _calendar,
    _ns,
    _session,
)

# The T1 producer document and the loop's explicit two-list display view. Both
# describe exactly one complete membership observation; neither is a score feed.
ADMISSION_SCHEMAS = frozenset({"sextant.j5-ranked/1", "sextant.display-admission/1"})

ROWS = {
    "enabled": "flag",
    "changed_only": "flag",
    "max_concurrent_runs": "runs",
    "max_retries": "retries",
    "overnight_enabled": "flag",
    "overnight_baseline_sessions": "sessions",
    **dict.fromkeys(("onset_floor_s", "regular_floor_s", "usage_floor_s"), "seconds"),
    "weekly_usage_backoff_pct": "percent",
    **dict.fromkeys(
        (
            "display_start_minute_et",
            "display_end_minute_et",
            "am_start_minute_et",
            "am_end_minute_et",
            "pm_start_minute_et",
            "pm_end_minute_et",
        ),
        "minute ET",
    ),
}


def next_display_open(now_ns: int, *, overnight_enabled: bool = False) -> int:
    """Next XNYS daytime/overnight opening, using each candidate's trading date."""
    local = datetime.fromtimestamp(now_ns // 1_000_000_000, ET)
    for offset in range(9):
        day = local.date() + timedelta(days=offset)
        for hour in (4, 20) if overnight_enabled else (4,):
            candidate = datetime.combine(day, time(hour), tzinfo=ET)
            trading_day = day + timedelta(days=1) if hour == 20 else day
            if candidate > local and _calendar(trading_day.year).is_session(trading_day):
                return _ns(candidate)
    raise ValueError("no display session opens in the next eight days")


def _members(value: dict) -> dict:
    """Exactly one complete shape: the ranked document or the two display lists."""
    if not isinstance(value, dict):
        raise ValueError("complete display membership required")
    if set(value) == {"NS Ranked"}:
        keys = ("NS Ranked",)
    elif set(value) == set(NAMES):
        keys = NAMES
    else:
        raise ValueError("complete display membership required")
    result = {}
    for key in keys:
        names = value[key]
        if (
            not isinstance(names, list)
            or any(
                not isinstance(n, str)
                or not QUALIFIED.fullmatch(n)
                or n.split(":", 1)[0] in {"SMART", "IB", "IBKR", "ALPACA"}
                for n in names
            )
            or len(set(names)) != len(names)
        ):
            raise ValueError("exact unique display members required")
        result[key] = sorted(names)
    return result


class Cadence:
    def __init__(
        self,
        root: Path,
        registry: Path,
        *,
        page: Callable[[str], None],
        landed_sha: str = "",
        enable_source: str = "",
        initial_membership: dict | None = None,
        initial_source: str = "",
    ) -> None:
        self.registry_path, self.state_path = registry, root / "j5-cadence.json"
        raw = json.loads(registry.read_text())
        rows = raw["rows"]
        if raw["schema"] != "j5-cadence.registry/1" or len(rows) != len(ROWS):
            raise ValueError("missing or duplicate cadence registry row")
        values = {}
        for item in rows:
            row = RegistryRow(**{**item, "last_change": date.fromisoformat(item["last_change"])})
            if (
                row.name in values
                or row.name not in ROWS
                or row.unit != ROWS[row.name]
                or type(row.value) is not int
                or row.value < 0
                or not row.source.strip()
                or row.calibration_state != "UNCALIBRATED"
            ):
                raise ValueError("sourced UNCALIBRATED integer cadence rows required")
            values[row.name] = row.value
        if (
            values["enabled"] not in {0, 1}
            or values["overnight_enabled"] not in {0, 1}
            or values["overnight_baseline_sessions"] < 5
            or any(
                values[k] != v
                for k, v in {
                    "changed_only": 1,
                    "max_concurrent_runs": 1,
                    "max_retries": 0,
                }.items()
            )
            or not 0
            < values["onset_floor_s"]
            <= values["regular_floor_s"]
            <= values["usage_floor_s"]
            or not 0 <= values["weekly_usage_backoff_pct"] <= 100
            or not 0
            <= values["display_start_minute_et"]
            <= values["am_start_minute_et"]
            < values["am_end_minute_et"]
            <= values["pm_start_minute_et"]
            < values["pm_end_minute_et"]
            <= values["display_end_minute_et"]
            <= 1440
        ):
            raise ValueError("invalid cadence policy bounds")
        self.params = MappingProxyType(values)
        self.params_hash = hashlib.sha256(
            json.dumps(sorted(rows, key=lambda r: r["name"]), sort_keys=True).encode()
        ).hexdigest()
        self.page, self.landed_sha, self.enable_source = page, landed_sha, enable_source
        self.initial_source = initial_source
        self.initial = (
            _members(initial_membership)
            if initial_membership is not None and initial_source.strip()
            else None
        )
        self.lease = WriterLease(AccountScope("tradingview-desktop", "controller"), root, "J5")

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        self.lease.acquire()
        try:
            state = (
                json.loads(self.state_path.read_text())
                if self.state_path.exists()
                else {
                    "params_hash": self.params_hash,
                    "confirmed": self.initial,
                    "confirmed_source": self.initial_source,
                    "last_started_ns": None,
                    "active": None,
                    "failed": False,
                    "usage_high": False,
                    "page_pending": False,
                    "first_session_j5_run_time_ns": None,
                    "first_session_date": None,
                    "last_j5_run_time_ns": None,
                    "run_receipt_source": None,
                }
            )
            if state["params_hash"] != self.params_hash:
                raise ValueError("cadence registry changed; reviewed state migration required")
            yield state
        finally:
            self.lease.close()

    def reconcile(self, membership: dict, *, source: str) -> dict:
        """Compare against the observed membership after a restart.

        A saved comparison is only as current as its last completed session; a
        different broker inventory must not leave a stale pair that skips a
        needed sync. Only a healthy state is reconciled.
        """
        desired = _members(membership)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("complete read-back source required")
        with self._locked() as state:
            if (
                state["active"] is not None
                or state["failed"] is True
                or state["page_pending"] is True
            ):
                raise ValueError("cadence state holds an unresolved run or page")
            if state["confirmed"] != desired:
                state["confirmed"] = desired
                state["confirmed_source"] = source
                write_atomic(self.state_path, state)
            return desired

    def floor(self, now: datetime, weekly_usage_pct: Decimal) -> int | None:
        _ns(now)
        local, p = now.astimezone(ET), self.params
        minute = local.hour * 60 + local.minute
        try:
            _, overnight = _session(now)
        except ValueError:
            return None
        if overnight:
            if not p["overnight_enabled"]:
                return None
        elif not p["display_start_minute_et"] <= minute < p["display_end_minute_et"]:
            return None
        if segment_start(_ns(now))[0] is None:
            return None
        onset = not overnight and (
            p["am_start_minute_et"] <= minute < p["am_end_minute_et"]
            or p["pm_start_minute_et"] <= minute < p["pm_end_minute_et"]
        )
        normal = p["onset_floor_s"] if onset else p["regular_floor_s"]
        return (
            max(normal, p["usage_floor_s"])
            if weekly_usage_pct > p["weekly_usage_backoff_pct"]
            else normal
        )

    def claim(
        self, unit: dict, *, now: datetime, weekly_usage_pct: Decimal | None, usage_source: str
    ) -> dict | None:
        if (
            not self.params["enabled"]
            or not self.landed_sha.strip()
            or not self.enable_source.strip()
        ):
            return None
        if (
            type(weekly_usage_pct) is not Decimal
            or not weekly_usage_pct.is_finite()
            or not 0 <= weekly_usage_pct <= 100
            or not usage_source.strip()
        ):
            return None
        at = _ns(now)
        try:
            with self._locked() as state:
                high = weekly_usage_pct > self.params["weekly_usage_backoff_pct"]
                if high != state["usage_high"]:
                    state.update(usage_high=high, page_pending=high)
                    write_atomic(self.state_path, state)
                    if high:
                        try:
                            self.page(
                                "J5 Claude weekly usage > "
                                f"{self.params['weekly_usage_backoff_pct']}%; "
                                f"floor {self.params['usage_floor_s']}s"
                            )
                        except Exception:
                            state["failed"] = True
                            write_atomic(self.state_path, state)
                            return None
                        state["page_pending"] = False
                        write_atomic(self.state_path, state)
                try:
                    desired = _members(unit["watchlists"])
                    if (
                        unit["schema"] not in ADMISSION_SCHEMAS
                        or unit["status"] != "fresh"
                        or unit["display_only"] is not True
                        or type(unit["paper_slot"]) is not int
                        or unit["paper_slot"] != DISPLAY_PAPER_SLOT
                        or not (desired.get("NS Ranked") or desired.get("NS movers"))
                        or not unit["generated_at_ns"] <= at < unit["expires_at_ns"]
                    ):
                        return None
                except (ValueError, KeyError, TypeError):
                    return None
                floor = self.floor(now, weekly_usage_pct)
                if (
                    floor is None
                    or state["active"]
                    or state["failed"]
                    or state["page_pending"]
                    or state["confirmed"] is None
                    or state["confirmed"] == desired
                    or (
                        state["last_started_ns"] is not None
                        and at - state["last_started_ns"] < floor * 1_000_000_000
                    )
                ):
                    return None
                run = {
                    "run_id": self.lease.token,
                    "params_hash": self.params_hash,
                    "landed_sha": self.landed_sha,
                    "enable_source": self.enable_source,
                    "started_at_ns": at,
                    "session_date": _session(now)[0],
                    "members": desired,
                    "floor_s": floor,
                    "usage_source": usage_source,
                    "weekly_usage_pct": str(weekly_usage_pct),
                }
                state.update(active=run, last_started_ns=at)
                write_atomic(self.state_path, state)
                return run
        except CustodyError:
            return None

    def finish(
        self, run_id: str, *, ended_at: datetime, successful: bool, receipt_source: str
    ) -> None:
        with self._locked() as state:
            run = state["active"]
            if (
                not run
                or run["run_id"] != run_id
                or type(successful) is not bool
                or not receipt_source.strip()
            ):
                raise ValueError("matching completion/read-back receipt required")
            elapsed = _ns(ended_at) - run["started_at_ns"]
            if elapsed < 0:
                raise ValueError("completion clock regressed")
            if state["first_session_j5_run_time_ns"] is None:
                state.update(
                    first_session_j5_run_time_ns=elapsed, first_session_date=run["session_date"]
                )
            state.update(
                active=None,
                failed=not successful,
                last_j5_run_time_ns=elapsed,
                run_receipt_source=receipt_source,
            )
            if successful:
                state["confirmed"] = run["members"]
                state["confirmed_source"] = receipt_source
            write_atomic(self.state_path, state)

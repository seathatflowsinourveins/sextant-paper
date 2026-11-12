"""Per-account config and limit-sheet binding, carried from the pinned node builders."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from nautilus_trader.model import ClientId, InstrumentId

from sextant.paper.sessions import Session

LANES = frozenset({"alpaca-paper-a", "alpaca-paper-b", "alpaca-paper-c", "ibkr-paper"})


def require_ibkr_paper_account(account_id: str) -> str:
    """Validate the unmodified paper code shared by native configs and the writer lease."""
    if not re.fullmatch(r"D[A-Z]{1,2}[0-9]+", account_id):
        raise ValueError("IBKR paper requires a bare D-prefixed account code")
    return account_id


@dataclass(frozen=True)
class PaperAccount:
    lane: str
    account_id: str
    client_id: str
    namespace: str
    sessions: tuple[Session, ...]
    external_claims: tuple[InstrumentId, ...] = ()
    ib_port: int | None = None

    def __post_init__(self):
        if self.lane not in LANES:
            raise ValueError("known paper lane required")
        if (
            not self.account_id
            or not self.client_id
            or not re.fullmatch(r"[A-Za-z0-9]+", self.namespace)
        ):
            raise ValueError("account, client ID and alphanumeric house namespace required")
        ClientId.from_str(self.client_id)  # rc7 refuses it at load, not before every send's hook
        if (
            not isinstance(self.sessions, tuple)
            or not self.sessions
            or any(not isinstance(s, Session) or s is Session.CLOSED for s in self.sessions)
        ):
            raise ValueError("explicit supported sessions required")
        if not isinstance(self.external_claims, tuple) or any(
            not isinstance(iid, InstrumentId) for iid in self.external_claims
        ):
            raise ValueError("external claims must be an immutable tuple of InstrumentIds")
        if self.external_claims and self.lane != "alpaca-paper-b":
            raise ValueError("only B's dedicated manager can claim external instruments")
        if self.lane == "ibkr-paper":
            require_ibkr_paper_account(self.account_id)
            if self.ib_port not in {4002, 7497}:
                raise ValueError("IB paper requires paper port 4002 or 7497")

    @property
    def broker(self) -> str:
        return "INTERACTIVE_BROKERS" if self.lane == "ibkr-paper" else "ALPACA"

    @property
    def scope_key(self) -> str:
        return f"{self.broker}:{self.account_id}"

    @property
    def house_prefix(self) -> str:
        return f"{self.namespace}-"


@dataclass(frozen=True)
class PaperBudget:
    requests: int
    window_s: int
    broker_cap: int
    external_reserve: int
    source: str


def load_budget(path: Path, lane: str, *, ib_rotation_paced: bool = False) -> PaperBudget:
    """Use C's lower shared budget and IB's conditional 30/s path, never 3000/min."""
    if lane not in LANES:
        raise ValueError("known paper lane required")
    row = yaml.safe_load(path.read_text())["lanes"][lane]
    if row["mode"] != "paper":
        raise ValueError("paper mode required")
    if lane == "ibkr-paper":
        field = (
            "order_actions_budget_per_sec"
            if ib_rotation_paced
            else "order_actions_budget_per_sec_ib_data"
        )
        rate, window, cap, reserve = row[field], 1, row["broker_limit_msgs_per_sec"], 0
    else:
        rate, window = row["order_actions_budget_per_min"], 60
        cap, reserve = row["broker_limit_per_min"], row.get("mcp_reserve_per_min", 0)
    if (
        any(type(n) is not int or n < 0 for n in (rate, window, cap, reserve))
        or rate == 0
        or rate + reserve > cap
    ):
        raise ValueError("invalid or over-cap shared paper budget")
    return PaperBudget(rate, window, cap, reserve, f"{path}:lanes.{lane}; ADR-0009")

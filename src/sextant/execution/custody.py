"""ADR-0007 custody facts and lineage; no native external-order claim substitute.

Adapted from pinned nodes/custody{,_lineage,_registry}.py and the account flock
in nautilus_alpaca/execution.py. Reports and supplied custody authority remain
distinct. Orders are controlled only through a durable house registration.
"""

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from sextant.execution.state import decimal, write_atomic

TERMINAL = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED", "REPLACED", "DENIED"})
PENDING = frozenset(
    {
        "PENDING_CANCEL",
        "PENDING_REPLACE",
        "PENDING_UPDATE",
        "PENDING_NEW",
        "INITIALIZED",
        "SUBMITTED",
        "SUBMITTING",
        "UNKNOWN",
    }
)
KNOWN = (
    TERMINAL
    | PENDING
    | frozenset(
        {
            "NEW",
            "ACCEPTED",
            "PARTIALLY_FILLED",
            "TRIGGERED",
            "ACCEPTED_FOR_BIDDING",
            "HELD",
            "DONE_FOR_DAY",
            "STOPPED",
            "SUSPENDED",
            "CALCULATED",
            "RESTATED",
        }
    )
)


VERSIONS = frozenset({1, 2})
RELEASED_VERSION = 2


def _held(row: dict) -> bool:
    """A row only this code version can read: released as never sent."""
    return bool(row.get("unsent"))


def local_close(order: "OrderFact") -> bool:
    """A terminal, unfilled, venue-less report: how a refusal on this node looks when reported."""
    return order.terminal and order.venue_order_id is None and order.filled == 0


class CustodyError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountScope:
    broker: str
    account: str

    def __post_init__(self):
        if not self.broker or not self.account:
            raise ValueError("broker and account required")

    @property
    def key(self) -> str:
        return f"{self.broker}:{self.account}"


@dataclass(frozen=True)
class PositionAuthority:
    quantity: Decimal
    source: str

    def __post_init__(self):
        decimal(self.quantity, "custody quantity")
        if not self.source:
            raise ValueError("position custody source required")


@dataclass(frozen=True)
class OrderFact:
    client_order_id: str
    instrument: str
    side: str
    quantity: Decimal
    filled: Decimal
    status: str
    venue_order_id: str | None
    owner_order: bool = False

    def __post_init__(self):
        decimal(self.quantity, "order quantity", zero=False)
        decimal(self.filled, "filled quantity")
        if self.side not in {"BUY", "SELL"} or self.filled > self.quantity:
            raise ValueError("order side or filled quantity")
        if type(self.owner_order) is not bool or (
            self.status == "FILLED" and self.filled != self.quantity
        ):
            raise ValueError("owner classification or terminal filled quantity")
        if not self.client_order_id or not self.instrument:
            raise ValueError("order identity required")
        if self.status not in KNOWN:
            object.__setattr__(self, "status", "UNKNOWN")

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def leaves(self) -> Decimal:
        return Decimal(0) if self.terminal else self.quantity - self.filled


@dataclass(frozen=True)
class AccountSnapshot:
    scope: AccountScope
    epoch: int
    positions: Mapping[str, Decimal]
    managed: Mapping[str, PositionAuthority]
    orders: tuple[OrderFact, ...]
    complete: bool
    known_at: int
    source: str

    def __post_init__(self):
        for quantity in self.positions.values():
            decimal(quantity, "broker long net")
        if type(self.epoch) is not int or self.epoch < 0 or not self.source or self.known_at < 0:
            raise ValueError("snapshot clocks, epoch or source")
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))
        object.__setattr__(self, "managed", MappingProxyType(dict(self.managed)))


@dataclass(frozen=True)
class FlatReceipt:
    scope: AccountScope
    epoch: int
    known_at: int
    source: str
    terminal_client_ids: tuple[str, ...]
    terminal_venue_ids: tuple[str, ...]


class WriterLease:
    def __init__(self, scope: AccountScope, root: Path, writer: str):
        self.scope, self.writer = scope, writer
        # Account configuration selects one IBKR lock root across launch paths/worktrees.
        # BUILD ON the existing account flock; pinned execution.py:811-821 uses a shared
        # home/.cache root and account digest. No IBKR launch argument overrides this root.
        self.root = (
            Path.home() / ".cache" / "sextant" / "locks"
            if scope.broker == "INTERACTIVE_BROKERS"
            else root
        )
        self.fd: int | None = None
        self.token: str | None = None
        self.pacer: Any = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def acquire(self) -> None:
        if self.held:
            raise CustodyError("writer already holds lease")
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(self.scope.key.encode()).hexdigest()
        fd = os.open(self.root / f"{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise CustodyError("account already has a writer") from None
        self.fd, self.token = fd, uuid4().hex

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd, self.token = None, None
            self.pacer = None

    def bind_pacer(self, pacer):
        if not self.held or (self.pacer is not None and self.pacer is not pacer):
            raise CustodyError("account writer must use one shared pacer")
        self.pacer = pacer


class Custody:
    def __init__(self, scope: AccountScope, path: Path, *, create=False):
        self.scope, self.path = scope, path
        self.loaded, self.error = False, None
        self.registrations: dict[str, dict] = {}
        self.snapshot: AccountSnapshot | None = None
        self.frozen: dict[str, str] = {}
        self._disk: bytes | None = None  # the file as this object last read or wrote it
        self._load(create=create)

    def _read_disk(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def refresh(self) -> bool:
        """Reload if the file changed since this object read or wrote it; True if it did.

        The writer calls this once it (re)acquires the lease, as upstream takes its account
        lock and then restores custody from its file before any venue connects
        (northstar-trading@8256c16 run.py:193-223 admit_accounts, nodes/live.py:489-501
        registry.restore). So a stale map never registers over another writer's rows. The old
        snapshot goes too.
        """
        if self.loaded and self._read_disk() == self._disk:
            return False
        self.snapshot, self.frozen, self.registrations = None, {}, {}
        self.loaded, self.error = False, None
        self._load()
        return True

    def _load(self, *, create=False) -> None:
        scope = self.scope
        try:
            raw = self._read_disk()
            if raw is not None:
                state = json.loads(raw)
                if state["scope"] != scope.key or state["version"] not in VERSIONS:
                    raise ValueError("custody account/version mismatch")
                for cid, row in state["house"].items():
                    if type(row["quantity"]) is not str:
                        raise ValueError("persisted quantity must be a Decimal string")
                    decimal(Decimal(row["quantity"]), "registered quantity", zero=False)
                    if not cid or not row["instrument"] or row["side"] not in {"BUY", "SELL"}:
                        raise ValueError("invalid house registration")
                    if not isinstance(row["lineage"], list) or not row["source"]:
                        raise ValueError("invalid house lineage")
                    sends = row.get("cancel_sends", 0)
                    unanswered = row.get("cancel_unanswered", False)
                    if type(sends) is not int or sends < 0 or type(unanswered) is not bool:
                        raise ValueError("invalid house cancel count")
                    unsent = row.get("unsent")
                    if unsent is not None and (
                        type(unsent) is not str or not unsent or row["lineage"]
                    ):
                        raise ValueError("invalid unsent house record")
                released = any(_held(row) for row in state["house"].values())
                if released != (state["version"] == RELEASED_VERSION):
                    raise ValueError("custody version disagrees with its released rows")
                self.registrations, self._disk = state["house"], raw
                self.loaded = True
            elif create:
                self.loaded = True
                self._save()
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.error = str(error)
            self.loaded = False

    def _save(self) -> None:
        if not self.loaded:
            raise CustodyError("custody state not loaded")
        # Version 2 marks released rows, so code without release_unsent refuses the file
        # visibly instead of reading those rows as live unreported orders.
        released = any(_held(row) for row in self.registrations.values())
        version = RELEASED_VERSION if released else 1
        try:
            write_atomic(
                self.path, dict(version=version, scope=self.scope.key, house=self.registrations)
            )
            self._disk = self._read_disk()
        except OSError as error:
            self.loaded, self.error = False, str(error)
            raise

    def register(self, cid: str, instrument: str, side: str, quantity: Decimal, *, source: str):
        if not self.loaded:
            raise CustodyError("custody state unavailable")
        decimal(quantity, "registered quantity", zero=False)
        if self.snapshot and any(
            o.client_order_id == cid and o.owner_order for o in self.snapshot.orders
        ):
            raise CustodyError("owner order cannot be registered as house")
        if not cid or not instrument or side not in {"BUY", "SELL"} or not source:
            raise CustodyError("invalid house identity/source")
        row = dict(
            instrument=instrument, side=side, quantity=str(quantity), lineage=[], source=source
        )
        if cid in self.registrations:
            old = self.registrations[cid]
            if old.get("unsent"):
                raise CustodyError("a released unsent registration is closed; use a new client ID")
            if any(old[key] != row[key] for key in ("instrument", "side", "quantity", "source")):
                raise CustodyError("house registration is immutable")
            return
        self.registrations[cid] = row
        self._save()

    def link(self, cid: str, venue_id: str, *, predecessor: str | None = None):
        row = self.registrations[cid]
        if row.get("unsent"):
            raise CustodyError("a released unsent registration cannot gain venue lineage")
        if not venue_id or (predecessor is not None and predecessor not in row["lineage"]):
            raise CustodyError("replacement predecessor missing")
        if any(
            venue_id in other["lineage"] for key, other in self.registrations.items() if key != cid
        ):
            raise CustodyError("venue identity already owned")
        if venue_id not in row["lineage"]:
            row["lineage"].append(venue_id)
            self._save()

    def observe(self, snapshot: AccountSnapshot, *, now: int) -> bool:
        if (
            not self.loaded
            or snapshot.scope != self.scope
            or snapshot.known_at > now
            or (self.snapshot and snapshot.epoch < self.snapshot.epoch)
        ):
            return False
        self.snapshot, self.frozen = snapshot, {}
        if not snapshot.complete:
            self.frozen.update(
                (iid, "incomplete account observation") for iid in snapshot.positions
            )
        for iid, net in snapshot.positions.items():
            authority = snapshot.managed.get(iid)
            if net > 0 and (authority is None or authority.quantity != net):
                self.frozen[iid] = "broker quantity lacks agreeing custody authority"
        for order in snapshot.orders:
            if order.status == "UNKNOWN":
                self.frozen[order.instrument] = "unresolved order outcome"
            row = self.registrations.get(order.client_order_id)
            if row is None:
                if not order.owner_order and not order.terminal:
                    self.frozen[order.instrument] = "foreign open order"
                continue
            if row.get("unsent"):
                if not local_close(order):
                    self.frozen[order.instrument] = "an order released as never sent was reported"
                continue
            if (
                order.owner_order
                or row["instrument"] != order.instrument
                or row["side"] != order.side
                or Decimal(row["quantity"]) < order.quantity
                or (order.venue_order_id is not None and order.venue_order_id not in row["lineage"])
            ):
                self.frozen[order.instrument] = "house lineage or quantity mismatch"
            if order.status == "UNKNOWN":
                self.frozen[order.instrument] = "unresolved order outcome"
        present = {order.client_order_id for order in snapshot.orders}
        for cid, row in self.registrations.items():
            if cid not in present and not row.get("unsent"):
                self.frozen[row["instrument"]] = "registered order has no authoritative report"
        return True

    def cancel_sends(self, cid: str) -> int:
        row = self.registrations.get(cid)
        return 0 if row is None else row.get("cancel_sends", 0)

    def record_cancel(self, cid: str) -> None:
        """Count a CANCEL leaving for the wire, durably, as row 8's bridge keeps its cancel_sends
        in custody (alpaca.py:141-152, 355-375), so the cap survives a reopen or a restart."""
        row = self.registrations.get(cid)
        if not self.loaded or row is None:
            raise CustodyError("a loaded house registration is required")
        row["cancel_sends"] = row.get("cancel_sends", 0) + 1
        try:
            self._save()
        except OSError:  # nothing else changed: custody stays usable, the count unrecorded (#198)
            row["cancel_sends"] -= 1
            self.loaded = True
            raise

    def record_unanswered(self, cid: str) -> None:
        """Mark durably that this order's CANCEL went unanswered past cancel_timeout_s, as row 8's
        bridge marks the order UNKNOWN in its records at its cap (alpaca.py:355-360) (#195)."""
        row = self.registrations.get(cid)
        if not self.loaded or row is None:
            raise CustodyError("a loaded house registration is required")
        if not row.get("cancel_unanswered"):
            row["cancel_unanswered"] = True
            try:
                self._save()
            except OSError:  # an audit mark: custody stays usable and keeps it in memory (#198)
                self.loaded = True
                raise

    def release_unsent(self, cid: str, *, reason: str) -> None:
        """Close a house registration known never to have reached the wire (#151).

        Upstream closes such a submit locally (order_reducer.py::_t_cancel_unsent,
        execution.py::AlpacaSubmitAborted). Without this durable record it never gets an
        authoritative report: submission_ready refuses every later order on the instrument,
        observe freezes it and the guard flattens nothing. A linked or reported row was sent.
        """
        row = self.registrations.get(cid)
        if not self.loaded or row is None or type(reason) is not str or not reason:
            raise CustodyError("a loaded house registration and a reason string are required")
        reported = () if self.snapshot is None else self.snapshot.orders
        if row["lineage"] or any(o.client_order_id == cid and not local_close(o) for o in reported):
            raise CustodyError("a linked or reported order was sent; it stays unresolved")
        if not row.get("unsent"):
            row["unsent"] = reason
            self._save()

    def release_local_unsent(self, cid: str, *, reason: str) -> None:
        """Close a zero-send refusal whose own pending fact custody already observed.

        The bridge's local fact is the only report for a never-sent submit. Its zero-send
        rejection is terminal on this node, so rewrite the still-pending venue-less, zero-fill
        fact to the local-close shape before release_unsent's reported-order safeguard. A
        linked or broker-reported order still refuses; a sent order is never released.
        """
        row = self.registrations.get(cid)
        if not self.loaded or row is None or type(reason) is not str or not reason:
            raise CustodyError("a loaded house registration and a reason string are required")
        if row["lineage"]:
            raise CustodyError("a linked or reported order was sent; it stays unresolved")
        snapshot = self.snapshot
        if snapshot is not None:
            orders = []
            rewritten = False
            for order in snapshot.orders:
                if order.client_order_id != cid or local_close(order):
                    orders.append(order)
                    continue
                if order.venue_order_id is not None or order.filled != 0 or order.terminal:
                    raise CustodyError("a linked or reported order was sent; it stays unresolved")
                orders.append(replace(order, status="REJECTED"))
                rewritten = True
            if rewritten:
                self.snapshot = replace(snapshot, orders=tuple(orders))
        self.release_unsent(cid, reason=reason)

    def _order(self, cid: str) -> OrderFact | None:
        if self.snapshot is None:
            return None
        matches = [o for o in self.snapshot.orders if o.client_order_id == cid and not o.terminal]
        return matches[-1] if len(matches) == 1 else None

    def may_cancel(self, cid: str) -> bool:
        order = self._order(cid)
        return bool(
            self.loaded
            and cid in self.registrations
            and order
            and not order.owner_order
            and order.instrument not in self.frozen
        )

    def may_replace(self, cid: str) -> bool:
        order = self._order(cid)
        return bool(self.may_cancel(cid) and order and order.status not in PENDING)

    def owner_reserved(self, iid: str) -> Decimal:
        return sum(
            (
                o.leaves
                for o in (() if self.snapshot is None else self.snapshot.orders)
                if o.instrument == iid and o.owner_order and o.side == "SELL"
            ),
            Decimal(0),
        )

    def submission_ready(self, iid: str, *, exclude_client_id: str | None = None) -> bool:
        snap = self.snapshot
        if not self.loaded or snap is None or not snap.complete or iid in self.frozen:
            return False
        present = {order.client_order_id for order in snap.orders}
        return not any(
            cid != exclude_client_id
            and row["instrument"] == iid
            and cid not in present
            and not row.get("unsent")
            for cid, row in self.registrations.items()
        )

    def available_exit(self, iid: str, *, exclude_client_id: str | None = None) -> Decimal:
        snap = self.snapshot
        if snap is None or not self.submission_ready(iid, exclude_client_id=exclude_client_id):
            return Decimal(0)
        net = snap.positions.get(iid)
        if net is None:
            return Decimal(0)
        reserved = sum(
            (o.leaves for o in snap.orders if o.instrument == iid and o.side == "SELL"), Decimal(0)
        )
        return max(Decimal(0), net - reserved)

    def flat_receipt(self) -> FlatReceipt | None:
        snap = self.snapshot
        if not self.loaded or snap is None or not snap.complete or self.frozen:
            return None
        if any(quantity != 0 for quantity in snap.positions.values()) or any(
            not order.terminal for order in snap.orders
        ):
            return None
        terminal = {o.client_order_id for o in snap.orders if o.terminal}
        venue_ids = {o.venue_order_id for o in snap.orders if o.terminal and o.venue_order_id}
        if {cid for cid, row in self.registrations.items() if not row.get("unsent")} - terminal:
            return None
        if any(set(row["lineage"]) - venue_ids for row in self.registrations.values()):
            return None
        return FlatReceipt(
            self.scope,
            snap.epoch,
            snap.known_at,
            snap.source,
            tuple(sorted(terminal)),
            tuple(sorted(venue_ids)),
        )

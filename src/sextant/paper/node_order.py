"""Paper B's order-mode node entry (N3, #470 rows 10-11): protection only, refusing by default.

With the rank gate row off (``ranking/registry.py:50-61``) the command runs B's Strategy with no
ranking consumer, so no rank is read and the Strategy refuses every house entry; it protects.
Every other start condition below still refuses by default. LEAD-1452 adds the explicit
``--attended-test-entry INSTRUMENT`` evaluation intent inside the same admitted Strategy.

One command builds the paper account's live node in the official engine's shape: the Alpaca data
client, the Alpaca execution client and B's lifecycle Strategy through
``LiveNodeBuilder.from_config(...).add_data_client(...).add_exec_client(...).build()``, then
``node.add_strategy`` and ``node.run()`` on the main thread (nautilus_trader v2.0.0rc8
``examples/live/interactive_brokers/exec_tester.py:82-135``, ``live/__init__.pyi:1147-1190``,
``docs/concepts/live.md:119-121``). A timer stops it through ``LiveNodeHandle.stop()``
(``live.md:186-189``). Every start condition is a named refusal, in this order: the rows, the
leases, the paper-only client, the claim list; then, after connect and reconciliation and
before the Strategy may send, the account must be FLAT (LEAD-0310 9). A refusal writes the receipt.

Engine facts that set the shape (rc8; cited in the manifest row and the part note): the
instruments a data client emits while connecting reach the execution client's 1,024-slot queue
in one start-up flush (``crates/live/src/node/mod.rs:484-498``), so more than 1,024 names is
refused and ``shutdown_on_error`` makes a logged full queue a failed start; custom Python
clients run without a cache database (``docs/concepts/live.md:232-233``).

Each lease excludes against its real current holder under that holder's own root (N3 part 4):
the writer lease under the old writer's locks directory, the stream reservation under the
prestage producer's leases directory (``tradingview/prestage.py:799-803``). Any other exception
before the run writes a failed receipt. B's journal rows and pages are appended to the run's
receipt directory through ``decision_record.DecisionLog``, the old writer's own append-only file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.common import Environment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live import (
    LiveNode,
    LiveNodeBuilder,
    LiveNodeConfig,
    LiveRiskEngineConfig,
    SubmissionRecoveryPolicy,
)
from nautilus_trader.live.clients import ExecutionClientFactory
from nautilus_trader.model import (
    ClientOrderId,
    InstrumentId,
    OrderSide,
    OrderStatus,
    Quantity,
    QuoteTick,
    Symbol,
    TimeInForce,
    TraderId,
)

from sextant.alpaca.config import AlpacaDataClientConfig, AlpacaExecutionClientConfig
from sextant.alpaca.constants import DEFAULT_FEED, VENUE
from sextant.alpaca.execution import _TERMINAL, execution_engine_config
from sextant.alpaca.factories import AlpacaDataClientFactory, AlpacaExecutionClientFactory
from sextant.entry.parameters import EntryParameters
from sextant.execution.custody import AccountScope, CustodyError, WriterLease
from sextant.execution.pacer import ActionKind
from sextant.execution.state import write_atomic
from sextant.paper import node_rank
from sextant.paper.b_lifecycle import BLifecycle, BLifecycleConfig, Journal
from sextant.paper.b_protector import load_b_parameters
from sextant.paper.b_protector_main import (
    EXPECTED_FULL_ENV,
    EXPECTED_SUFFIX_ENV,
    load_account_config,
)
from sextant.paper.config import PaperAccount
from sextant.paper.decision_record import DecisionLog, Row
from sextant.paper.sessions import Session, session_at
from sextant.ranking import node_ranking
from sextant.ranking.consumer import RankingConsumer
from sextant.ranking.registry import RANK_GATE_ENABLED

NODE_NAME = "paper-b-node"
CLIENT_NAME = "ALPACA"
WRITER = "b-node"
HOUSE_PREFIX = "O-"  # rc8's generator O-{date}-{time}-{trader}-{tag}-{n} (b_lifecycle._house)
QUEUE = 1024  # rc8 crates/live/src/python/runtime.rs:39 COMMAND_CAPACITY, an engine constant
RECEIPT = "order-mode-receipt.json"
JOURNAL = "b-lifecycle-journal.jsonl"  # beside the receipt; the node keeps no other store
WIDTH = ("alpaca_min_trail_fraction", "trail_atr_period", "trail_atr_bar_minutes")
WIDTH += ("trail_atr_multiple",)
TIMING = frozenset(f"timeout_{n}_secs" for n in ("connection", "reconciliation", "portfolio"))
TIMING |= {"timeout_disconnection_secs", "delay_post_stop_secs"}
RESTART = (
    "no cache database: custom Python clients support none in either launch mode (rc8 "
    "docs/concepts/live.md:232-233). A restart rebuilds orders and positions from the venue's "
    "reports (startup reconciliation) and re-arms at the native width; the campaign record "
    "(width and offsets in force, counts, re-entry state) is not recovered."
)


class OrderModeRefused(ValueError):
    """A start condition failed; ``reason`` names it and the receipt records it."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


@dataclass(frozen=True)
class Rows:
    native: Decimal
    poll_ns: int  # reconcile_interval_s
    width: dict[str, Decimal] | None
    inflight_ms: int
    commission: list[dict[str, Any]]
    pacing: list[dict[str, Any]]
    entry: EntryParameters | None
    steps: dict[str, str]


@dataclass(frozen=True)
class AttendedEntry:
    instrument_id: InstrumentId
    quantity: Quantity
    max_age_ns: int


@dataclass(frozen=True)
class OrderArguments:
    account: PaperAccount
    rows: Rows
    universe_artifact: Path
    duration_s: int
    receipt_dir: Path
    api_key_env: str
    api_secret_env: str
    writer_lease_root: Path  # the old writer's locks directory: its WriterLease is there
    stream_lease_root: Path  # the prestage producer's leases directory: its reservation is there
    existing_streams: int
    lease_source: str
    universe: tuple[InstrumentId, ...] = ()  # the claim list, read after the leases
    universe_source: str = "not read"
    ranking: node_ranking.RankingArguments | None = None
    attended: AttendedEntry | None = None


def _rows(path: Path) -> list[dict[str, Any]]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(r, dict) and "name" in r for r in rows):
        raise ValueError(f"{Path(path).name} is not a row list")
    return rows


def _unavailable(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(row["name"] for row in rows if row.get("value") is None)


def load_rows(raw: argparse.Namespace) -> Rows:
    """Every registry through its existing loader; protection needs only the native row."""
    try:
        b = load_b_parameters(raw.b_parameters)
        node = {row["name"]: row for row in _rows(raw.node_parameters)}
        pacing = _rows(raw.custody_parameters)
        entry_rows, exit_rows = _rows(raw.entry_parameters), _rows(raw.exit_parameters)
        reentry_rows = _rows(raw.reentry_parameters)
        inflight = node["native_inflight_check_interval_ms"]["value"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise OrderModeRefused("registries", f"{type(error).__name__}: {error}") from error
    if type(inflight) is not int or inflight < 0:
        raise OrderModeRefused("registries", "native_inflight_check_interval_ms is not an int")
    steps: dict[str, str] = {"protect": f"on: native_trail_fraction {b.native_trail_fraction}"}
    # The data client streams 1-minute bars only (alpaca/data.py _bar_timeframe); another width
    # bar is refused there, and under shutdown_on_error that refusal would stop the node.
    streamed = b.width_on and b.trail_atr_bar_minutes == 1
    width = {n: getattr(b, n) for n in WIDTH} if streamed else None
    steps["tighten"] = (
        "on: width rows on the streamed 1-minute bar"
        if streamed
        else "off: width rows unavailable"
        if not b.width_on
        else "off: the width bar is not streamed; it needs the engine's own aggregation "
        "(LEAD-0310 4, a Strategy part)"
    )
    entry: EntryParameters | None = None
    try:
        entry = EntryParameters.from_rows(entry_rows, sizing_only=True)
        steps["entry"] = "rows available; the rank gate row is off (ranking/registry.py:50)"
    except (ValueError, KeyError, TypeError) as error:
        steps["entry"] = f"off: {error}"
    steps["gain_lock"] = (
        "off: no loader reads B's own exit file into ExitParams (LEAD-0310 6); unavailable "
        f"rows {_unavailable(exit_rows)}"
    )
    steps["reentry"] = f"off: no re-entry part on main; unavailable {_unavailable(reentry_rows)}"
    commission = [row for row in node.values() if row["name"].startswith("paper_us_equity_")]
    poll = int(b.reconcile_interval_s * 1_000_000_000)
    return Rows(b.native_trail_fraction, poll, width, inflight, commission, pacing, entry, steps)


def read_universe(path: Path) -> tuple[tuple[InstrumentId, ...], str]:
    """The ranked universe file, the K2 artifact the ranking mode reads, and nothing else."""
    from sextant.data.baseline_recipe import load_universe

    try:
        names, digest = load_universe(Path(path))
    except (OSError, ValueError) as error:
        raise OrderModeRefused("claim_list", f"{type(error).__name__}: {error}") from error
    source = f"K2 universe artifact sha256 {digest['sha256']}"
    return tuple(InstrumentId(Symbol(name), VENUE) for name in names), source


def _absolute_directory(value: str) -> Path:
    """argparse type callable: CPython v3.13.18 Lib/argparse.py:2571-2584."""
    path = Path(value)
    if not value or not path.is_absolute():
        raise argparse.ArgumentTypeError("must be a non-empty absolute directory path")
    return path


def parse(argv: list[str] | None = None) -> OrderArguments:
    parser = argparse.ArgumentParser(
        prog="python -m sextant.paper.node_order", allow_abbrev=False, exit_on_error=False
    )
    parser.add_argument("--attended-test-entry", metavar="INSTRUMENT")
    required = parser.add_argument_group("required")
    for flag, kind in (
        ("--account-config", Path),
        ("--universe-artifact", Path),
        ("--b-parameters", Path),
        ("--entry-parameters", Path),
        ("--exit-parameters", Path),
        ("--reentry-parameters", Path),
        ("--node-parameters", Path),
        ("--custody-parameters", Path),
        ("--receipt-dir", _absolute_directory),
        ("--duration-s", int),
        ("--api-key-env", str),
        ("--api-secret-env", str),
        ("--paper-slot", int),
        ("--writer-lease-root", _absolute_directory),
        ("--stream-lease-root", _absolute_directory),
        ("--existing-streams", int),
        ("--lease-source", str),
    ):
        required.add_argument(flag, type=kind, required=True)
    for flag in ("--ranking-universe-artifact", "--ranking-baseline-dir", "--ranking-inputs-dir"):
        parser.add_argument(flag, type=Path)
    try:
        raw = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        directories = {"--receipt-dir", "--writer-lease-root", "--stream-lease-root"}
        if error.argument_name in directories:
            reason = error.argument_name.removeprefix("--").replace("-", "_")
            # Recover only a valid destination, irrespective of argument order. Use
            # argparse's own parser again; no argv scanner or admission is involved.
            destination = argparse.ArgumentParser(
                add_help=False, allow_abbrev=False, exit_on_error=False
            )
            destination.add_argument("--receipt-dir", type=_absolute_directory)
            try:
                receipt, _ = destination.parse_known_args(argv)
            except argparse.ArgumentError:
                receipt = argparse.Namespace(receipt_dir=None)
            if receipt.receipt_dir is not None:
                write_atomic(
                    receipt.receipt_dir / RECEIPT,
                    {
                        "schema": "sextant.paper-order-node.receipt/1",
                        "phase": "refused",
                        "reason": reason,
                        "detail": str(error),
                    },
                )
            parser.error(f"{reason}: {error}")
        parser.error(str(error))
    if raw.paper_slot != 2:  # slot 1 is the owner's account, slot 3 is revoked (AGENTS.md)
        raise OrderModeRefused("paper_slot", f"only paper slot 2, not {raw.paper_slot}")
    if raw.duration_s <= 0:
        raise OrderModeRefused("duration", "must be a positive number of seconds")
    try:
        account = load_account_config(raw.account_config)
    except ValueError as error:
        raise OrderModeRefused("registries", f"account config: {error}") from error
    if account.lane != "alpaca-paper-b":
        raise OrderModeRefused("registries", "the account config is not Paper B's")
    rows = load_rows(raw)
    try:
        ranking = node_rank.load(raw)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise OrderModeRefused("ranking", str(error)) from error
    return OrderArguments(
        account,
        rows,
        raw.universe_artifact,
        raw.duration_s,
        raw.receipt_dir,
        raw.api_key_env,
        raw.api_secret_env,
        raw.writer_lease_root,
        raw.stream_lease_root,
        raw.existing_streams,
        raw.lease_source,
        ranking=ranking,
        attended=load_attended(raw),
    )


def load_attended(raw: argparse.Namespace) -> AttendedEntry | None:
    """LEAD-1452: explicit intent only; use the existing row/freshness loaders."""
    if raw.attended_test_entry is None:
        return None
    try:
        iid = InstrumentId.from_str(raw.attended_test_entry)
    except ValueError as error:
        raise OrderModeRefused("attended_test_entry_instrument", "invalid instrument") from error
    try:
        rows = [r for r in _rows(raw.node_parameters) if r["name"] == "attended_test_entry_qty"]
        if len(rows) != 1:
            raise ValueError("attended_test_entry_qty missing or duplicated")
        row = rows[0]
        qty = row.get("value")
        if (
            type(qty) is not int
            or qty <= 0
            or row.get("calibration_state") != "UNCALIBRATED"
            or row.get("lane_scope") != "alpaca-paper-b"
            or not all(row.get(k) for k in ("source", "unit", "last_change"))
        ):
            raise ValueError("attended_test_entry_qty lacks whole-share starting authority")
        fresh = EntryParameters.from_rows(_rows(raw.entry_parameters), first_leg_only=True)
        return AttendedEntry(iid, Quantity.from_int(qty), fresh.ns("max_data_age_s"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise OrderModeRefused("attended_test_entry_parameters", str(error)) from error


def execution_config(args: OrderArguments) -> AlpacaExecutionClientConfig:
    return AlpacaExecutionClientConfig(
        api_key_env=args.api_key_env,
        api_secret_env=args.api_secret_env,
        account_id=f"ALPACA-{args.account.namespace}",  # native.py's SIM-{namespace} shape
        house_prefix=HOUSE_PREFIX,
        pacing_rows=args.rows.pacing,
        commission_rows=args.rows.commission,
    )


def refuse_non_paper(clients: list[tuple[str, Any, Any]], config: LiveNodeConfig) -> None:
    """One execution client, the Alpaca one: it connects with ``paper=True`` only
    (execution.py ``_connect``), and alpaca-py then picks the paper host with no override
    (alpaca-py v0.44.0 ``alpaca/trading/client.py:76-83``)."""
    named = [f"{name}: {type(cfg).__name__}" for name, _, cfg in clients]
    if config.exec_clients or len(clients) != 1:
        raise OrderModeRefused("execution_client", f"exactly one Alpaca client, not {named}")
    _, factory, cfg = clients[0]
    kind = factory if isinstance(factory, type) else type(factory)
    if not issubclass(kind, AlpacaExecutionClientFactory) or type(cfg) is not (
        AlpacaExecutionClientConfig
    ):
        raise OrderModeRefused("execution_client", f"not the paper Alpaca client: {named}")


def node_config(args: OrderArguments, **timing: Any) -> LiveNodeConfig:
    unknown = sorted(set(timing) - TIMING)
    if unknown:
        raise TypeError(f"unknown timing fields {unknown}")
    return LiveNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId.from_str(f"PAPER{args.account.namespace}-001"),  # native.py:77
        shutdown_on_error=True,
        risk_engine=LiveRiskEngineConfig(bypass=False),
        exec_engine=execution_engine_config(
            universe=args.universe,
            inflight_check_interval_ms=args.rows.inflight_ms,
            submission_recovery_policy=SubmissionRecoveryPolicy.RETAIN_UNRESOLVED,
        ),
        **timing,
    )


@dataclass
class Gate:
    """The cutover rule (LEAD-0310 9): FLAT and no resting house order, or nothing is sent."""

    old_prefix: str
    claims: set[str] = field(default_factory=set)
    poll_ns: int = 0
    paged: set[str] = field(default_factory=set)
    clients: list[Any] = field(default_factory=list)
    checked: bool = False
    open: bool = False
    reasons: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    refused_sends: int = 0

    def check(self, cache: Any) -> list[str]:
        positions = list(cache.positions_open())
        house = [
            order
            for order in cache.orders_open()
            if str(order.client_order_id).startswith((HOUSE_PREFIX, self.old_prefix))
        ]
        withheld = [row for client in self.clients for row, _ in client.withheld_positions]
        # A withheld row is withheld whatever its status (its name is not claimed); only a row
        # that has not ended is open. Alpaca's terminal statuses (execution.py _TERMINAL) are
        # the engine's closed set (rc8 crates/model/src/enums.rs:1489-1500) plus "replaced",
        # which ends only its own row: its whole lineage is walked (_lineage).
        rows = [row for client in self.clients for row, _ in client.withheld]
        by_id = {row.get("id"): row for row in rows}
        ends, unresolved = {}, 0
        for row in rows:
            if not str(row.get("client_order_id", "")).startswith((HOUSE_PREFIX, self.old_prefix)):
                continue
            state, last = _lineage(row, by_id)
            if state == "unresolved":
                unresolved += 1
            elif state == "open":
                ends[id(last)] = last  # one lineage counts once, however many rows lead into it
        self.counts = {
            "open_positions": len(positions),
            "open_house_orders": len(house) + len(ends),
            "withheld_holdings": len(withheld),
            "unresolved_replacements": unresolved,
        }
        names = ("not_flat", "house_order_open", "holding_outside_claims", "replacement_unresolved")
        values = (len(positions), self.counts["open_house_orders"], len(withheld), unresolved)
        return [name for name, count in zip(names, values, strict=True) if count]


def _lineage(row: dict[str, Any], by_id: Mapping[Any, dict[str, Any]]) -> tuple[str, Any]:
    """Walk a replacement lineage through Alpaca's ``replaced_by`` link (alpaca-py cc4cb3b7
    ``alpaca/trading/models.py:223``; a successor may carry any client id, ``requests.py:300``),
    following a populated link on ANY status, so a terminal-looking middle never ends it early.

    ``("open", last)`` when any visited row is working or of unknown status; otherwise
    ``("unresolved", last)`` for a link to a row not in the same listing, a cycle, a link that is
    not a string, or a last row ``replaced`` with no link; ``("ended", last)`` only when every
    visited row is terminal and the last has no link. It only reads; nothing is sent or changed."""
    seen, working = {id(row)}, False
    while True:
        working = working or row.get("status") not in _TERMINAL
        key = row.get("replaced_by")
        if key is None or key == "":
            if working:
                return "open", row
            return ("unresolved" if row.get("status") == "replaced" else "ended"), row
        successor = by_id.get(key) if isinstance(key, str) else None
        if successor is None or id(successor) in seen:
            return ("open" if working else "unresolved"), row
        seen.add(id(successor))
        row = successor


def account_matches(number: object) -> bool:
    """The old writer's rule (b_protector_main.py:300-313): the full id wins over the suffix.
    Compared as digests, so neither value is printed, logged or kept."""
    full = os.environ.get(EXPECTED_FULL_ENV) or ""
    suffix = os.environ.get(EXPECTED_SUFFIX_ENV) or ""
    if not isinstance(number, str) or not (full or suffix):
        return False
    seen, want = (number, full) if full else (number[-len(suffix) :], suffix)
    digest = [hashlib.sha256(text.encode()).digest() for text in (seen, want)]
    return hmac.compare_digest(*digest)


class GatedBLifecycle(BLifecycle):
    """B's Strategy, built from its own config, behind the cutover gate: ``on_start`` checks
    the reconciled cache first, and every send is refused until the gate opens. A refused gate
    asks the node to stop (``Strategy.shutdown_system``, rc8 trading stub :525)."""

    gate: Gate

    def __init__(self, config: BLifecycleConfig) -> None:
        super().__init__(config)
        self.attended: AttendedEntry | None = None
        self.attended_attempted = False
        self.attended_order_id: ClientOrderId | None = None
        self.attended_stopping = False
        self.attended_stop: Callable[[], None] | None = None
        self.attended_deadline = 0
        self.attended_outcome: dict[str, Any] = {}
        self.ask_reason = "no_quote"

    def on_quote(self, quote: QuoteTick) -> None:
        super().on_quote(quote)
        intent = self.attended
        if intent is None or self.attended_attempted or not self.gate.open:
            return
        if self.attended_stopping:
            return
        book, now = self.books.get(quote.instrument_id), self.clock.timestamp_ns()
        if quote.instrument_id == intent.instrument_id:
            self.ask_reason = "stale" if now - quote.ts_event > intent.max_age_ns else "crossed"
        if (
            quote.instrument_id != intent.instrument_id
            or book is None
            or book.instrument is None
            or book.quote is not quote
            or now - quote.ts_event > intent.max_age_ns
            or not 0 < quote.bid_price.as_decimal() <= quote.ask_price.as_decimal()
        ):
            return
        # Consume before any fallible routing/factory/send, like rc8 testkit strategy.rs:503-515.
        self.attended_attempted = True
        routed, client = self._route(book, instrument=str(book.instrument_id))
        if not routed:
            return
        order = self.order_factory.limit(
            intent.instrument_id,
            OrderSide.BUY,
            intent.quantity,
            quote.ask_price,
            time_in_force=TimeInForce.DAY,
            tags=["attended-test-entry"],
        )  # rc8 common stub:1388-1406; same native factory/Strategy identity as ordinary B.
        self.attended_order_id = order.client_order_id
        self._record(
            kind="attended_test_entry",
            ts=now,
            order=str(order.client_order_id),
            quantity=str(intent.quantity),
            limit=str(quote.ask_price),
        )
        self.submit_order(
            order, client_id=client, params={"extended_hours": session_at(now) is not Session.RTH}
        )

    def stop_attended(self, stop: Callable[[], None], bound_ns: int) -> None:
        """Node-owned pre-stop: rc8 callbacks require RUNNING (strategy/mod.rs:1397)."""
        if self.attended_stopping:
            return
        self.attended_stopping, self.attended_stop = True, stop
        try:
            self.attended_deadline = self.clock.timestamp_ns() + bound_ns
            self.clock.set_time_alert_ns(
                "b-entry-stop", self.attended_deadline, self._attended_done
            )
            cid = self.attended_order_id
            order = None if cid is None else self.cache.order(cid)
            if cid is not None and order is not None and not order.is_closed:
                self._cancel(cid)
        except Exception:
            return self._attended_done(failed=True)
        self._attended_done()

    def _on_order(self, event: Any) -> None:
        try:
            super()._on_order(event)
        except Exception:
            self._attended_done(failed=True)
            if not self.attended_stopping:
                raise
        self._attended_done()

    def _attended_done(self, _event: Any = None, *, failed: bool = False) -> None:
        if self.attended_stop is None:
            return
        counts = {"sequence_errors": 1}
        try:
            book = None if self.attended is None else self.books.get(self.attended.instrument_id)
            if not failed and book is not None:
                cid = self.attended_order_id
                order = None if cid is None else self.cache.order(cid)
                pending = int(cid is not None and (order is None or not order.is_closed))
                cover = Decimal(0)
                for own_id in book.offsets:  # only B's own native protective trails
                    trail = self.cache.order(own_id)
                    if trail is not None and trail.status is OrderStatus.ACCEPTED:
                        cover += trail.leaves_qty.as_decimal()
                missing = max(Decimal(0), self._held(book) - cover)
                if (pending or missing) and self.clock.timestamp_ns() < self.attended_deadline:
                    return
                counts = {"entry_pending": pending, "unprotected_shares": int(missing)}
            elif not failed:
                counts = {"book_missing": 1}
        except Exception:
            counts = {"sequence_errors": 1}
        stop, self.attended_stop = self.attended_stop, None
        try:
            stop()
        except Exception:
            counts = {"sequence_errors": 1}
        if any(counts.values()):
            self.attended_outcome = {"entry_unresolved_at_stop": counts}
        if self.attended_outcome:
            try:
                print(f"ERROR entry_unresolved_at_stop {counts}", file=sys.stderr, flush=True)
            except Exception:
                self.journal_errors += 1  # diagnostics cannot strand the stop

    def on_start(self) -> None:
        gate = self.gate
        gate.reasons, gate.checked = gate.check(self.cache), True
        if gate.reasons:
            return self._stop_refused()
        gate.clients[0].create_task(self._verify(), "b-node-account")

    def _stop_refused(self) -> None:
        self.shutdown_system("order mode refused: " + ", ".join(self.gate.reasons))

    async def _verify(self) -> None:
        """After connect: the account the client reached is the expected one, read through
        the client's own paced gateway; only then does B start."""
        client = self.gate.clients[0]
        try:
            row = await client.gateway.call(ActionKind.QUERY, "get", "/account")
            number = row.get("account_number") if isinstance(row, dict) else None
        except Exception:  # an unread account is never a match
            number = None
        if not account_matches(number):
            self.gate.reasons = ["account_mismatch"]
            return self._stop_refused()
        self.gate.open = True
        BLifecycle.on_start(self)
        self.clock.set_timer_ns(
            "b-node-outside-holdings",
            self.gate.poll_ns,
            callback=lambda _event: client.create_task(self._outside(), "b-node-outside"),
        )

    async def _outside(self) -> None:
        """While RUNNING: a holding in a name outside the claim list cannot be protected
        here, so each such name is paged once (the unit's journal) and counted."""
        rows = await self.gate.clients[0].gateway.call(ActionKind.QUERY, "get", "/positions")
        for row in rows if isinstance(rows, list) else ():
            name = row.get("symbol") if isinstance(row, dict) else None
            if isinstance(name, str) and name not in self.gate.claims | self.gate.paged:
                self.gate.paged.add(name)
                print(f"order node page holding_outside_claims_running {name}", file=sys.stderr)

    def _allowed(self) -> bool:
        if not self.gate.open:
            self.gate.refused_sends += 1
        return self.gate.open

    def submit_order(self, *args: Any, **kwargs: Any) -> Any:
        return super().submit_order(*args, **kwargs) if self._allowed() else None

    def modify_order(self, *args: Any, **kwargs: Any) -> Any:
        return super().modify_order(*args, **kwargs) if self._allowed() else None

    def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
        return super().cancel_order(*args, **kwargs) if self._allowed() else None


class _Retaining(ExecutionClientFactory):
    """The factory, keeping each client it creates so the gate can read the withheld rows."""

    def __init__(self, factory: Any, sink: list[Any]) -> None:
        self.factory, self.sink = factory, sink

    def create(self, **kwargs: Any) -> Any:  # type: ignore[override]
        client = self.factory.create(**kwargs)
        self.sink.append(client)
        return client


def journal_into(log: DecisionLog) -> Journal:
    """B's ``Journal`` over the existing append-only log: append never raises or blocks."""
    return lambda row: log.append(Row(row))


def strategy_config(
    args: OrderArguments, *, consumer: RankingConsumer | None, journal: Journal | None
) -> BLifecycleConfig:
    return BLifecycleConfig(
        namespace=args.account.namespace,
        instrument_ids=args.universe,
        consumer=consumer,
        rank_gate=RANK_GATE_ENABLED,
        journal=journal,
        entry_params=args.rows.entry,
        native_trail_fraction=args.rows.native,
        width_rows=args.rows.width,
    )


def build(
    args: OrderArguments,
    *,
    consumer: RankingConsumer | None,
    journal: Journal | None = None,
    data_factory: Any = AlpacaDataClientFactory,
    exec_factory: Any = AlpacaExecutionClientFactory,
    timing: Mapping[str, float] | None = None,
) -> tuple[LiveNode, GatedBLifecycle]:
    loaded_ids = args.universe
    if args.ranking is not None:
        names = set(args.ranking.universe)
        if not {iid.symbol.value for iid in args.universe} <= names:
            raise OrderModeRefused("ranking_universe", "claims absent from ranking universe")
        if len(names) > QUEUE:
            raise OrderModeRefused(
                "ranking_capacity",
                "full universe exceeds startup queue; "
                "requires the separately reviewed chunked-load part",
            )
        loaded_ids = tuple(InstrumentId(Symbol(n), VENUE) for n in sorted(names))
        if consumer is not None:
            raise OrderModeRefused("ranking_consumer", "in-node composition owns its consumer")
        try:
            consumer = node_rank.consumer(args.ranking, now=time.time_ns())
        except ValueError as error:
            raise OrderModeRefused("ranking", str(error)) from error
    # The Strategy first, so a refused config leaves no node built on this thread.
    strategy = GatedBLifecycle(strategy_config(args, consumer=consumer, journal=journal))
    strategy.attended = args.attended
    config = node_config(args, **dict(timing or {}))
    exec_config = execution_config(args)
    refuse_non_paper([(CLIENT_NAME, exec_factory, exec_config)], config)
    gate = Gate(args.account.house_prefix, {iid.symbol.value for iid in args.universe})
    gate.poll_ns = args.rows.poll_ns
    data = AlpacaDataClientConfig(
        api_key_env=args.api_key_env,
        api_secret_env=args.api_secret_env,
        feed=DEFAULT_FEED,
        instrument_provider=InstrumentProviderConfig(load_ids=[str(i) for i in loaded_ids]),
    )
    node = (
        LiveNodeBuilder.from_config(NODE_NAME, config)
        .add_data_client(CLIENT_NAME, data_factory, data)
        .add_exec_client(CLIENT_NAME, _Retaining(exec_factory, gate.clients), exec_config)
        .build()
    )
    strategy.gate = gate
    try:
        node.add_strategy(strategy)
        if args.ranking is not None:
            node_ranking.attach(node, args.ranking, inputs=node_ranking.file_inputs(args.ranking))
    except Exception:
        node.dispose()
        raise
    return node, strategy


def write_receipt(args: OrderArguments, phase: str, **body: Any) -> Path:
    scope = AccountScope(args.account.broker, args.account.account_id).key
    write_atomic(
        args.receipt_dir / RECEIPT,
        {
            "schema": "sextant.paper-order-node.receipt/1",
            "phase": phase,
            "steps": args.rows.steps,
            "claims": {
                "count": len(args.universe),
                "source": args.universe_source,
                "startup_flush": len(args.universe),
                "queue_capacity": QUEUE,
                "load_after_start": "not built: needs the data client and execution client parts",
            },
            "lease": {
                "writer": WRITER,
                "scope_sha256": hashlib.sha256(scope.encode()).hexdigest(),
                "market_data": "rank-live",
                "writer_root": str(args.writer_lease_root),
                "stream_root": str(args.stream_lease_root),
            },
            "engine": {
                "environment": "LIVE",
                "house_prefix": HOUSE_PREFIX,
                "reconciliation": "startup, unbounded lookback, scoped to the claim list",
                "filter_unclaimed_external_orders": True,
                "generate_missing_orders": False,
                "inflight_check_interval_ms": args.rows.inflight_ms,
                "submission_recovery_policy": "RETAIN_UNRESOLVED",
                "shutdown_on_error": True,
                "cache_database": None,
            },
            "restart": RESTART,
            **body,
        },
    )
    return args.receipt_dir / RECEIPT


async def run_attended(node: LiveNode, strategy: GatedBLifecycle, args: OrderArguments) -> None:
    """Hosted rc8 run leaves duration/signals with us (python/node.rs:968-994)."""
    loop = asyncio.get_running_loop()
    bound = int(LiveNodeConfig().timeout_shutdown_secs * 1_000_000_000)

    def stop() -> None:
        strategy.stop_attended(node.handle().stop, bound)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    timer = loop.call_later(args.duration_s, stop)
    try:
        await node.run_async()
    finally:
        timer.cancel()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def run(
    args: OrderArguments,
    *,
    consumer: RankingConsumer | None,
    leases: Callable[[ExitStack, OrderArguments], None],
    data_factory: Any = AlpacaDataClientFactory,
    exec_factory: Any = AlpacaExecutionClientFactory,
    timing: Mapping[str, float] | None = None,
) -> tuple[Path, int]:
    """The remaining start conditions in order, then the node; (receipt, exit code)."""
    # The Strategy takes no consumer only while the rank gate row is False (P2's
    # BLifecycleConfig refuses any other pair); with it off, B protects and no rank is read.
    # Only the bool False counts as off here, so a 0 or a missing value refuses.
    if consumer is None and RANK_GATE_ENABLED.value is not False:  # before any lease
        detail = "the rank gate row is on and no RankingConsumer exists (ranking/registry.py:50)"
        return write_receipt(args, "refused", reason="ranking_consumer", detail=detail), 2
    if not (os.environ.get(EXPECTED_FULL_ENV) or os.environ.get(EXPECTED_SUFFIX_ENV)):
        detail = f"set {EXPECTED_FULL_ENV} or {EXPECTED_SUFFIX_ENV} (b_protector_main.py:300-306)"
        return write_receipt(args, "refused", reason="account_mismatch", detail=detail), 2
    with ExitStack() as held:
        try:
            leases(held, args)
            # Nothing but the one paper client may configure an execution client.
            refuse_non_paper(
                [(CLIENT_NAME, exec_factory, execution_config(args))], LiveNodeConfig()
            )
            old = args.account.house_prefix
            if HOUSE_PREFIX.startswith(old) or old.startswith(HOUSE_PREFIX):
                raise OrderModeRefused("house_prefix", "the old writer's prefix overlaps O-")
            universe, source = read_universe(args.universe_artifact)
            if len(universe) > QUEUE:
                raise OrderModeRefused(
                    "claim_list",
                    f"{len(universe)} names exceed one startup flush of the execution client's "
                    f"{QUEUE}-slot queue; the chunked load after start is not built",
                )
            args = replace(args, universe=universe, universe_source=source)
            if args.attended is not None and args.attended.instrument_id not in universe:
                raise OrderModeRefused(
                    "attended_test_entry_instrument", "instrument is outside the claim list"
                )
            log = DecisionLog(args.receipt_dir / JOURNAL, on_page=journal_page)
            held.callback(log.close)
            node, strategy = build(
                args,
                consumer=consumer,
                journal=journal_into(log),
                data_factory=data_factory,
                exec_factory=exec_factory,
                timing=timing,
            )
        except OrderModeRefused as refusal:
            return write_receipt(args, "refused", reason=refusal.reason, detail=refusal.detail), 2
        except Exception as error:  # any other start failure: a receipt, then the leases go
            name = type(error).__name__
            return write_receipt(args, "failed", reason=name, detail=f"{name}: {error}"), 2
        if strategy.missing:  # the Strategy's own list (P2 BLifecycle.missing): no house entry
            off = f"off: the Strategy refuses entries; missing {strategy.missing}"
            args = replace(args, rows=replace(args.rows, steps=args.rows.steps | {"entry": off}))
        write_receipt(args, "starting")
        timer = threading.Timer(args.duration_s, node.handle().stop)
        timer.daemon = True
        failure = None
        try:
            if args.attended is not None:
                asyncio.run(run_attended(node, strategy, args))
            else:
                timer.start()
                node.run()
        except Exception as error:  # the run's own failure is recorded, then the exit is nonzero
            failure = f"{type(error).__name__}: {error}"
        finally:
            timer.cancel()
            node.dispose()
        gate = strategy.gate
        cause = startup_cause(gate.clients)
        outcome = strategy.attended_outcome
        if args.attended and gate.open and not strategy.attended_attempted and not outcome:
            counts = dict(last_reason=strategy.ask_reason, attempts=0, submits=0)
            outcome = {"test_entry_not_sent": counts}
        failure = failure or (
            "entry_unresolved_at_stop" if "entry_unresolved_at_stop" in outcome else None
        )
        phase = "stopped" if gate.open else "refused" if gate.checked else "aborted_at_start"
        phase = "failed" if failure else phase
        reason = failure or (None if gate.open else ",".join(gate.reasons) or "startup_aborted")
        journal = {"file": JOURNAL, "errors": strategy.journal_errors}
        del node, strategy  # rc8 allows one LiveNode per thread until it is released
        log.close()  # every row the Strategy appended is on disk before the final receipt
        receipt = write_receipt(
            args,
            phase,
            reason=reason,
            gate=gate.counts,
            refused_sends=gate.refused_sends,
            outside_holdings_paged=len(gate.paged),
            journal=journal,
            **({"reconciliation": cause} if cause else {}),
            **outcome,
        )
        return receipt, 0 if phase == "stopped" else 2


def startup_cause(clients: list[Any]) -> dict[str, Any] | None:
    """The client's per-stage startup trace (counts, classes, status, our own reason texts),
    including terminal history skipped without an instrument (LEAD-1618).
    A failed stage also emits one ERROR line; no value is in either."""
    try:  # Best effort: the recorder never replaces the run's own failure or its receipt.
        trace = {k: v for c in clients for k, v in getattr(c, "report_trace", {}).items()}
        failed = {stage: entry for stage, entry in trace.items() if "error" in entry}
        if not failed:
            return trace or None
        parts = [
            f"stage={stage} error={e['error']} first={e.get('first')} status={e.get('status')} "
            f"failures={e.get('failures')} skipped_unloaded={e.get('skipped_unloaded', 0)} "
            f"before_order_history={e.get('before_order_history', 0)} "
            f"unlisted_order={e.get('unlisted_order', 0)} "
            f"quantity_exceeds={e.get('quantity_exceeds', 0)} "
            f"reason={e.get('reason')!r} requests="
            + ",".join(f"{k}:{v.get('requests')}/{v.get('rows')}" for k, v in e["requests"].items())
            for stage, e in failed.items()
        ]
        line = f"order node ERROR startup reconciliation failed: {'; '.join(parts)}"
    except Exception as fault:
        trace = {"recorder_error": type(fault).__name__}
        line = f"order node ERROR startup reconciliation failed: {trace}"
    with suppress(Exception):
        print(line, file=sys.stderr)
    return trace


def journal_page(reason: str, _symbol: str | None, detail: str) -> None:
    """The log's own failure pages (an invalid or unwritten row) go to the unit's journal."""
    print(f"order node page {reason} {detail}", file=sys.stderr, flush=True)


def take_leases(held: ExitStack, args: OrderArguments) -> None:
    """The account's one writer (custody.py ``WriterLease``, the old writer's scope) and the
    key's one market-data stream (producer.py ``reserve_stream``), both for the whole run, each
    under its own holder's root so neither exclusion is lost to a shared directory."""
    from sextant.tradingview.producer import reserve_stream

    scope = AccountScope(args.account.broker, args.account.account_id)
    lease = WriterLease(scope, args.writer_lease_root, WRITER)
    try:
        lease.acquire()
    except CustodyError as error:
        raise OrderModeRefused("writer_lease", str(error)) from error
    held.callback(lease.close)
    try:
        held.enter_context(
            reserve_stream(
                args.stream_lease_root,
                paper_slot=2,
                existing_streams=args.existing_streams,
                source=args.lease_source,
            )
        )
    except (ValueError, CustodyError) as error:
        raise OrderModeRefused("market_data_lease", str(error)) from error


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse(argv)
    except OrderModeRefused as refusal:
        print(f"order mode refused: {refusal}", flush=True)
        return 2
    _, code = run(args, consumer=None, leases=take_leases)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""Alpaca paper ExecutionClient following NT d53849b's Python adapter template.

AP cc4cb3b7 trading/{client,stream}.py and common/rest.py; venue order,
replace, activities and websocket references read 2026-11-10. Replacement
proposal/confirmation follows NT crates/adapters/architect_ax/src/execution.rs.
House namespace, executing-exit guard and pacing: ADR-0007/0009, LEAD-2201.
Core owns trading state. This client retains venue identity evidence only.
"""

import asyncio
import functools
import os
import re
import weakref
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Order, Position
from alpaca.trading.stream import TradingStream
from nautilus_trader.common import Logger
from nautilus_trader.core import dt_to_unix_nanos, unix_nanos_to_iso8601
from nautilus_trader.live import LiveExecutionEngineConfig, SubmissionRecoveryPolicy
from nautilus_trader.live.clients import ExecutionClient
from nautilus_trader.model import (
    AccountBalance,
    ClientOrderId,
    ExecutionMassStatus,
    Money,
    OrderStatus,
    VenueOrderId,
)

from sextant.execution.pacer import AccountPacer, ActionKind
from sextant.risk.controls import Controls

from .reports import _decimal, fill_report, order_status_report, position_status_report
from .wire import AccountGateway, guard_mutation, house_id, modify_payload, submit_payload


class IncompleteCoverage(RuntimeError):
    """Retain good reports and original failed rows; never return a partial list."""

    def __init__(self, reports, failures):
        self.reports, self.failures = reports, failures
        named = "; ".join(str(error) for _, error in failures if "contract" in str(error))
        super().__init__(
            "Alpaca reconciliation coverage incomplete" + (f": {named}" if named else "")
        )


# The startup trace (FAILED-START-1.md): the engine's composer keeps only the outer context of
# a generator's error (common/clients/execution.rs:543-561; python/node.rs:676), so each stage
# records its own counts and first failure. Only these texts of ours are copied: they name no
# symbol, price, quantity, id or key. A venue body or a validation error is never copied.
_STAGE: ContextVar[str | None] = ContextVar("alpaca_report_stage", default=None)
_REASONS = (
    "historical fill that no order report explains",
    "order pages are not contiguous",
    "order page made no progress",
    "order listing needs more requests than the registered request budget",
    "fill activity pages exceed budget",
    "Alpaca ",
    "replacement ",
    "report instrument is unavailable",
    "venue quantity exceeds instrument size precision",
)


_FIELDS = frozenset({"id", "order_id", "cum_qty", "filled_qty", "qty", "status", "symbol"})
_FIELDS |= {"client_order_id", "submitted_at", "replaces", "replaced_by", "side", "type"}


def _reason(error: BaseException) -> str | None:
    key = error.args[0] if isinstance(error, KeyError) and error.args else None
    if isinstance(key, str) and key in _FIELDS:  # Args may be unhashable.
        return f"missing field {key}"
    text = str(error) if type(error).__module__.startswith(("sextant.", "builtins")) else ""
    return text[:200] if text.startswith(_REASONS) else None


def _traced(stage: str):
    """Record a report generator's requests, rows and first failure in ``report_trace``."""

    def wrap(method):
        @functools.wraps(method)
        async def traced(self, command):
            entry = {
                "requests": {},
                "failures": 0,
                "skipped_unloaded": 0,
                "before_order_history": 0,
                "unlisted_order": 0,
                "quantity_exceeds": 0,
            }
            token = None
            try:
                self.report_trace[stage] = entry
                token = _STAGE.set(stage)
            except BaseException as fault:
                entry["recorder_error"] = type(fault).__name__
            try:
                return await method(self, command)
            except BaseException as exc:
                try:  # Observation includes cancellation; the generator's error still propagates.
                    entry.update(error=type(exc).__name__, failures=1)
                    failures = exc.failures if isinstance(exc, IncompleteCoverage) else []
                    first = failures[0][1] if failures else exc
                    status = getattr(first, "status_code", None)
                    entry.update(
                        first=type(first).__name__,
                        status=status if isinstance(status, int) else None,
                        failures=len(failures) or 1,
                        reason=_reason(first),
                    )
                except BaseException as fault:
                    entry["recorder_error"] = type(fault).__name__
                raise
            finally:
                try:
                    if token is not None:
                        _STAGE.reset(token)
                    if entry["skipped_unloaded"]:
                        Logger("AlpacaExecution").warning(
                            f"reconciliation {stage}: skipped_unloaded={entry['skipped_unloaded']}"
                        )
                except BaseException as fault:
                    entry["recorder_error"] = type(fault).__name__

        return traced

    return wrap


def execution_engine_config(*, universe, **kwargs) -> LiveExecutionEngineConfig:
    """LEAD-2201 B4/B5 and item 8; manager.rs at d53849b gates synthetic alignment here.

    Lead ruling 2026-11-11: reconciliation is limited to the house universe
    (node_config.rs:444-445; an empty list would reconcile every instrument).
    """
    scope = [str(instrument_id) for instrument_id in universe]
    if not scope:
        raise ValueError("reconciliation requires the house universe")
    if kwargs.get("reconciliation_lookback_mins") is not None:
        # E5 u9: a holding older than a window would be unexplained and abort startup;
        # the paged listing reads the whole history instead (node_config.rs:442-443).
        raise ValueError("reconciliation lookback must stay unbounded")
    # An unacknowledged submission may be live: the engine retains it rather than rejecting
    # it when its recovery queries run out (submission.rs:45-50). The per-client switch
    # (common clients/execution.rs:120) has no Python binding at the pin.
    retain = SubmissionRecoveryPolicy.RETAIN_UNRESOLVED
    if kwargs.setdefault("submission_recovery_policy", retain) != retain:
        raise ValueError("submission recovery must retain unresolved orders")
    # NT d53849b manager.rs:1935-1939 cancels pending mutations on in-flight timeout,
    # even when their outcome is unknown. Use its open-order reconciliation instead:
    # failed clients retain coverage gaps (2430-2472), and missing pending mutations
    # are deferred (3614-3642). node/mod.rs:1430-1466 selects these native loops.
    # Reuse the caller's cadence (or the engine default), without a new timing value.
    interval = LiveExecutionEngineConfig(**kwargs).inflight_check_interval_ms
    kwargs["inflight_check_interval_ms"] = 0
    if interval:
        cadence = kwargs.setdefault("open_check_interval_secs", interval / 1000)
        if cadence is None or not 0 < cadence < float("inf"):
            raise ValueError("open-order reconciliation must replace in-flight checking")
    return LiveExecutionEngineConfig(
        generate_missing_orders=False,
        filter_position_reports=False,
        filter_unclaimed_external_orders=True,
        reconciliation_instrument_ids=scope,
        **kwargs,
    )


async def _deliver_update(reference, update):
    # Dereference only on the node loop; no client or traceback crosses to SDK thread.
    owner = reference()
    if owner is not None:
        try:
            await owner._on_trade_update(update)
        except IncompleteCoverage as exc:
            owner.failures.extend((row, error.with_traceback(None)) for row, error in exc.failures)


def _observe(stream, reference, loop):
    """Report each socket the SDK opens and each subscription the venue confirms.

    alpaca-py sends the listen request without awaiting the venue's acknowledgement, drops it
    in its dispatcher and reconnects without telling its caller (stream.py:117-126, 141-145,
    164-168, 204-241); the ack is {"stream": "listening", "data": {"streams":
    ["trade_updates"]}} (websocket-streaming page, read 2026-11-11). Weak, as the callback.
    """
    dispatch, start = stream._dispatch, stream._start_ws

    def notify(kind):
        if not loop.is_closed():
            loop.call_soon_threadsafe(lambda: (owner := reference()) and owner._on_stream(kind))

    async def observed_dispatch(msg):
        if msg.get("stream") == "listening":
            if "trade_updates" in (msg.get("data") or {}).get("streams", ()):
                notify("listening")
        await dispatch(msg)

    async def observed_start():
        notify("socket")
        await start()

    stream._dispatch, stream._start_ws = observed_dispatch, observed_start


def _run_stream(stream, owner, ready, closing):
    async def run():
        task = asyncio.current_task()
        owner.call_soon_threadsafe(ready.set_result, (asyncio.get_running_loop(), task))
        try:
            if not closing.is_set():
                await stream._run_forever()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await stream.close()
            finally:
                stream._trade_updates_handler = None

    asyncio.run(run())


def _bounds(command):
    params = {}
    for field, name in (("start", "after"), ("end", "until")):
        value = getattr(command, field, None)
        if value is not None:
            params[name] = unix_nanos_to_iso8601(value)
    if getattr(command, "instrument_id", None) is not None:
        params["symbols"] = command.instrument_id.symbol.value
    return params


# Alpaca order statuses that end an order (orders-at-alpaca, read 2026-11-10).
_TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})


@dataclass(eq=False)
class _Command:
    """One house submit, modify or cancel, registered when its hook schedules it.

    ``sent`` is set only by ``_mark_sent``, the gateway's on-wire hook, before the request
    leaves; that is the one place on-wire evidence is recorded. Afterwards only an
    authoritative answer resolves it: the venue's response, a documented refusal or a read.
    """

    kind: str
    native: Any
    venue_order_id: Any = None
    sent: bool = False
    withdrawn: bool = False  # Pacer admission refuses it from here on.
    settled: bool = False  # A refusal or a venue answer was applied once.
    submitted: bool = False
    row_id: str | None = None  # The venue order a DELETE or a PATCH targets.
    wire_id: str | None = None  # A PATCH's proposal key.
    root: str | None = None  # A submit's engine client order id.
    ended: int = 0  # The tick when its own HTTP call returned or raised; 0 while it runs.


# Every trade_updates event the client classifies: alpaca-py TradeEvent at the pin
# (enums.py:321-333) plus the documented status and refusal events (Alpaca streaming,
# read 2026-11-10). Anything else, "restated" included, is incomplete coverage.
_INGRESS = frozenset(
    {
        "new",
        "accepted",
        "rejected",
        "canceled",
        "expired",
        "done_for_day",
        "replaced",
        "pending_new",
        "stopped",
        "pending_cancel",
        "pending_replace",
        "calculated",
        "suspended",
        "accepted_for_bidding",
        "held",
        "pending_review",
        "fill",
        "partial_fill",
        "order_replace_rejected",
        "order_cancel_rejected",
    }
)
# Events after which no cancel of the order can still act (Alpaca order statuses).
_TERMINAL_EVENTS = frozenset({"fill", "canceled", "expired", "rejected", "replaced"})


def _merge_listings(*listings) -> list:
    """Merge listings by venue id; a row with no string id goes on to its own row boundary."""
    keyed: dict[str, Any] = {}
    loose: list = []
    for row in (row for listing in listings for row in listing):
        key = row.get("id") if isinstance(row, dict) else None
        if isinstance(key, str):
            keyed[key] = row
        else:
            loose.append(row)
    return [*keyed.values(), *loose]


# The broker's documented refusals after send (reference pages, read 2026-11-11): POST
# /v2/orders 403 and 422, DELETE /v2/orders/{order_id} 422; PATCH documents only 200. Any
# other answer is ambiguous (engine failure.rs:28-42): the order may be live, so it is never
# rejected locally; a POST's 429 too, as Coinbase's (execution.rs:1473-1478), since the
# engine retains an unanswered submission (manager.rs:421-460). A PATCH's or DELETE's 429 is
# a refusal, as Bybit's (execution.rs:2389-2411): the engine keeps no pending mutation and
# cancels it once its queries run out (manager.rs:1935-1939), which our 429 pause makes
# likely; alpaca-py itself resends after a 429 (rest.py:198-204), as not processed.
_REFUSALS = {
    "submit": frozenset({403, 422}),
    "cancel": frozenset({422, 429}),
    "modify": frozenset({429}),
}


def _unresolved(exc, token) -> bool:
    """After send, only a documented venue refusal resolves a command locally.

    alpaca-py rest.py:197-210 raises APIError for 4xx/5xx and returns
    response.json() on 2xx (requests' JSONDecodeError, a ValueError); the session
    call raises requests' RequestException family (ConnectionError, Timeout,
    ChunkedEncodingError, ...). None of these, nor any other exception after the
    gateway's on-wire token, proves the venue did not act. Before it, nothing left.
    """
    if not token.sent:
        return False
    status = exc.status_code if isinstance(exc, APIError) else None
    return status not in _REFUSALS[token.kind]


class AlpacaExecutionClient(ExecutionClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.house_prefix = getattr(self.config, "house_prefix", "")
        self.gateway: AccountGateway | None = None
        self.coverage_complete = False
        self._active: dict[str, str] = {}
        self._roots: dict[str, str] = {}
        self._proposals: dict[str, tuple[str, str]] = {}
        self._sequence: dict[str, int] = {}
        self.failures: list[tuple[object, Exception]] = []
        self.report_trace: dict[str, dict[str, Any]] = {}  # per stage, counts only
        # Owner rows the engine would skip anyway; kept with payload and reason.
        self.withheld: list[tuple[dict, str]] = []
        self.withheld_positions: list[tuple[dict, str]] = []
        self._executing: set[str] = set()
        self._cancels: set[str] = set()  # Venue ids with a sent, unanswered DELETE.
        self._orders_complete = False
        self._stream = None
        self._stream_task = None
        self._stream_ready = None
        self._closing = Event()
        self._listening: asyncio.Future | None = None  # The venue's first listen ack.
        self.stream_live = False  # trade_updates confirmed on the current socket
        self.stream_restarts = 0  # sockets the SDK replaced after one was lost
        self.stream_ended = False  # the stream ended while the client was not closing
        self._actions: set[asyncio.Task] = set()
        self._submissions: dict[str, _Command] = {}
        self._commands: set[_Command] = set()
        self.late_answers = 0  # Venue answers to a command an earlier answer settled.
        self._active_n: dict[str, int] = {}  # Successor number of each root's active order.
        self._pending: dict[str, _Command] = {}  # venue id -> its one unresolved DELETE
        self._owed: dict[str, int] = {}  # stream refusals still owed to read-settled attempts
        self._cancel_seen: set[str] = set()  # venue ids that ever had a DELETE on the wire
        self._tick = 0
        self._patches: dict[str, _Command] = {}  # proposal key -> its PATCH on the wire
        self._modify_owed: dict[str, int] = {}  # the same debt for replace refusals
        self._modify_seen: set[str] = set()
        self._held: dict[str, list[dict]] = {}  # successor fills awaiting lineage
        self._releasing: set[str] = set()  # venue ids whose held fills are being forwarded
        self._forwarded: set[tuple[str, str]] = set()  # (venue id, execution id)
        self.duplicate_executions = 0
        # No invented commission: caller supplies an evidenced Money per execution.
        self.commission_for_fill: Callable[[dict], Money] | None = None
        self.commission_refusal: str | None = None

    def _queue_action(self, operation, name, token: _Command | None = None):
        # NT's Python client queue is FIFO; await no wire receipt in its hook.
        # The pinned AX adapter likewise spawns supervised command work.
        if self._closing.is_set():
            operation.close()
            if token is not None:  # Nothing left: refuse rather than leave it pending.
                self._refuse(token, f"{token.kind} not sent: execution client is closing")
            raise RuntimeError("execution client is closing")
        try:
            task = self.create_task(operation, name)
        except BaseException:
            operation.close()
            if token is not None:  # No task exists, so no done callback can refuse it.
                self._refuse(token, f"{token.kind} not sent: scheduling failed")
            raise
        self._actions.add(task)
        task.add_done_callback(self._actions.discard)
        if token is not None:
            self._commands.add(token)
            task.add_done_callback(lambda done: self._task_done(token, done))

    def _task_done(self, token: _Command, task) -> None:
        # Any cancellation (disconnect, node stop, a timeout wrapper, runner teardown),
        # even before the coroutine's first step, ends here; only unsent work is refused.
        self._commands.discard(token)
        if task.cancelled() and not token.sent:
            self._refuse(token, f"{token.kind} not sent before cancellation")

    def _mark_sent(self, token: _Command) -> None:
        """The send boundary: the one place on-wire evidence is recorded."""
        token.sent = True
        if token.kind == "cancel" and token.row_id is not None:
            self._cancels.add(token.row_id)
            self._pending[token.row_id] = token
            self._cancel_seen.add(token.row_id)
        if token.kind == "modify" and token.wire_id is not None:
            self._patches[token.wire_id] = token
            self._modify_seen.add(str(token.row_id))

    def _now(self) -> int:
        self._tick += 1
        return self._tick

    def _resolve(self, row_id, *, issued=None) -> _Command | None:
        """Answer this order's one unresolved cancel. A terminal or refusal event answers it at
        once; a read only when the DELETE's own call ended before the read was issued."""
        token = self._pending.get(row_id)
        if token is None or (issued is not None and not 0 < token.ended < issued):
            return None
        self._drop(token)
        return token

    def _drop(self, token: _Command) -> None:
        token.settled = True
        if self._pending.get(str(token.row_id)) is token:
            del self._pending[str(token.row_id)]
            self._cancels.discard(str(token.row_id))

    def _answer_patches(self, row_id, *, issued=None) -> set[str]:
        """The same rule for a PATCH (one per order: modifies are serialized): a refusal or a
        confirmed replacement answers it at once; a read only after its own call ended."""
        keys = [k for k, (_, parent) in self._proposals.items() if parent == row_id]
        keys = [k for k in keys if k in self._patches]  # Unsent ones stay reserved.
        if issued is not None:
            keys = [k for k in keys if 0 < self._patches[k].ended < issued]
        for key in keys:
            self._patches.pop(key).settled = True
        return {self._proposals.pop(key)[0] for key in keys}

    def _answered(self, kind: str, key: str | None) -> None:
        """An authoritative answer settles the live sent command it answers, once.

        Whichever comes first (the venue's response, a stream event or a read) settles
        the command; a later answer for it emits nothing (review e5r3-99ac5bf).
        """
        for token in self._commands:
            if (
                token.kind == kind
                and token.sent
                and key in (token.row_id, token.wire_id, token.root)
            ):
                token.settled = True

    def _refuse(self, token: _Command, reason: str) -> None:
        """Apply a definite refusal once: nothing left, or the venue said no (4xx).

        Refusing an unsent command withdraws it in the same act, so a request it left queued
        in the pacer is dropped at admission and never reaches the wire (review of PR 552)."""
        if not token.sent:
            token.withdrawn = True
        if token.settled:
            if token.sent:
                self.late_answers += 1  # An earlier authoritative answer already settled it.
            return
        token.settled = True
        if token.wire_id is not None:
            self._proposals.pop(token.wire_id, None)
            self._patches.pop(token.wire_id, None)
        if token.row_id is not None:
            self._drop(token)
        native, now = token.native, self.clock.timestamp_ns()
        if native is None:
            return
        if token.kind == "submit":
            if token.submitted:
                self.generate_order_rejected(native, reason, now, False)
            else:
                self.generate_order_denied(native, reason)
        elif token.kind == "modify":
            self.generate_order_modify_rejected(native, token.venue_order_id, reason, now)
        else:
            self.generate_order_cancel_rejected(native, token.venue_order_id, reason, now)

    def _command(self, kind: str, command) -> _Command:
        native = command.order if kind == "submit" else self.cache.order(command.client_order_id)
        return _Command(kind, native, getattr(command, "venue_order_id", None))

    async def _submit_order(self, command):
        token = self._command("submit", command)
        self._queue_action(self._do_submit_order(command, token), "alpaca-submit", token)

    async def _modify_order(self, command):
        token = self._command("modify", command)
        self._queue_action(self._do_modify_order(command, token), "alpaca-modify", token)

    async def _cancel_order(self, command):
        token = self._command("cancel", command)
        self._queue_action(self._do_cancel_order(command, token), "alpaca-cancel", token)

    async def _query_order(self, command):
        self._queue_action(self._do_query_order(command), "alpaca-query-order")

    async def _query_account(self, command):
        self._queue_action(self._do_query_account(command), "alpaca-query-account")

    async def _connect(self):
        config: Any = self.config
        house_id(self.house_prefix, self.house_prefix)
        if config.pacing_rows is None:
            raise ValueError("execution pacing rows unavailable")
        controls = Controls.from_rows(config.pacing_rows)
        if not callable(self.commission_for_fill):
            refusal = f": {self.commission_refusal}" if self.commission_refusal else ""
            raise ValueError(f"fill reports unavailable: commission evidence required{refusal}")
        key, secret = os.environ[config.api_key_env], os.environ[config.api_secret_env]
        sdk = TradingClient(key, secret, paper=True, raw_data=True)
        self.gateway = AccountGateway(
            sdk, AccountPacer(controls, self.clock.timestamp_ns), self._position_counts
        )
        await self._do_query_account(None)
        self._stream = TradingStream(key, secret, paper=True, raw_data=True)
        loop = asyncio.get_running_loop()
        reference = weakref.ref(self)

        async def receive(update):
            # The SDK owns its own loop; every cache/output access stays on node loop.
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(_deliver_update(reference, update), loop)
            )

        self._stream.subscribe_trade_updates(receive)
        self._closing.clear()
        self._stream_ready, self._listening = loop.create_future(), loop.create_future()
        self.stream_live = self.stream_ended = False
        _observe(self._stream, reference, loop)
        self._stream_task = self.create_task(self._run_trade_stream(loop), "trade-updates")

    async def _run_trade_stream(self, loop):
        try:
            await asyncio.to_thread(
                _run_stream, self._stream, loop, self._stream_ready, self._closing
            )
        finally:
            if not self._closing.is_set():
                self.stream_ended = True
                self._stream_lost("Alpaca trade stream ended unexpectedly")

    def _on_stream(self, kind: str) -> None:
        if kind == "listening":
            if self._listening is not None and not self._listening.done():
                self._listening.set_result(None)
            elif not self.stream_live:  # Resubscribed after a loss: read the gap back.
                self.create_task(self._recover(), "alpaca-recovery")
            self.stream_live = True
        elif self.stream_live:  # A new socket: the one the SDK had was lost.
            self.stream_restarts += 1
            self._stream_lost("Alpaca trade stream lost; the SDK is reconnecting")

    async def _recover(self) -> None:
        """Hand the engine one mass status of what the stream missed, as Derive does after each
        reconnect (execution.rs:2563-2575); the engine reconciles it as at startup
        (engine/mod.rs:1119-1121). Fills are the engine's inference from the order reports:
        this client's fill history stays unqualified (``_generate_fill_reports``)."""
        # The engine's report commands have no Python constructor (live/__init__.pyi:543-582);
        # this one carries their fields: the whole history, as the startup composer reads it.
        scope: Any = SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None)
        # Stamped before the first await, as the composer does (python/client/mod.rs:2118-2120):
        # a fill the stream delivers during the reads stays newer than the snapshot.
        ts_init, socket = self.clock.timestamp_ns(), self.stream_restarts
        try:
            orders = await self._generate_order_status_reports(scope)
            positions = await self._generate_position_status_reports(scope)
        except Exception as exc:
            self.coverage_complete = False
            if not isinstance(exc, IncompleteCoverage):  # Its rows are already kept.
                self.failures.append((None, exc))
            Logger("AlpacaExecution").error(f"Alpaca trade stream recovery failed: {exc}")
            return
        if self.stream_restarts != socket:  # Lost again during the reads: the next ack reads.
            self.coverage_complete = False
            return
        venue: Any = self.venue  # Set at construction (live/clients.py); never None here.
        mass = ExecutionMassStatus(self.client_id, self.account_id, venue, ts_init)
        mass.add_order_reports(orders)
        mass.add_position_reports(positions)
        self._handle_report(mass)

    def _stream_lost(self, reason: str) -> None:
        """Visible at once, as the official Derive client does (execution.rs:805-807, 842-846)."""
        self.stream_live = self.coverage_complete = False
        Logger("AlpacaExecution").error(reason)

    async def _subscribed(self) -> None:
        """Startup reads follow the venue's confirmation of trade_updates, as Hyperliquid reads
        history only after it (execution.rs:3114-3120), so nothing between a read and the
        subscription is lost. The engine's reconciliation timeout bounds the wait; before
        connect there is no stream to wait for."""
        if self._listening is not None:
            await asyncio.shield(self._listening)

    async def _disconnect(self):
        # Disconnect never cancels orders or closes positions.
        self.coverage_complete = self.stream_live = False
        self._closing.set()
        for token in tuple(self._commands):
            if not token.sent:
                # Positive not-sent evidence: the admission check now refuses it.
                token.withdrawn = True
                self._refuse(token, f"{token.kind} not sent before disconnect")
        actions = tuple(self._actions)
        for action in actions:
            action.cancel()
        if actions:
            await asyncio.gather(*actions, return_exceptions=True)
        if self._stream_task is not None and self._stream_ready is not None:
            if not self._stream_task.done():
                loop, task = await asyncio.shield(self._stream_ready)
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)
            await asyncio.shield(self._stream_task)
            self._stream_task = self._stream_ready = None
        if self.gateway is not None:
            await self.gateway.close()

    def _position_counts(self):
        opened = len(self.cache.positions_open(venue=self.venue, account_id=self.account_id))
        exiting = len(
            {
                o.instrument_id
                for o in self.cache.orders_open(venue=self.venue, account_id=self.account_id)
                if o.is_sell
            }
        )
        return opened, exiting

    async def _call(self, kind, method, path, data=None, **kwargs) -> Any:
        if self.gateway is None:
            raise RuntimeError("account gateway is disconnected")
        stage, count = _STAGE.get(), None
        try:  # Best effort: counting never changes the call's result or exception.
            if stage is not None:
                name = "/orders/{id}" if path.startswith("/orders/") else path  # Never an id.
                count = self.report_trace[stage]["requests"].setdefault(
                    name, {"requests": 0, "rows": 0}
                )
                count["requests"] += 1
        except BaseException as fault:
            count = None
            with suppress(BaseException):
                if stage is not None:
                    self.report_trace[stage]["recorder_error"] = type(fault).__name__
        result = await self.gateway.call(kind, method, path, data, **kwargs)
        if count is not None:
            try:
                count["rows"] += len(result) if isinstance(result, list) else 1
            except BaseException as fault:
                with suppress(BaseException):
                    if stage is not None:
                        self.report_trace[stage]["recorder_error"] = type(fault).__name__
                    count["rows"] = None
        return result

    def _instrument_id(self, row):
        from nautilus_trader.model import InstrumentId

        return InstrumentId.from_str(f"{row['symbol']}.{self.venue}")

    def _instrument(self, row):
        instrument = self.cache.instrument(self._instrument_id(row))
        if instrument is None:
            raise ValueError("report instrument is unavailable")
        return instrument

    def _unloaded_history(self, row, stage: str):
        # NT d53849b spot/execution.rs:3542-3545: warn and skip uncached reports.
        # Only terminal history may be withheld; working rows must stop the start.
        if self.cache.instrument(self._instrument_id(row)) is not None:
            return False
        pending = [row]  # alpaca-py cc4cb3b7 models.py:241: legs are recursive Orders.
        while pending:
            part = pending.pop()
            if part.get("status") not in _TERMINAL:
                raise ValueError("report instrument is unavailable for non-terminal order")
            pending.extend(part.get("legs") or [])
        self.report_trace[stage]["skipped_unloaded"] += 1
        return True

    async def _identity(self, row):
        wire_id, venue_id = row["client_order_id"], row["id"]
        if not wire_id.startswith(self.house_prefix) or not self.house_prefix:
            return ClientOrderId(wire_id)
        root = re.sub(r"-r[0-9]+$", "", wire_id)
        suffix = re.search(r"-r([0-9]+)$", wire_id)
        number = int(suffix[1]) if suffix else 0
        # House successors are numbered r1, r2, ...: an older number is never bound again.
        stale = number < self._active_n.get(root, -1)
        if row.get("replaces"):
            # An owner successor never gains house authority from a parent.
            issued = self._now()
            parent = await self._call(ActionKind.QUERY, "get", f"/orders/{row['replaces']}")
            if (
                not parent["client_order_id"].startswith(self.house_prefix)
                or re.sub(r"-r[0-9]+$", "", parent["client_order_id"]) != root
                or parent.get("replaced_by") != venue_id
                or parent["status"] != "replaced"
                or row["status"] == "pending_new"  # Confirmed ones may be replaced again.
            ):
                raise ValueError("replacement lacks venue confirmation")
            proposal = self._proposals.get(wire_id)
            if proposal is not None and proposal != (root, parent["id"]):
                raise ValueError("replacement differs from local proposal")
            # Re-read after the await: a newer successor may have bound.
            stale = number < self._active_n.get(root, -1)
            previous = self._active.get(root)
            # Superseded: the venue named a successor, or a newer one is bound.
            superseded = (
                stale
                or bool(row.get("replaced_by"))
                or (venue_id in self._roots and previous not in (None, venue_id))
            )
            self._roots[parent["id"]] = root
            self._settle(parent, issued)
            if previous != venue_id and not superseded:
                native = self.cache.order(ClientOrderId(root))
                if native is not None:
                    report = self._parse_order(row, ClientOrderId(root))
                    self.generate_order_updated(
                        native,
                        VenueOrderId(venue_id),
                        report.quantity,
                        report.price,
                        report.trigger_price,
                        None,
                        report.ts_last,
                    )
        elif wire_id != root:
            raise ValueError("replacement namespace requires parent evidence")
        known_predecessor = stale or (
            venue_id in self._roots and self._active.get(root) not in (None, venue_id)
        )
        if not row.get("replaced_by") and not known_predecessor:
            self._active[root], self._active_n[root] = venue_id, number
        self._roots[venue_id] = root
        self._sequence[root] = max(self._sequence.get(root, 0), number)
        await self._release(venue_id)  # Every confirming path releases.
        return ClientOrderId(root)

    def _settle(self, row, issued: int):
        """Resolve proposals against this order from an authoritative venue read.

        Alpaca: replaced is terminal and pending_replace still waits; any other status
        proves no replacement pending, so a lost PATCH reply cannot block later modifies.
        NT manager.rs:3614-3642 defers unresolved pending mutations during recovery.
        """
        if row["status"] in ("pending_replace", "pending_cancel"):
            return
        applied = row["status"] == "replaced" or bool(row.get("replaced_by"))
        # A confirmed replacement answers its PATCH at once; a read answers only attempts
        # whose own call ended before it was issued (PR 569 review).
        dropped = self._answer_patches(row["id"], issued=None if applied else issued)
        cancel_lost = self._resolve(row["id"], issued=issued)
        if applied:
            return  # Applied: the successor binding updates the engine.
        # Unchanged at the venue: this authoritative read answers the pending mutation.
        now, venue_id = self.clock.timestamp_ns(), VenueOrderId(row["id"])
        for root in dropped:
            native = self.cache.order(ClientOrderId(root))
            if getattr(native, "status", None) == OrderStatus.PENDING_UPDATE:
                self._modify_owed[row["id"]] = self._modify_owed.get(row["id"], 0) + 1
                self.generate_order_modify_rejected(
                    native, venue_id, "venue order unchanged after unanswered modify", now
                )
        root = self._roots.get(row["id"])
        if cancel_lost and root is not None and row["status"] not in _TERMINAL:
            native = self.cache.order(ClientOrderId(root))
            if getattr(native, "status", None) == OrderStatus.PENDING_CANCEL:
                # The venue's own refusal for it may still arrive: it is owed, and answers nothing.
                self._owed[row["id"]] = self._owed.get(row["id"], 0) + 1
                self.generate_order_cancel_rejected(
                    native, venue_id, "venue order still working after unanswered cancel", now
                )

    def _parse_order(self, row, stable):
        # E3 validates each row independently; a typed list is never requested.
        return order_status_report(
            Order.model_validate(row),
            account_id=self.account_id,
            instrument=self._instrument(row),
            ts_init=self.clock.timestamp_ns(),
            client_order_id=stable,
        )

    async def _report(self, row, stable=None):
        stable = stable or await self._identity(row)
        self._answered("submit", str(stable))  # The venue holds the order: the POST is answered.
        if row["status"] == "replaced":
            child = await self._call(ActionKind.QUERY, "get", f"/orders/{row['replaced_by']}")
            if child["client_order_id"].startswith(self.house_prefix):
                await self._identity(child)
                if self._active.get(str(stable)) == child["id"]:
                    return None  # Proven predecessor; E3 otherwise refuses replaced rows.
        if self._roots.get(row["id"]) == str(stable) and self._active.get(str(stable)) != row["id"]:
            return None  # Suppress stale predecessor state, not its execution reports.
        return self._parse_order(row, stable)

    def _mutation_admitted(self, command, row):
        native = self.cache.order(command.client_order_id)
        return (
            row["id"] not in self._executing
            and self._active.get(str(command.client_order_id)) == row["id"]
            and not (
                native is not None
                and (
                    getattr(native, "is_triggered", False) is True
                    or (
                        getattr(native, "is_sell", False) is True
                        and native.filled_qty.as_decimal() > 0
                    )
                )
            )
        )

    async def _current(self, command):
        issued = self._now()
        root = house_id(str(command.client_order_id), self.house_prefix)
        venue = self._active.get(root)
        path = f"/orders/{venue}" if venue else "/orders:by_client_order_id"
        row = await self._call(
            ActionKind.QUERY, "get", path, {} if venue else {"client_order_id": root}
        )
        if row.get("replaced_by"):
            row = await self._call(ActionKind.QUERY, "get", f"/orders/{row['replaced_by']}")
        if str(await self._identity(row)) != root:
            raise ValueError("venue order is outside requested house namespace")
        self._settle(row, issued)
        self._parse_order(row, command.client_order_id)
        guard_mutation(row, self.house_prefix)
        if not self._mutation_admitted(command, row):
            raise ValueError("executing exit cannot be mutated")
        return row

    async def _do_submit_order(self, command, token: _Command | None = None):
        native = command.order
        token = token or _Command("submit", native)
        try:
            payload = submit_payload(native, command.params or {}, self.house_prefix)
        except ValueError as exc:
            token.settled = True
            self.generate_order_denied(native, str(exc))
            return
        self.generate_order_submitted(native)
        token.submitted = True
        root = token.root = str(native.client_order_id)
        self._submissions[root] = token
        kind = ActionKind.REDUCE if native.side.name == "SELL" else ActionKind.SUBMIT
        try:
            row = await self._call(
                kind,
                "post",
                "/orders",
                payload,
                admission=lambda: not token.withdrawn,
                on_wire=lambda: self._mark_sent(token),
            )
        except Exception as exc:
            if _unresolved(exc, token):
                raise  # Sent: the venue's later status decides, never a local reject.
            sent = token.sent
            self._refuse(
                token, "venue submit refused" if sent else "submission refused before wire"
            )
            return
        finally:
            if self._submissions.get(root) is token:
                self._submissions.pop(root)
        token.settled = True  # The venue's response answers the submit.
        report = await self._report(row)
        if report is not None:
            self._handle_report(report)

    async def _do_modify_order(self, command, token: _Command | None = None):
        token = token or self._command("modify", command)
        try:
            row = await self._current(command)
            root = str(command.client_order_id)
            if any(value[0] == root for value in self._proposals.values()):
                raise ValueError("replacement outcome remains unresolved")
            payload = modify_payload(command, row)
            number = self._sequence.get(root, 0) + 1
            token.wire_id = house_id(f"{root}-r{number}", self.house_prefix)
            token.row_id = row["id"]
            payload["client_order_id"] = token.wire_id
            self._sequence[root] = number
            self._proposals[token.wire_id] = (root, row["id"])  # Reserved; serializes modifies.
            await self._call(
                ActionKind.MODIFY,
                "patch",
                f"/orders/{row['id']}",
                payload,
                admission=lambda: not token.withdrawn and self._mutation_admitted(command, row),
                on_wire=lambda: self._mark_sent(token),
                on_end=lambda: setattr(token, "ended", self._now()),
            )
            # A PATCH response is a proposal, never terminal replacement evidence.
            token.settled = True
        except Exception as exc:
            if _unresolved(exc, token):
                raise  # Sent: kept until an authoritative read settles it.
            self._refuse(token, "modification refused")

    async def _do_cancel_order(self, command, token: _Command | None = None):
        token = token or self._command("cancel", command)
        native = token.native
        try:
            root = house_id(str(command.client_order_id), self.house_prefix)
            pending = self._submissions.get(root)
            if pending is not None and not pending.sent:
                if (
                    native is not None
                    and getattr(native, "is_sell", False) is True
                    and (
                        getattr(native, "is_triggered", False) is True
                        or native.filled_qty.as_decimal() > 0
                    )
                ):
                    raise ValueError("executing exit cannot be mutated")
                pending.withdrawn = pending.settled = token.settled = True
                if native is not None:
                    self.generate_order_canceled(native, None, self.clock.timestamp_ns())
                return
            row = await self._current(command)
            token.row_id = row["id"]
            if row["id"] in self._pending:
                # Single flight: one unresolved cancel per order. Like Coinbase, never re-cancel
                # (a CancelRejected would flip the order back to working, execution.rs:1236-1240).
                token.settled = True
                return
            await self._call(
                ActionKind.CANCEL,
                "delete",
                f"/orders/{row['id']}",
                admission=lambda: not token.withdrawn and self._mutation_admitted(command, row),
                on_wire=lambda: self._mark_sent(token),
                on_end=lambda: setattr(token, "ended", self._now()),
            )
            # Alpaca's DELETE "Attempts to cancel an Open Order" (204): an attempt, not a
            # terminal cancel. The marker stays until a terminal confirmation or a refusal.
            token.settled = True
        except Exception as exc:
            if _unresolved(exc, token):
                raise  # Sent: the venue's later status decides, never a local reject.
            self._refuse(token, "cancellation refused")

    async def _cancel_all_orders(self, command):
        raise NotImplementedError("account-wide cancellation is forbidden")

    async def _do_query_account(self, command: Any):
        row = await self._call(ActionKind.QUERY, "get", "/account")
        # NT IBKR account.rs:397-419 and types/balance.rs:149-172: net value,
        # available funds, then clamp and derive locked after Money rounding.
        total = _decimal(row["equity"], "account equity")
        available = _decimal(row["buying_power"], "account buying power")
        _decimal(row["cash"], "account cash")
        if total >= 0:
            available = min(total, max(Decimal(0), available))
        currency = row["currency"]
        total_money = Money.from_str(f"{total} {currency}")
        free_money = Money.from_str(f"{available} {currency}")
        locked = total_money.as_decimal() - free_money.as_decimal()
        self.generate_account_state(
            [
                AccountBalance(
                    total_money,
                    Money.from_str(f"{locked} {currency}"),
                    free_money,
                )
            ],
            [],
            True,
            self.clock.timestamp_ns(),
            info={"buying_power": row["buying_power"], "cash": row["cash"]},
        )

    async def _generate_order_status_report(self, command: Any):
        issued = self._now()
        venue = self._active.get(str(command.client_order_id)) or command.venue_order_id
        if venue is not None:
            row = await self._call(ActionKind.QUERY, "get", f"/orders/{venue}")
        else:
            row = await self._call(
                ActionKind.QUERY,
                "get",
                "/orders:by_client_order_id",
                {"client_order_id": str(command.client_order_id)},
            )
        visited = set()
        while row.get("replaced_by"):
            if row["id"] in visited:
                raise ValueError("replacement lineage cycle")
            visited.add(row["id"])
            row = await self._call(ActionKind.QUERY, "get", f"/orders/{row['replaced_by']}")
        report = await self._report(row)
        self._settle(row, issued)
        if (
            report is not None
            and command.client_order_id is not None
            and (report.client_order_id != command.client_order_id)
        ):
            raise IncompleteCoverage([], [(row, ValueError("query reached foreign successor"))])
        return report

    async def _do_query_order(self, command):
        report = await self._generate_order_status_report(command)
        if report is not None:
            self._handle_report(report)

    def _owner_withheld(self, row) -> str | None:
        """Why the engine would skip this owner row; None for every house row.

        NT manager.rs:4665-4668 filters unclaimed external orders and 1056-1057
        skips reports without a cached instrument; Alpaca replaced and contingent
        classes have no simple report shape. DESIGN-owner-activity.md, E5 round.
        """
        if not self.house_prefix or row["client_order_id"].startswith(self.house_prefix):
            return None
        if row.get("order_class") not in (None, "", "simple"):
            # Lead ruling 2026-11-11: managed paper is plain LIMIT, no bracket.
            reason = (
                f"owner {row['order_class']} order {row['client_order_id']} ({row['id']}) "
                "is outside the managed-paper contract; startup refused"
            )
            # Built per call: a module-level Logger aborted the pinned node process
            # when the node ran on a non-main thread (witnessed in this round).
            Logger("AlpacaExecution").warning(reason)
            raise ValueError(reason)
        if row["status"] == "replaced" and Decimal(row.get("filled_qty") or "0") == 0:
            return "owner order replaced without fills"
        if self.cache.instrument(self._instrument_id(row)) is None:
            return "owner order instrument unavailable"
        return None

    async def _list_orders(self, params):
        """Read GET /orders completely, newest first, paging by ``until``.

        alpaca-py requests.py:202-213: limit max 500, ``until`` on submission time,
        direction desc; no page token. Each next page must again contain the rows at
        the previous page's oldest instant (so neither reading of ``until`` can skip
        a row) and must add rows. A failed page, a gap, no progress, or more pages
        than the registered request budget is incomplete coverage, never truncation.
        """
        if self.gateway is None:
            raise RuntimeError("account gateway is disconnected")
        budget, limit = self.gateway.pacer.controls["request_budget"], 500
        query: dict[str, Any] = dict(params, limit=limit, direction="desc")
        rows: dict[str, dict] = {}
        unkeyed: list = []  # Rows the pager cannot key or place; each fails in its own row.
        edge: set[str] = set()
        for _ in range(budget):
            try:
                page = await self._call(ActionKind.QUERY, "get", "/orders", query)
            except Exception as exc:
                raise IncompleteCoverage([], [(query, exc)]) from exc
            stamped = {}
            for row in page:
                try:
                    stamped[row["id"]] = (dt_to_unix_nanos(row["submitted_at"]), row)
                except Exception:
                    unkeyed.append(row)
            if not edge <= set(stamped):
                raise IncompleteCoverage(
                    [], [(query, ValueError("order pages are not contiguous"))]
                )
            fresh = {key: row for key, (_, row) in stamped.items() if key not in rows}
            rows.update(fresh)
            if len(page) < limit:
                return [*rows.values(), *unkeyed]
            if not fresh:
                raise IncompleteCoverage([], [(query, ValueError("order page made no progress"))])
            instant = min(ns for ns, _ in stamped.values())
            edge = {key for key, (ns, _) in stamped.items() if ns == instant}
            oldest = next(row for ns, row in stamped.values() if ns == instant)
            query = dict(query, until=oldest["submitted_at"])
        reason = "order listing needs more requests than the registered request budget"
        raise IncompleteCoverage([], [(params, ValueError(reason))])

    @_traced("orders")
    async def _generate_order_status_reports(self, command):
        await self._subscribed()
        issued = self._now()
        self.coverage_complete = self._orders_complete = False
        self.withheld = []
        try:
            rows = await self._list_orders(
                {
                    "status": "open" if command.open_only else "all",
                    "nested": True,
                    **_bounds(command),
                }
            )
            if getattr(command, "start", None) is not None:
                # A submission-time window cannot cover older still-working orders,
                # including the native open-check's default lookback (config.rs:506).
                opened = await self._list_orders({"status": "open", "nested": True})
                rows = _merge_listings(rows, opened)
        except IncompleteCoverage as exc:
            self.failures.extend(exc.failures)
            raise
        reports, failures = [], []
        for row in rows:
            try:
                if self._unloaded_history(row, "orders"):
                    self.withheld.append((row, "terminal order instrument unavailable"))
                    continue
                reason = self._owner_withheld(row)
                if reason is not None:
                    self.withheld.append((row, reason))
                    continue
                report = await self._report(row)
                self._settle(row, issued)
                if report is not None:
                    reports.append(report)
            except Exception as exc:
                failures.append((row, exc))
        failures += [
            (update, ValueError("execution held for its unconfirmed lineage"))
            for held in self._held.values()
            for update in held
        ]
        if failures:
            self.failures.extend(failures)
            raise IncompleteCoverage(reports, failures)
        self._orders_complete = True
        self.coverage_complete = not self._held and not self._releasing
        return reports

    def _provides_bulk_position_coverage(self, instrument_id):
        # A withheld row never authorises a flat inference for its instrument.
        return (
            self.coverage_complete
            and not self._held
            and not self._releasing
            and all(
                self._instrument_id(row) != instrument_id
                for row, _ in (*self.withheld, *self.withheld_positions)
            )
        )

    @_traced("positions")
    async def _generate_position_status_reports(self, command):
        await self._subscribed()
        self.coverage_complete = False
        rows = await self._call(ActionKind.QUERY, "get", "/positions")
        reports, failures = [], []
        self.withheld_positions = []
        for row in rows:
            try:
                if self.cache.instrument(self._instrument_id(row)) is None:
                    # No house order can exist without its instrument; under the ruled
                    # scope the engine skips it (manager.rs:1314-1319). No flat coverage.
                    self.withheld_positions.append((row, "position instrument unavailable"))
                    continue
                report = position_status_report(
                    Position.model_validate(row),
                    account_id=self.account_id,
                    instrument=self._instrument(row),
                    ts_init=self.clock.timestamp_ns(),
                )
                if report is not None:
                    reports.append(report)
            except Exception as exc:
                failures.append((row, exc))
        if failures:
            self.coverage_complete = False
            self.failures.extend(failures)  # Kept, as the order reader keeps its rows.
            raise IncompleteCoverage(reports, failures)
        self.coverage_complete = self._orders_complete and not self._held and not self._releasing
        return reports

    async def _list_fill_activities(self, params):
        """Read GET /account/activities/FILL completely, newest first: page_size at most 100,
        page_token the id of the previous page's last row (account activities reference, read
        2026-11-11). More pages than the registered request budget is incomplete coverage."""
        if self.gateway is None:
            raise RuntimeError("account gateway is disconnected")
        budget, size = self.gateway.pacer.controls["request_budget"], 100
        query: dict[str, Any] = dict(params, page_size=size, direction="desc")
        rows: list = []
        for _ in range(budget):
            page = await self._call(ActionKind.QUERY, "get", "/account/activities/FILL", query)
            rows.extend(page)
            if len(page) < size:
                return rows
            query = dict(query, page_token=page[-1]["id"])
        raise IncompleteCoverage([], [(query, ValueError("fill activity pages exceed budget"))])

    @_traced("fills")
    async def _generate_fill_reports(self, command):
        """No fill report is built from history: a FILL row carries no stream execution id and
        no fee (account activities reference, read 2026-11-11), and none is invented.

        The engine infers each fill an order report explains (create_inferred_fill,
        reconciliation/orders.rs:1065-1145), so a row whose order is listed with at least its
        cumulative quantity needs no report. Unlisted fills before the oldest listed submission
        are counted, not refused; other unexplained fills still refuse. BUILD ON native bounded
        fill reads (d53849b architect_ax/execution.rs:1676-1695). A holding no order explains
        still stops the start in the engine (node/mod.rs:992-996).
        """
        await self._subscribed()
        params = _bounds(command)
        params.pop("symbols", None)  # Activities accepts symbol, singular.
        instrument_id = getattr(command, "instrument_id", None)
        if instrument_id is not None:
            params["symbol"] = instrument_id.symbol.value
        rows = await self._list_fill_activities(params)
        if not rows:
            return []
        listed = {}  # The whole history: a fill in the window may be an older order's.
        for row in await self._list_orders({"status": "all", "nested": True}):
            for order in (row, *(row.get("legs") or ())) if isinstance(row, dict) else ():
                if isinstance(order, dict) and isinstance(order.get("id"), str):
                    listed[order["id"]] = order
        oldest = min(
            (dt_to_unix_nanos(order["submitted_at"]) for order in listed.values()), default=None
        )
        trace = self.report_trace["fills"]
        failures = []
        for row in rows:
            try:
                order = listed.get(row["order_id"])
                stamp = row.get("transaction_time")
                if order is None and oldest is not None and stamp is not None:
                    if dt_to_unix_nanos(stamp) < oldest:
                        trace["before_order_history"] += 1
                        continue
                if order is None or Decimal(order["filled_qty"]) < Decimal(row["cum_qty"]):
                    trace["unlisted_order" if order is None else "quantity_exceeds"] += 1
                    raise ValueError("historical fill that no order report explains")
                self._unloaded_history(order, "fills")  # Same terminal-only rule; no inferred fill.
            except Exception as exc:
                failures.append((row, exc))
        if failures:
            self.coverage_complete = False
            self.failures.extend(failures)
            raise IncompleteCoverage([], failures)
        return []

    async def _on_trade_update(self, update: dict[str, Any]):
        try:
            await self._process_update(update)
        except Exception as exc:
            self.coverage_complete = False
            if isinstance(exc, IncompleteCoverage):
                raise
            raise IncompleteCoverage([], [(update, exc)]) from exc

    async def _release(self, venue_id: str) -> None:
        """Forward, in order, the fills held for this unconfirmed lineage. One that still
        fails stays held with the rest, so coverage stays incomplete."""
        if venue_id in self._releasing:
            return
        self._releasing.add(venue_id)
        held = self._held.pop(venue_id, [])  # A replay's own release finds none.
        try:
            while held:
                await self._process_update(held[0])
                held.pop(0)  # Once processed.
        except Exception as exc:
            self.failures.append((held[0], exc))
            self.coverage_complete = False
        finally:
            self._releasing.discard(venue_id)
            if held:  # Kept, in order, for the next one.
                self._held[venue_id] = held + self._held.get(venue_id, [])

    def _record(self, event: str, row: dict) -> bool:
        """Every safety fact the event names, keyed by its venue order id, synchronously on
        arrival and before any await (review e5r4-266edf5 P1). Returns True for a refusal an
        authoritative read already emitted."""
        if row.get("side") == "sell" and event in ("fill", "partial_fill", "stopped"):
            self._executing.add(row["id"])
        if event in _TERMINAL_EVENTS:
            self._resolve(row["id"])  # Terminal: no attempt can still act; the debt ends.
            self._owed.pop(row["id"], None)
        late = False
        if event == "order_cancel_rejected":  # The broker's refusal names no attempt.
            if self._owed.get(row["id"]):
                self._owed[row["id"]] -= 1  # Paid to a read-settled attempt: answers nothing.
                late = True
            else:
                late = self._resolve(row["id"]) is None and row["id"] in self._cancel_seen
        if event == "order_replace_rejected":
            if self._modify_owed.get(row["id"]):
                self._modify_owed[row["id"]] -= 1
                late = True
            else:
                late = not self._answer_patches(row["id"]) and row["id"] in self._modify_seen
        self.late_answers += late
        return late

    async def _process_update(self, update: dict[str, Any]):
        if update.get("stream") != "trade_updates":
            raise ValueError("raw trade_updates envelope required")
        data = update["data"]
        event, row = data["event"], data["order"]
        if event in ("trade_bust", "trade_correct"):
            self.coverage_complete = False
            raise IncompleteCoverage(
                [], [(update, ValueError("trade correction requires reconciliation"))]
            )
        if event not in _INGRESS:
            self.coverage_complete = False
            raise ValueError("unclassified trade update event")
        late = self._record(event, row)
        # One identity resolution for every event: a successor binds before anything about
        # it is forwarded, and unconfirmed lineage fails here as incomplete coverage.
        try:
            stable = await self._identity(row)
        except ValueError:
            if event in ("fill", "partial_fill") and row.get("replaces"):
                self._held.setdefault(row["id"], []).append(update)  # Released once confirmed.
            raise
        native = self.cache.order(stable)
        now = dt_to_unix_nanos(data.get("timestamp", row["updated_at"]))
        if event in ("order_replace_rejected", "order_cancel_rejected"):
            if native is not None and not late:
                emit = (
                    self.generate_order_modify_rejected
                    if event == "order_replace_rejected"
                    else self.generate_order_cancel_rejected
                )
                emit(native, VenueOrderId(row["id"]), "venue mutation refused", now)
            return
        if event in ("fill", "partial_fill"):
            if self.commission_for_fill is None:
                self.coverage_complete = False
                raise IncompleteCoverage(
                    [], [(update, ValueError("fill commission evidence unavailable"))]
                )
            execution = (row["id"], str(data.get("execution_id")))
            if execution in self._forwarded:  # One forward per execution.
                self.duplicate_executions += 1
                return
            report = fill_report(
                update,
                account_id=self.account_id,
                instrument=self._instrument(row),
                ts_init=self.clock.timestamp_ns(),
                commission=self.commission_for_fill(data),
                client_order_id=stable,
            )
            self._forwarded.add(execution)
            self._handle_report(report)
            return
        if event == "stopped" and native is not None:
            self.generate_order_triggered(native, VenueOrderId(row["id"]), now)
        report = await self._report(row, stable)
        if report is not None:
            self._handle_report(report)

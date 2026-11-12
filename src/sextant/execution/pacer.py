"""Integer-ns account pacer adapted from pinned execution/pacer.py and Alpaca pacing.py.

An action receives the wire hook, called once per actual request by its transport.
Retries and list members are separate queued actions. This module opens no socket.
"""

import heapq
import itertools
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum

from sextant.risk.controls import ENTRY_RESERVE_EXCEEDS_BURST, Controls


class ActionKind(IntEnum):
    CANCEL = 0
    REDUCE = 1
    MODIFY = 2
    QUERY = 3
    SUBMIT = 4


class PacerRefused(RuntimeError):
    """The account pacer did not admit a broker call; the caller must fail closed."""

    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        super().__init__(message or reason)


class TokenBucket:
    def __init__(self, limit: int, window_ns: int, burst: int):
        self.limit, self.window_ns, self.burst = limit, window_ns, burst
        self.interval_ns = window_ns // (limit - burst) + 1
        self.tokens, self.anchor = burst, None

    def refill(self, now: int):
        if self.anchor is None or self.tokens >= self.burst:
            self.anchor = now
            return
        credits = max(0, (now - self.anchor) // self.interval_ns)
        if credits:
            self.tokens = min(self.burst, self.tokens + credits)
            self.anchor = (
                now if self.tokens == self.burst else self.anchor + credits * self.interval_ns
            )

    def take(self, now: int) -> bool:
        self.refill(now)
        if self.tokens:
            self.tokens -= 1
            return True
        return False


@dataclass(order=True)
class Queued:
    kind: int
    sequence: int
    send: Callable = field(compare=False)
    admission: Callable[[], bool] = field(compare=False)
    open_positions: int = field(compare=False)
    exiting_positions: int = field(compare=False)
    position_counts: Callable[[], tuple[int, int]] | None = field(compare=False)
    refuse: Callable[[str], None] | None = field(compare=False, default=None)
    snapshot_reads: int = field(compare=False, default=0)
    entry_positions: Callable[[], int] | None = field(compare=False, default=None)


class AccountPacer:
    def __init__(self, controls: Controls, now_ns: Callable[[], int]):
        self.controls, self.now_ns = controls, now_ns
        self.bucket = TokenBucket(
            controls["request_budget"], controls.ns("window_s"), controls.burst
        )
        self.queue: list[Queued] = []
        self.entry_charges: dict[str, bool] = {}
        self.sequence = itertools.count()
        self.wire_requests = 0
        self.wire_actions: Counter[ActionKind] = Counter()
        self.paused_until, self.consecutive_429 = 0, 0
        self.fills = 0
        self.restored = False

    def request(
        self,
        kind: ActionKind,
        send: Callable,
        *,
        admission: Callable[[], bool] = lambda: True,
        open_positions: int = 0,
        exiting_positions: int = 0,
        position_counts: Callable[[], tuple[int, int]] | None = None,
        refuse: Callable[[str], None] | None = None,
        snapshot_reads: int = 0,
        entry_positions: Callable[[], int] | None = None,
    ):
        item = Queued(
            int(kind),
            next(self.sequence),
            send,
            admission,
            open_positions,
            exiting_positions,
            position_counts,
            refuse,
            snapshot_reads,
            entry_positions,
        )
        if item.kind == ActionKind.SUBMIT:
            reserve = self._entry_reserve(item)
            if reserve is not None and reserve >= self.bucket.burst:
                if refuse is not None:
                    refuse(ENTRY_RESERVE_EXCEEDS_BURST)
                raise PacerRefused(
                    ENTRY_RESERVE_EXCEEDS_BURST,
                    f"{ENTRY_RESERVE_EXCEEDS_BURST}: entry reserve {reserve} "
                    f">= burst {self.bucket.burst}",
                )
        heapq.heappush(self.queue, item)
        self.drain()

    def _entry_reserve(self, item: Queued) -> int | None:
        per_open, per_exit = (
            self.controls["reserve_per_open_position"],
            self.controls["reserve_per_exiting_position"],
        )
        opened, exiting = (
            item.position_counts()
            if item.position_counts
            else (item.open_positions, item.exiting_positions)
        )
        pending = item.entry_positions() if item.entry_positions else 0
        if any(
            type(count) is not int or count < 0
            for count in (opened, exiting, item.snapshot_reads, pending)
        ):
            return None
        if per_open is None or ((exiting or item.snapshot_reads) and per_exit is None):
            return None
        # The new/pending entry's partial-fill and full-fill handovers add two
        # snapshot pairs and one write to the existing open-position floor.
        partial_resize = (
            per_exit + item.snapshot_reads + item.snapshot_reads if item.snapshot_reads else 0
        )
        return (
            per_open * (opened + 1)
            + (per_exit or 0) * exiting
            + item.snapshot_reads * (opened + exiting + 1)
            + partial_resize * (pending + 1)
        )

    def drain(self):
        while self.queue:
            now, item = self.now_ns(), self.queue[0]
            if now < self.paused_until:
                return
            if not item.admission():
                heapq.heappop(self.queue)
                continue
            self.bucket.refill(now)
            if item.kind == ActionKind.SUBMIT:
                reserve = self._entry_reserve(item)
                if reserve is None:
                    heapq.heappop(self.queue)
                    continue
                if reserve >= self.bucket.burst:
                    heapq.heappop(self.queue)
                    if item.refuse is not None:
                        item.refuse(ENTRY_RESERVE_EXCEEDS_BURST)
                    continue
                if self.bucket.tokens <= reserve:
                    return
            if not self.bucket.take(now):
                return
            heapq.heappop(self.queue)
            self._run(item)

    def _run(self, item: Queued):
        used, active = False, True

        def before_wire():
            nonlocal used
            if not active:
                raise ValueError("deferred wire hook must return through the pacer")
            if used or not item.admission():
                raise ValueError("one admitted paced action permits one wire request")
            used = True
            self.wire_requests += 1
            if item.kind != ActionKind.QUERY:
                self.wire_actions[ActionKind(item.kind)] += 1

        try:
            item.send(before_wire)
        finally:
            active = False

    def record_fill(self):
        self.fills += 1

    def oer(self) -> Decimal:
        return Decimal(sum(self.wire_actions.values())) / Decimal(self.fills + 1)

    def to_state(self, day: str) -> dict:
        return dict(
            day=day,
            requests=self.wire_requests,
            actions=sum(self.wire_actions.values()),
            new_orders=self.wire_actions[ActionKind.SUBMIT] + self.wire_actions[ActionKind.REDUCE],
            fills=self.fills,
            kinds={kind.name: count for kind, count in self.wire_actions.items()},
        )

    def restore(self, state: dict, *, day: str):
        if self.restored:
            raise ValueError("wire counters already restored")
        if state.get("day") == day:
            if any(
                type(state.get(k)) is not int or state[k] < 0
                for k in ("requests", "actions", "fills")
            ):
                raise ValueError("invalid persisted wire counters")
            kinds = {ActionKind[name]: count for name, count in state["kinds"].items()}
            if any(type(v) is not int or v < 0 for v in kinds.values()) or (
                sum(kinds.values()) != state["actions"] or state["actions"] > state["requests"]
            ):
                raise ValueError("wire action counters disagree")
            self.wire_requests += state["requests"]
            self.fills += state["fills"]
            self.wire_actions.update(kinds)
        self.restored = True

    def on_rate_limited(self, reset_after_ns: int | None = None):
        self.consecutive_429 += 1
        pause = (
            self.controls.ns("backoff_base_s") * 2 ** (self.consecutive_429 - 1)
            if reset_after_ns is None
            else reset_after_ns
        )
        pause = min(pause, self.controls.ns("backoff_max_s"))
        now = self.now_ns()
        self.bucket.tokens, self.bucket.anchor = 0, now
        self.paused_until = max(self.paused_until, now + pause)

    def on_success(self):
        self.consecutive_429 = 0

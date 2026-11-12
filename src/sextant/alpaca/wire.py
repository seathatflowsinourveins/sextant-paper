"""Paper account gateway and exact wire values; no order state machine.

NT d53849b: docs/developer_guide/python_adapters.md (off-loop I/O and hooks),
docs/integrations/interactive_brokers.md (params.trailing_offset).
AP cc4cb3b7: alpaca/common/rest.py (get/post/patch/delete, _retry),
alpaca/trading/requests.py. Alpaca orders-at-alpaca read 2026-11-10.
House authority and shared priority/reserves: ADR-0007/0009, AccountPacer.
"""

import asyncio
import re
from decimal import Decimal, DecimalException
from uuid import UUID

from alpaca.common.exceptions import APIError
from nautilus_trader.model import OrderSide, OrderType, TimeInForce, TrailingOffsetType

from sextant.execution.pacer import AccountPacer, ActionKind, PacerRefused


def house_id(value: str, prefix: str) -> str:
    if not prefix or not value.startswith(prefix) or len(value) > 48 or not value.isascii():
        raise ValueError("client order namespace required")
    return value


def whole(value) -> str:
    number = value.as_decimal()
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise ValueError("positive whole quantity required")
    return format(number, ".0f")


def price_text(value) -> str:
    number = value.as_decimal()
    tick = Decimal("0.01") if number >= 1 else Decimal("0.0001")
    if number <= 0 or number % tick:
        raise ValueError("price must already be tick rounded")
    return format(number, "f")


def offset_text(value) -> str:
    if type(value) not in (Decimal, str, int):
        raise ValueError("trailing offset requires decimal basis points")
    try:
        offset = Decimal(value)
        if not offset.is_finite() or offset <= 0:
            raise ValueError("positive trailing offset required")
        return format(offset / 100, "f")
    except DecimalException as exc:
        raise ValueError("valid decimal trailing offset required") from exc


def submit_payload(order, params: dict, prefix: str) -> dict:
    extended = params.get("extended_hours", False)
    if type(extended) is not bool:
        raise ValueError("extended_hours must be boolean")
    if order.time_in_force not in (TimeInForce.DAY, TimeInForce.GTC):
        raise ValueError("unsupported time in force")
    client_id = house_id(str(order.client_order_id), prefix)
    if re.search(r"-r[0-9]+$", client_id):
        raise ValueError("initial order uses reserved successor namespace")
    body = dict(
        client_order_id=client_id,
        symbol=order.instrument_id.symbol.value,
        qty=whole(order.quantity),
        side="buy" if order.side == OrderSide.BUY else "sell",
        time_in_force="day" if order.time_in_force == TimeInForce.DAY else "gtc",
        order_class="simple",
        extended_hours=extended,
    )
    if order.order_type == OrderType.LIMIT:
        body.update(type="limit", limit_price=price_text(order.price))
    elif order.order_type == OrderType.TRAILING_STOP_MARKET and not extended:
        if order.trailing_offset_type != TrailingOffsetType.BASIS_POINTS:
            raise ValueError("trail requires basis point offset")
        body.update(type="trailing_stop", trail_percent=offset_text(order.trailing_offset))
    else:
        raise ValueError("only LIMIT or regular-hours native trail is supported")
    return body


def guard_mutation(row: dict, prefix: str):
    house_id(row["client_order_id"], prefix)
    if row.get("side") == "sell" and (
        Decimal(row.get("filled_qty", "0")) > 0
        or row.get("triggered_at")
        or row.get("status") in ("stopped", "partially_filled", "filled")
    ):
        raise ValueError("executing exit cannot be mutated")


def modify_payload(command, row: dict) -> dict:
    body = {}
    if command.quantity is not None:
        body["qty"] = whole(command.quantity)
    if command.price is not None:
        body["limit_price"] = price_text(command.price)
    if row["type"] == "trailing_stop":
        offset = (command.params or {}).get("trailing_offset")
        if offset is not None:
            body["trail"] = offset_text(offset)
        elif command.trigger_price is not None:
            raise ValueError("trail trigger requires params trailing_offset")
    elif command.trigger_price is not None:
        body["stop_price"] = price_text(command.trigger_price)
    if not body:
        raise ValueError("empty modification")
    return body


class AccountGateway:
    """Every REST call, including reads, passes through the same account pacer."""

    def __init__(self, sdk, pacer: AccountPacer, position_counts):
        self.sdk, self.pacer, self.position_counts = sdk, pacer, position_counts
        # The pinned SDK otherwise retries invisibly inside one paced request.
        self.sdk._retry = 0
        self.tasks: set[asyncio.Task] = set()

    async def call(
        self,
        kind,
        method,
        path,
        data=None,
        *,
        admission=lambda: True,
        on_wire=lambda: None,
        on_end=lambda: None,
    ):
        permitted = (
            method == "get"
            and path
            in {
                "/account",
                "/positions",
                "/orders",
                "/orders:by_client_order_id",
                "/account/activities/FILL",
            }
        ) or (method == "post" and path == "/orders")
        if path.startswith("/orders/") and method in {"get", "patch", "delete"}:
            try:
                UUID(path.removeprefix("/orders/"))
            except ValueError:
                pass
            else:
                permitted = True
        if not permitted:
            raise ValueError("unsupported account endpoint")
        future = asyncio.get_running_loop().create_future()

        async def perform():
            try:
                result = await asyncio.to_thread(getattr(self.sdk, method), path, data or {})
            except Exception as exc:
                if isinstance(exc, APIError) and exc.status_code == 429:
                    self.pacer.on_rate_limited()
                if not future.done():
                    future.set_exception(exc)
            else:
                self.pacer.on_success()
                if not future.done():
                    future.set_result(result)
            finally:
                on_end()  # the wire call itself ended, whoever still awaits it

        def allowed():
            try:
                return admit()
            except Exception as exc:  # contained: a failed check refuses only its own request
                if not future.done():
                    future.set_exception(exc)
                return False

        def admit():
            if future.done():
                return False
            if kind == ActionKind.SUBMIT:
                opened, exiting = self.position_counts()
                if (
                    any(type(n) is not int or n < 0 for n in (opened, exiting))
                    or self.pacer.controls["reserve_per_open_position"] is None
                    or (exiting and self.pacer.controls["reserve_per_exiting_position"] is None)
                ):
                    future.set_exception(ValueError("entry reserve authority unavailable"))
                    return False
            if not admission():
                future.set_exception(ValueError("wire admission refused"))
                return False
            return True

        def send(mark):
            mark()
            on_wire()
            task = asyncio.create_task(perform())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        def refuse(reason):
            if not future.done():
                future.set_exception(ValueError(reason))

        try:
            self.pacer.request(
                kind, send, admission=allowed, position_counts=self.position_counts, refuse=refuse
            )
        except PacerRefused as exc:
            # pacer.py request(): refuse() precedes the raise; nothing was sent.
            refuse(exc.reason)
        try:
            while not future.done():
                # Integer-ns pacing; float here is only asyncio's scheduling API.
                await asyncio.wait({future}, timeout=self.pacer.bucket.interval_ns / 1_000_000_000)
                self.pacer.drain()
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def close(self):
        # A cancelled Python await cannot stop a synchronous wire call.
        if self.tasks:
            await asyncio.gather(*self.tasks)

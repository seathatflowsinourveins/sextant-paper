"""Convert Alpaca snapshots to Nautilus reconciliation reports.

Sources read 2026-11-10: alpaca-py@cc4cb3b7 alpaca/trading/{models,enums}.py,
https://docs.alpaca.markets/us/docs/orders-at-alpaca,
https://docs.alpaca.markets/us/reference/subscribetotradev2sse,
https://alpaca.markets/sdks/python/api_reference/trading/enums.html,
https://docs.alpaca.markets/us/docs/websocket-streaming,
and https://docs.alpaca.markets/us/docs/position-average-entry-price-calculation.
NautilusTrader@d53849b docs/developer_guide/{python_adapters,adapters}.md,
crates/adapters/{architect_ax,coinbase}/src/http/parse.rs,
crates/adapters/okx/src/websocket/parse.rs,
crates/execution/src/reconciliation/orders.rs, crates/model/src/orders/trailing_stop_market.rs
and python/nautilus_trader/model/__init__.pyi.
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from alpaca.trading import enums as alpaca
from alpaca.trading.models import Order, Position
from nautilus_trader.common import Logger
from nautilus_trader.core import dt_to_unix_nanos
from nautilus_trader.model import (
    AccountId,
    AvgPxReconciliation,
    ClientOrderId,
    Equity,
    FillReport,
    LiquiditySide,
    Money,
    OrderSide,
    OrderStatus,
    OrderStatusReport,
    OrderType,
    PositionSide,
    PositionStatusReport,
    Price,
    Quantity,
    TimeInForce,
    TradeId,
    TrailingOffsetType,
    TriggerType,
    VenueOrderId,
)


def _log() -> Logger:
    """Build the engine Logger where it logs.

    The Python ``Logger`` is an unsendable pyclass (nautilus_trader@d53849b
    ``crates/common/src/python/logging.rs:434-438``): used from a thread other than its
    creator's, PyO3 aborts the process, so a module-level instance cannot serve a node
    whose loop runs on another thread.
    """
    return Logger("AlpacaReports")


_STATUS = {
    alpaca.OrderStatus.NEW: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.ACCEPTED: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.HELD: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.PENDING_REVIEW: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.PENDING_NEW: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.ACCEPTED_FOR_BIDDING: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.DONE_FOR_DAY: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.STOPPED: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.SUSPENDED: OrderStatus.ACCEPTED,
    alpaca.OrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    alpaca.OrderStatus.FILLED: OrderStatus.FILLED,
    alpaca.OrderStatus.CANCELED: OrderStatus.CANCELED,
    alpaca.OrderStatus.EXPIRED: OrderStatus.EXPIRED,
    alpaca.OrderStatus.REJECTED: OrderStatus.REJECTED,
    alpaca.OrderStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL,
    alpaca.OrderStatus.PENDING_REPLACE: OrderStatus.PENDING_UPDATE,
}
_SIDE = {alpaca.OrderSide.BUY: OrderSide.BUY, alpaca.OrderSide.SELL: OrderSide.SELL}
_POSITION_SIDE = {
    alpaca.PositionSide.LONG: PositionSide.LONG,
    alpaca.PositionSide.SHORT: PositionSide.SHORT,
}
_TYPE = {
    alpaca.OrderType.MARKET: OrderType.MARKET,
    alpaca.OrderType.LIMIT: OrderType.LIMIT,
    alpaca.OrderType.STOP: OrderType.STOP_MARKET,
    alpaca.OrderType.STOP_LIMIT: OrderType.STOP_LIMIT,
    alpaca.OrderType.TRAILING_STOP: OrderType.TRAILING_STOP_MARKET,
}
_TIF = {
    alpaca.TimeInForce.DAY: TimeInForce.DAY,
    alpaca.TimeInForce.GTC: TimeInForce.GTC,
    alpaca.TimeInForce.OPG: TimeInForce.AT_THE_OPEN,
    alpaca.TimeInForce.CLS: TimeInForce.AT_THE_CLOSE,
    alpaca.TimeInForce.IOC: TimeInForce.IOC,
    alpaca.TimeInForce.FOK: TimeInForce.FOK,
}


class UnrepresentableQuantity(ValueError):
    """An exact venue quantity exceeds instrument precision (adapter guide, Numeric precision)."""


class UnmappedOrderStatus(ValueError):
    """A venue status needs explicit classification or successor evidence (adapter guide, enums)."""


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field} requires decimal text from the venue")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} requires valid decimal text") from exc
    if not result.is_finite():
        raise ValueError(f"{field} requires a finite decimal")
    return result


def _quantity(value: Decimal, instrument: Equity) -> Quantity:
    quantity = Quantity.from_decimal_dp(value, instrument.size_precision)
    if quantity.as_decimal() != value:
        raise UnrepresentableQuantity("venue quantity exceeds instrument size precision")
    return quantity


def order_status_report(
    order: Order,
    *,
    account_id: AccountId,
    instrument: Equity,
    ts_init: int,
    client_order_id: ClientOrderId | None = None,
) -> OrderStatusReport:
    """Map a simple equity snapshot at its loaded instrument precision.

    The caller supplies the stable engine ID after resolving replacement lineage.
    A PATCH response alone can be a proposal (Alpaca's Replace Order by ID reference).
    """
    if (
        order.asset_class != alpaca.AssetClass.US_EQUITY
        or order.order_class != alpaca.OrderClass.SIMPLE
    ):
        raise ValueError("Alpaca order report requires a simple equity order")
    if order.qty is None or order.filled_qty is None:
        raise ValueError("Alpaca order report requires ordered and filled share quantity")
    kind = order.type or order.order_type
    if kind is None or order.side is None:
        raise ValueError("Alpaca order report requires order type and side")
    if order.symbol != instrument.raw_symbol.value:
        raise ValueError("Alpaca order does not match the loaded instrument")

    quantity = _decimal(order.qty, "order quantity")
    filled_qty = _decimal(order.filled_qty, "filled quantity")
    if quantity <= 0 or not 0 <= filled_qty <= quantity:
        raise ValueError("Alpaca order report has inconsistent share quantities")

    # Alpaca's lifecycle retains pre-open and day-paused orders. Coinbase's adapter
    # likewise reports queued/pre-open acceptance. Read the Alpaca page 2026-11-10.
    status = _STATUS.get(order.status)
    if order.status == alpaca.OrderStatus.CALCULATED:
        status = OrderStatus.FILLED if filled_qty == quantity else OrderStatus.ACCEPTED
    if status is None:
        raise UnmappedOrderStatus(
            f"Alpaca order status requires classification or succession: {order.status}"
        )
    if status == OrderStatus.ACCEPTED and 0 < filled_qty < quantity:
        status = OrderStatus.PARTIALLY_FILLED
    if order.status in (
        alpaca.OrderStatus.STOPPED,
        alpaca.OrderStatus.SUSPENDED,
        alpaca.OrderStatus.CALCULATED,
        alpaca.OrderStatus.PENDING_REVIEW,
    ):
        _log().warning(f"Retaining Alpaca order with venue status {order.status}")

    trigger_type = (
        TriggerType.DEFAULT
        if kind
        in (alpaca.OrderType.STOP, alpaca.OrderType.STOP_LIMIT, alpaca.OrderType.TRAILING_STOP)
        else None
    )
    trailing_offset = None
    trailing_offset_type = None
    if kind == alpaca.OrderType.TRAILING_STOP:
        if (order.trail_price is None) == (order.trail_percent is None):
            raise ValueError("Alpaca trailing stop requires exactly one offset")
        if order.trail_price is not None:
            trailing_offset = _decimal(order.trail_price, "trail price")
            trailing_offset_type = TrailingOffsetType.PRICE
        elif order.trail_percent is not None:
            trailing_offset = _decimal(order.trail_percent, "trail percent") * 100
            trailing_offset_type = TrailingOffsetType.BASIS_POINTS

    # The SDK supplies submission and update times, with no separate acceptance timestamp.
    return OrderStatusReport(
        account_id=account_id,
        instrument_id=instrument.id,
        venue_order_id=VenueOrderId(str(order.id)),
        order_side=_SIDE[order.side],
        order_type=_TYPE[kind],
        time_in_force=_TIF[order.time_in_force],
        order_status=status,
        quantity=_quantity(quantity, instrument),
        filled_qty=_quantity(filled_qty, instrument),
        ts_accepted=dt_to_unix_nanos(order.submitted_at),
        ts_last=dt_to_unix_nanos(order.updated_at),
        ts_init=ts_init,
        client_order_id=client_order_id or ClientOrderId(order.client_order_id),
        expire_time=dt_to_unix_nanos(order.expires_at) if order.expires_at is not None else None,
        price=Price.from_decimal_dp(
            _decimal(order.limit_price, "limit price"), instrument.price_precision
        )
        if order.limit_price is not None
        else None,
        trigger_price=Price.from_decimal_dp(
            _decimal(order.stop_price, "stop price"), instrument.price_precision
        )
        if order.stop_price is not None
        else None,
        trigger_type=trigger_type,
        trailing_offset=trailing_offset,
        trailing_offset_type=trailing_offset_type,
        avg_px=_decimal(order.filled_avg_price, "average fill price")
        if filled_qty and order.filled_avg_price is not None
        else None,
    )


def position_status_report(
    position: Position,
    *,
    account_id: AccountId,
    instrument: Equity,
    ts_init: int,
) -> PositionStatusReport | None:
    """Map an open equity position at snapshot observation time."""
    if position.asset_class != alpaca.AssetClass.US_EQUITY:
        _log().warning(f"Omitting out-of-scope Alpaca position asset class {position.asset_class}")
        return None
    if position.symbol != instrument.raw_symbol.value:
        raise ValueError("Alpaca position does not match the loaded instrument")
    side = _POSITION_SIDE[position.side]
    quantity = _decimal(position.qty, "position quantity")
    if quantity.is_zero() or (side == PositionSide.LONG and quantity < 0):
        raise ValueError("Alpaca open position report has inconsistent share quantity")
    avg_entry_price = _decimal(position.avg_entry_price, "average entry price")
    if avg_entry_price < 0:
        raise ValueError("Alpaca position report has invalid average entry price")

    # Alpaca's cost basis can change overnight without a fill.
    return PositionStatusReport(
        account_id=account_id,
        instrument_id=instrument.id,
        position_side=side,
        quantity=_quantity(quantity.copy_abs(), instrument),
        ts_last=ts_init,
        ts_init=ts_init,
        avg_px_open=avg_entry_price or None,
        avg_px_open_reconciliation=AvgPxReconciliation.OPENING_ONLY,
    )


def order_status_report_results(
    orders: Iterable[tuple[Mapping[str, Any], Equity]],
    *,
    account_id: AccountId,
    ts_init: int,
) -> list[tuple[Mapping[str, Any], OrderStatusReport | ValueError]]:
    """Process every resolved record and retain failures with their original payload.

    The adapter guide's fallible per-record mapping preserves sibling records.
    Callers must retain incomplete coverage when a result is an error. Contingent
    records retain their legs for the external-order proposal and custody decision.
    """
    results: list[tuple[Mapping[str, Any], OrderStatusReport | ValueError]] = []
    for row, instrument in orders:
        try:
            report = order_status_report(
                Order.model_validate(row),
                account_id=account_id,
                instrument=instrument,
                ts_init=ts_init,
            )
        except ValueError as exc:
            _log().error(f"Alpaca order report failed: {type(exc).__name__}")
            results.append((row, exc))
        else:
            results.append((row, report))
    return results


def fill_report(
    update: Mapping[str, Any],
    *,
    account_id: AccountId,
    instrument: Equity,
    ts_init: int,
    commission: Money,
    client_order_id: ClientOrderId | None = None,
) -> FillReport | None:
    """Map a TradingStream(raw_data=True) envelope with commission evidence."""
    if not isinstance(update, Mapping) or update.get("stream") != "trade_updates":
        raise ValueError("Alpaca fill report requires a raw trade_updates envelope")
    data = update.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("Alpaca fill report requires raw event data")
    if data.get("event") not in ("fill", "partial_fill"):
        return None
    order = data.get("order")
    if not isinstance(order, Mapping):
        raise ValueError("Alpaca fill report requires raw order data")
    if order.get("asset_class") != "us_equity" or order.get("side") not in _SIDE:
        raise ValueError("Alpaca fill report requires a US equity and side")
    if not isinstance(data.get("execution_id"), str):
        raise ValueError("Alpaca fill report requires execution ID, fill price and fill quantity")
    if order.get("symbol") != instrument.raw_symbol.value:
        raise ValueError("Alpaca fill does not match the loaded instrument")

    price = _decimal(data.get("price"), "fill price")
    quantity = _decimal(data.get("qty"), "fill quantity")
    if price <= 0 or quantity <= 0:
        raise ValueError("Alpaca fill report requires positive finite fill price and quantity")

    return FillReport(
        account_id=account_id,
        instrument_id=instrument.id,
        venue_order_id=VenueOrderId(order["id"]),
        trade_id=TradeId(data["execution_id"]),
        order_side=_SIDE[order["side"]],
        last_qty=_quantity(quantity, instrument),
        last_px=Price.from_decimal_dp(price, instrument.price_precision),
        commission=commission,
        liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
        ts_event=dt_to_unix_nanos(data["timestamp"]),
        ts_init=ts_init,
        client_order_id=client_order_id or ClientOrderId(order["client_order_id"]),
    )

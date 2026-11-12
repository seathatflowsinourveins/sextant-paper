"""N1b (LEAD-2325 2e): the capacity-counting Actor on a real BacktestNode with known lags.

Fixture ticks carry chosen ``ts_init - ts_event`` lags, so the per-second nearest-rank
percentiles are known exactly. Fixture values exercise mechanisms only.
"""

from __future__ import annotations

import ast
import json
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.backtest import BacktestNode
from nautilus_trader.config import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestRunConfig,
    BacktestVenueConfig,
    DataCatalogConfig,
)
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    AggressorSide,
    Currency,
    Equity,
    InstrumentId,
    NautilusDataType,
    OmsType,
    Price,
    Quantity,
    QuoteTick,
    Symbol,
    TradeId,
    TradeTick,
)
from nautilus_trader.persistence import ParquetDataCatalog

from sextant.ranking.capacity import CapacityActor, CapacityActorConfig

ROOT = Path(__file__).parents[2]
MODULE = ROOT / "src/sextant/ranking/capacity.py"
NS, MS, US = 1_000_000_000, 1_000_000, 1_000
START = 1_793_088_000 * NS
PENNY = InstrumentId.from_str("PENNY.ALPACA")
EQUITY = Equity(
    instrument_id=PENNY,
    raw_symbol=Symbol("PENNY"),
    currency=Currency.from_str("USD"),
    price_precision=2,
    price_increment=Price.from_str("0.01"),
    lot_size=Quantity.from_str("1"),
    ts_event=0,
    ts_init=0,
)


def trade(init: int, lag: int, n: int) -> TradeTick:
    return TradeTick(
        PENNY,
        Price.from_str("0.50"),
        Quantity.from_str("100"),
        AggressorSide.BUY,
        TradeId(f"t{n}"),
        init - lag,
        init,
    )


def quote(init: int, lag: int) -> QuoteTick:
    size = Quantity.from_str("100")
    return QuoteTick(
        PENNY, Price.from_str("0.49"), Price.from_str("0.50"), size, size, init - lag, init
    )


# Second 0: ten trades 50 ms apart with lags 1..10 ms. Second 1: a hundred trades 5 ms apart
# with lags 1..100 us; the first comes 500 ms after second 0's last.
TRADES = [trade(START + k * 50 * MS, k * MS, k) for k in range(1, 11)]
TRADES += [trade(START + NS + j * 5 * MS, (j + 1) * US, 100 + j) for j in range(100)]
QUOTES = [quote(START + 600 * MS + k * MS, k * MS) for k in range(1, 5)]


def run(tmp_path: Path, receipt: Path, cls=CapacityActor, **kwargs: Any) -> CapacityActor:
    store = tmp_path / "catalog"
    store.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(store))
    catalog.write_instruments([EQUITY])
    catalog.write_trade_ticks(TRADES)
    catalog.write_quote_ticks(QUOTES)
    config = BacktestRunConfig(
        venues=[
            BacktestVenueConfig(
                name="ALPACA",
                oms_type=OmsType.NETTING,
                account_type=AccountType.CASH,
                starting_balances=["100000 USD"],
                fee_model=MakerTakerFeeModel(D(0), D(0)),
            )
        ],
        data=[
            BacktestDataConfig(
                data_type=kind, catalog=DataCatalogConfig(path=str(store)), instrument_id=PENNY
            )
            for kind in (NautilusDataType.TradeTick, NautilusDataType.QuoteTick)
        ],
        engine=BacktestEngineConfig(bypass_logging=True, run_analysis=False),
        raise_exception=True,
        dispose_on_completion=False,
        start=START - NS,
    )
    kwargs.setdefault("quotes", True)
    kwargs.setdefault("bucket_window_s", 3600)  # fixture window
    actor = cls(
        CapacityActorConfig(
            instrument_ids=(PENNY,), receipt_path=receipt, receipt_interval_s=1, **kwargs
        )
    )
    node = BacktestNode([config])
    try:
        node.build()
        node.add_actor(config.id, actor)
        node.run()
    finally:
        node.dispose()
    return actor


def test_known_lags_give_exact_per_second_percentiles_counts_and_gaps(tmp_path) -> None:
    receipt = tmp_path / "out" / "capacity.json"
    actor = run(tmp_path, receipt)
    body = json.loads(receipt.read_text())
    assert body["final"] is True and body["errors"] == 0 and actor.writes >= 2
    first, second = (b for b in body["buckets"] if b["second"] in (START // NS, START // NS + 1))
    assert first["trades"] == {
        "count": 10,
        "lag_ns": {"p50": 5 * MS, "p95": 10 * MS, "p99": 10 * MS, "max": 10 * MS},
        "max_gap_ns": 50 * MS,
    }
    assert first["quotes"]["count"] == 4 and first["quotes"]["lag_ns"]["p50"] == 2 * MS
    assert second["trades"] == {
        "count": 100,
        "lag_ns": {"p50": 50 * US, "p95": 95 * US, "p99": 99 * US, "max": 100 * US},
        "max_gap_ns": 500 * MS,  # second 0's last trade to second 1's first
    }
    assert first["handler_ns"]["count"] == 14 and second["handler_ns"]["count"] == 100
    assert second["handler_ns"]["max"] >= 0


def test_quotes_are_counted_only_when_configured(tmp_path) -> None:
    receipt = tmp_path / "trades.json"
    run(tmp_path, receipt, quotes=False)
    buckets = json.loads(receipt.read_text())["buckets"]
    assert all(bucket["quotes"]["count"] == 0 for bucket in buckets)


def test_a_failed_receipt_write_is_counted_and_never_raises(tmp_path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    actor = run(tmp_path, blocker / "capacity.json")
    assert actor.write_errors >= 1 and actor.ticks == len(TRADES) + len(QUOTES)


def test_the_receipt_interval_has_no_default() -> None:
    with pytest.raises(TypeError):
        CapacityActorConfig(  # type: ignore[call-arg]
            instrument_ids=(PENNY,), receipt_path=Path("x")
        )
    with pytest.raises(ValueError):
        CapacityActorConfig(
            instrument_ids=(PENNY,),
            receipt_path=Path("x"),
            receipt_interval_s=0,
            bucket_window_s=60,
        )


def test_the_actor_holds_no_socket_and_places_nothing() -> None:
    tree = ast.parse(MODULE.read_text())
    imports = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    roots = {name.split(".")[0] for name in imports}
    allowed = {"__future__", "collections", "datetime", "math", "time", "pathlib", "typing"}
    assert roots <= allowed | {"nautilus_trader", "sextant"}
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert names.isdisjoint(
        {"submit_order", "modify_order", "cancel_order", "order_factory", "reconnect_socket"}
    )


def test_a_timer_write_after_stop_never_replaces_the_final_receipt(tmp_path) -> None:
    # A live node can dispatch a receipt timer event queued before the stop after on_stop.
    class Late(CapacityActor):
        def on_stop(self) -> None:
            super().on_stop()
            self._on_timer(None)

    receipt = tmp_path / "late" / "capacity.json"
    run(tmp_path, receipt, cls=Late)
    assert json.loads(receipt.read_text())["final"] is True


def test_an_unknown_config_key_is_refused_by_name() -> None:
    with pytest.raises(TypeError, match="receipt_interval"):
        CapacityActorConfig(  # a misspelled key, never swallowed
            instrument_ids=(PENNY,),
            receipt_path=Path("x"),
            receipt_interval_s=1,
            bucket_window_s=60,
            receipt_interval=5,
        )
    config = CapacityActorConfig(  # the engine's own base fields still pass through
        instrument_ids=(PENNY,),
        receipt_path=Path("x"),
        receipt_interval_s=1,
        bucket_window_s=60,
        log_events=False,
    )
    assert config.log_events is False


# --- Review round 1 (Codex xhigh at ba990fd): the exact-head witnesses as proofs ----------


def test_closed_buckets_and_queue_states_stay_inside_the_window(tmp_path, monkeypatch) -> None:
    samples = 256  # F5: a long session in one receipt interval
    monkeypatch.setattr(
        sys.modules[__name__], "TRADES", [trade(START + i * NS, 0, i) for i in range(samples)]
    )
    monkeypatch.setattr(sys.modules[__name__], "QUOTES", [])

    class LongInterval(CapacityActor):
        def __init__(self, config) -> None:
            super().__init__(config)
            self._interval = samples + 1

    receipt = tmp_path / "capacity.json"
    actor = run(tmp_path, receipt, cls=LongInterval, quotes=False, bucket_window_s=60)
    assert len(actor._closed) <= 60 and actor.dropped_buckets == samples - 1 - 60
    body = json.loads(receipt.read_text())
    assert len(body["buckets"]) <= 61 and body["dropped_buckets"] == actor.dropped_buckets
    assert body["ticks"] == samples  # the totals still cover the whole session


def test_the_bucket_window_has_no_default() -> None:
    with pytest.raises(TypeError, match="bucket_window_s"):
        CapacityActorConfig(  # type: ignore[call-arg]
            instrument_ids=(PENNY,), receipt_path=Path("unused.json"), receipt_interval_s=1
        )
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="bucket_window_s"):
            CapacityActorConfig(
                instrument_ids=(PENNY,),
                receipt_path=Path("unused.json"),
                receipt_interval_s=1,
                bucket_window_s=cast(Any, bad),
            )


def test_a_failing_subscription_is_counted_and_the_node_keeps_running(tmp_path) -> None:  # F4
    class Unavailable(CapacityActor):
        def subscribe_trades(self, instrument_id, client_id=None, params=None) -> None:
            raise OSError("fixture subscription registration unavailable")

    actor = run(tmp_path, tmp_path / "capacity.json", cls=Unavailable)
    assert actor.faults["subscribe"] == 1 and actor.ticks == len(QUOTES)  # quotes still count
    assert actor.writes >= 1  # the timer and the final receipt still run


def test_a_failing_timer_registration_is_counted_and_the_node_keeps_running(tmp_path) -> None:
    class NoTimer(CapacityActor):
        def __init__(self, config) -> None:
            super().__init__(config)
            self._interval = 0  # the engine refuses a zero interval at registration

    actor = run(tmp_path, tmp_path / "capacity.json", cls=NoTimer)
    assert actor.faults["timer"] == 1 and actor.ticks == len(TRADES) + len(QUOTES)


def test_the_engine_base_fields_reach_the_config_through_new_not_init() -> None:
    """Counter-witness (gate review of #487): the pinned pattern passes ``actor_id`` and the log
    settings to ``DataActorConfig.__new__`` and calls ``super().__init__()`` with none
    (docs/concepts/actors.md:15-21); the base ``__init__`` is ``object.__init__`` and refuses
    them, so forwarding ``**kwargs`` would break every config."""
    from nautilus_trader.config import DataActorConfig
    from nautilus_trader.model import ActorId

    config = CapacityActorConfig(
        instrument_ids=(PENNY,),
        receipt_path=Path("x"),
        receipt_interval_s=1,
        bucket_window_s=60,
        actor_id=ActorId("CAPACITY-PROOF"),
        log_events=False,
        log_commands=False,
    )
    assert config.actor_id == ActorId("CAPACITY-PROOF")
    assert config.log_events is False and config.log_commands is False

    class Forwarding(DataActorConfig):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

    with pytest.raises(TypeError, match=r"object\.__init__"):
        Forwarding(log_events=False)

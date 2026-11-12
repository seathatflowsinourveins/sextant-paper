"""The data-only ranking node entry for the capacity measurement (LEAD-2325 2d/2e, part N2a).

One command builds NautilusTrader's live node data-only: a ``LiveNodeConfig`` with no
execution client, refused if one is configured. It registers the Alpaca data client with
#452's ``AlpacaDataClientFactory`` through ``LiveNodeBuilder.from_config(...).add_data_client
(name, factory, config).build()``, as the pinned adapter guide registers a Python factory
(nautilus_trader@d53849b ``docs/developer_guide/python_adapters.md:33-66``). It then adds
the ``CapacityActor`` and calls ``node.run()``, which owns the calling thread and SIGINT and
SIGTERM (``docs/concepts/live.md:117-120, 167-170``). A daemon timer stops the node through
``LiveNodeHandle.stop()``, which is safe from any thread (``live.md:187-190``), when the
duration ends. The Actor writes the final receipt in ``on_stop``. The market-data lease is an
injected context manager held for the whole run, and no lease means no start. The command
takes ``tradingview.producer.reserve_stream``. Credentials are only the environment-variable
names #452's config reads, never a value. No strategy, no execution client, no order path.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nautilus_trader.common import Environment
from nautilus_trader.live import LiveNode, LiveNodeBuilder, LiveNodeConfig
from nautilus_trader.model import InstrumentId, Symbol, TraderId

from sextant.alpaca.config import AlpacaDataClientConfig
from sextant.alpaca.constants import VENUE
from sextant.alpaca.factories import AlpacaDataClientFactory
from sextant.ranking import node_parameters
from sextant.ranking.capacity import CapacityActor, CapacityActorConfig

NODE_NAME = "ranking-node"
CLIENT_NAME = "ALPACA"
TRADER_ID = TraderId.from_str("RANKING-001")  # the ranking host's bus identity (host.py)
RECEIPT_NAME = "capacity.json"
# Window flags that no longer exist (lead 04:2x): refused by the row they used to override.
RETIRED = {
    "--receipt-interval-s": "receipt_interval_s",
    "--bucket-window-s": "bucket_window_s",
    "--parameters": "the committed parameter registry",
}
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}")


# The one-socket lease (reserve_stream's shape): any context manager, held for the whole run.
MarketDataLease = AbstractContextManager[Any]


@dataclass(frozen=True)
class Arguments:
    universe: tuple[InstrumentId, ...]
    duration_s: int
    receipt_dir: Path
    receipt_interval_s: int
    bucket_window_s: int
    quotes: bool
    data: AlpacaDataClientConfig
    parameters: dict[str, tuple[int | None, str]] | None = None  # value and origin (F7)


@dataclass(frozen=True)
class LeaseArguments:
    root: Path
    paper_slot: int
    existing_streams: int
    source: str


def read_universe(path: Path) -> tuple[InstrumentId, ...]:
    """One symbol per line; blank lines are skipped, anything else is refused."""
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    names = [name for name in names if name]
    if not names or len(set(names)) != len(names):
        raise ValueError("the universe file needs unique symbols, one per line")
    for name in names:
        if _SYMBOL.fullmatch(name) is None:
            raise ValueError(f"not a US equity symbol: {name!r}")
    return tuple(InstrumentId(Symbol(name), VENUE) for name in names)


def _positive(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def parse(argv: list[str] | None = None) -> tuple[Arguments, LeaseArguments]:
    # allow_abbrev=False: a prefix or misspelling is refused, never mapped to an option
    # (https://docs.python.org/3.13/library/argparse.html#allow-abbrev, read 2026-11-11).
    parser = argparse.ArgumentParser(prog="python -m sextant.ranking.node_main", allow_abbrev=False)
    required = parser.add_argument_group("required")
    required.add_argument("--universe", type=Path, required=True)
    required.add_argument("--duration-s", type=_positive, required=True)
    required.add_argument("--receipt-dir", type=Path, required=True)
    required.add_argument("--feed", required=True)
    required.add_argument("--api-key-env", required=True)
    required.add_argument("--api-secret-env", required=True)
    required.add_argument("--lease-root", type=Path, required=True)
    required.add_argument("--paper-slot", type=int, required=True)
    required.add_argument("--existing-streams", type=int, required=True)
    required.add_argument("--lease-source", required=True)
    parser.add_argument("--quotes", action="store_true", help="also count quotes")
    node_parameters.refuse_retired(sys.argv[1:] if argv is None else argv, RETIRED)
    raw = parser.parse_args(argv)
    rows = node_parameters.load()  # the committed rows only: no override (lead 04:2x)
    resolved: dict[str, tuple[int | None, str]] = {
        "receipt_interval_s": node_parameters.resolve(rows, "receipt_interval_s"),
        # Not a tunable: every closed second of the run is kept. A run of D seconds touches at
        # most D + 1 whole seconds, so the bound never drops one (dropped_buckets checks it).
        "bucket_window_s": (raw.duration_s + 1, "derived: --duration-s + 1"),
    }
    if resolved["receipt_interval_s"][0] is None:
        raise ValueError("no registry value for receipt_interval_s")
    data = AlpacaDataClientConfig(
        api_key_env=raw.api_key_env, api_secret_env=raw.api_secret_env, feed=raw.feed
    )
    args = Arguments(
        read_universe(raw.universe),
        raw.duration_s,
        raw.receipt_dir,
        int(resolved["receipt_interval_s"][0] or 0),
        int(resolved["bucket_window_s"][0] or 0),
        raw.quotes,
        data,
        resolved,
    )
    lease = LeaseArguments(raw.lease_root, raw.paper_slot, raw.existing_streams, raw.lease_source)
    return args, lease


def node_config() -> LiveNodeConfig:
    """Data-only: no execution client, so no order can reach a venue from this node."""
    return LiveNodeConfig(environment=Environment.LIVE, trader_id=TRADER_ID)


def refuse_order_paths(config: LiveNodeConfig) -> None:
    if config.exec_clients:
        raise ValueError("the ranking node is data-only: an execution client is configured")


def build(
    args: Arguments, *, factory: Any = AlpacaDataClientFactory, config: LiveNodeConfig | None = None
) -> tuple[LiveNode, CapacityActor]:
    config = node_config() if config is None else config
    refuse_order_paths(config)
    node = (
        LiveNodeBuilder.from_config(NODE_NAME, config)
        .add_data_client(CLIENT_NAME, factory, args.data)
        .build()
    )
    actor = CapacityActor(
        CapacityActorConfig(
            instrument_ids=args.universe,
            receipt_path=args.receipt_dir / RECEIPT_NAME,
            receipt_interval_s=args.receipt_interval_s,
            bucket_window_s=args.bucket_window_s,
            quotes=args.quotes,
        )
    )
    node.add_actor(actor)
    return node, actor


def run(
    args: Arguments,
    *,
    lease: MarketDataLease | None,
    factory: Any = AlpacaDataClientFactory,
    config: LiveNodeConfig | None = None,
) -> Path:
    """Hold the lease, run the node for the duration or until a signal, then dispose."""
    if lease is None:
        raise ValueError("the market-data lease is required: no lease, no start")
    with lease:
        if args.parameters is not None:
            node_parameters.write_receipt(args.receipt_dir, args.parameters)
        node, _ = build(args, factory=factory, config=config)
        timer = threading.Timer(args.duration_s, node.handle().stop)
        timer.daemon = True
        try:
            timer.start()
            node.run()
        finally:
            timer.cancel()
            node.dispose()
    return args.receipt_dir / RECEIPT_NAME


def main(argv: list[str] | None = None) -> Path:
    from sextant.tradingview.producer import reserve_stream

    args, lease = parse(argv)
    reserved = reserve_stream(
        lease.root,
        paper_slot=lease.paper_slot,
        existing_streams=lease.existing_streams,
        source=lease.source,
    )
    return run(args, lease=reserved)


if __name__ == "__main__":
    main()

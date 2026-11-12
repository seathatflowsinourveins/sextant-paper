"""Part 6: B's existing entry/consumer/gate composed without enabling any row.

BUILD ON Sextant@453cae04 sim/launcher.py:259-272 through paper.entry_inputs;
Sextant@b202fead paper/node_rank.py:49-66 (the node still owns the one consumer);
nautilus_trader@d53849b2 python/nautilus_trader/trading/__init__.pyi:483-552.
BLifecycle owns decide_rank_gate and the native order factory; this module only wires inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sextant.paper.b_lifecycle import BLifecycleConfig, Journal
from sextant.paper.entry_inputs import load_entry_inputs
from sextant.ranking import registry
from sextant.ranking.consumer import RankingConsumer

if TYPE_CHECKING:
    from sextant.paper.node_order import OrderArguments


def strategy_config(
    args: OrderArguments, *, consumer: RankingConsumer | None, journal: Journal | None
) -> BLifecycleConfig:
    """Keep off-path inputs unchanged; enabled entries use the consumer's exact segment.

    Missing dependencies remain missing. BLifecycleConfig checks the baseline/feed identity;
    BLifecycle refuses entries until every input exists and a fresh rank decision passes.
    """
    baseline = None
    if registry.RANK_GATE_ENABLED.value is True and consumer is not None and args.ranking:
        baseline = args.ranking.baselines.get(consumer.reader.segment.name)
    inputs = load_entry_inputs(
        args.rows.entry,
        exit_params=args.rows.exit,
        commission_rate=args.rows.commission_rate,
        basis_path=args.basis_path,
        baseline=baseline,
    )
    return BLifecycleConfig(
        namespace=args.account.namespace,
        instrument_ids=args.universe,
        consumer=consumer,
        rank_gate=registry.RANK_GATE_ENABLED,
        journal=journal,
        native_trail_fraction=args.rows.native,
        width_rows=args.rows.width,
        **inputs,
    )

"""B's optional in-node ranking: BUILD ON node_ranking's verified loaders and actor.

Native transport: nautilus_trader@d53849b216e20f2c122ca01b4dd0da377638b186
 docs/concepts/message_bus.md:55-65,180-239; custom_data.md:87-145.
No entry-age authority is accepted from CLI or input files (LEAD-1501).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sextant.ranking import node_ranking, registry
from sextant.ranking.baseline import segment_start
from sextant.ranking.consumer import RankingConsumer
from sextant.ranking.publisher import universe_digest
from sextant.ranking.registry import EntryAges


def ages() -> EntryAges | None:
    try:
        return registry.entry_ages(registry.B_ENTRY_AGE_ROWS)
    except (ValueError, TypeError):
        return None  # absent/partial/unsourced is today's protection-only path


def load(raw: argparse.Namespace) -> node_ranking.RankingArguments | None:
    if ages() is None:
        return None  # no file read, consumer or actor when authority is absent
    fields = ("ranking_universe_artifact", "ranking_baseline_dir", "ranking_inputs_dir")
    if any(getattr(raw, name) is None for name in fields):
        raise ValueError("ranking requires universe artifact, baseline dir and inputs dir")
    return node_ranking.load_inputs(
        argparse.Namespace(
            universe_artifact=raw.ranking_universe_artifact,
            baseline_dir=raw.ranking_baseline_dir,
            ranking_inputs_dir=raw.ranking_inputs_dir,
            registry=Path(__file__).resolve().parents[3]
            / "lanes/tradingview/k2-ranking.params.json",
            duration_s=raw.duration_s,
            receipt_dir=raw.receipt_dir / "ranking",
            feed="sip",
            api_key_env=raw.api_key_env,
            api_secret_env=raw.api_secret_env,
        )
    )


def consumer(args: node_ranking.RankingArguments, *, now: int) -> RankingConsumer:
    rows = ages()
    if rows is None:
        raise ValueError("ranking entry ages unavailable")
    segment = segment_start(now)[0]
    if segment is None:
        raise ValueError("ranking has no active segment")
    inputs = node_ranking.file_inputs(args)(segment)
    if inputs is None:
        raise ValueError("ranking has no verified inputs for this session/segment")
    return RankingConsumer(
        registry=args.registry,
        feed=args.data.feed,
        segment=segment,
        universe_hash=universe_digest(args.universe),
        baseline_hash=inputs.baseline.identity,
        **rows,
    )

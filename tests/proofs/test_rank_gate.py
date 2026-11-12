"""Offline proofs for the runner-independent rank veto."""

from datetime import date
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType

import pytest
from nautilus_trader.model import CustomData
from test_ranking import NS, START, publisher
from test_ranking_consumer import consumer, published

from sextant.ranking import registry
from sextant.ranking.publisher import ranking_data_type


def rank_gate() -> ModuleType:
    module = "sextant.ranking.rank_gate"
    assert find_spec(module) is not None, "rank gate decision function is missing"
    return import_module(module)


def test_rank_gate_defaults_to_disabled() -> None:
    gate = rank_gate()
    default = gate.decide_rank_gate()
    explicit = gate.decide_rank_gate(enabled=False, rank=None, consumer_reason="heartbeat_stale")
    assert (default.status.value, default.reason) == ("disabled", "rank_gate_disabled")
    assert explicit == default


@pytest.mark.parametrize(
    ("have_generation", "have_heartbeat", "generation_age", "heartbeat_age", "reason"),
    [
        (False, True, 2, 2, "generation_missing"),
        (True, False, 2, 2, "heartbeat_missing"),
        (True, True, 1, 2, "entry_generation_stale"),
        (True, True, 2, 1, "entry_heartbeat_stale"),
    ],
    ids=("missing-generation", "missing-heartbeat", "stale-generation", "stale-heartbeat"),
)
def test_enabled_gate_refuses_missing_or_stale_evidence(
    have_generation: bool,
    have_heartbeat: bool,
    generation_age: int,
    heartbeat_age: int,
    reason: str,
) -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    candidate = next(row.symbol for row in generation.rows if row.focus_eligible is True)
    entry = consumer(pub, heartbeat=heartbeat_age, generation=generation_age)
    if have_generation:
        assert entry.handle(CustomData(ranking_data_type(pub.feed), generation), now=at)
    if have_heartbeat:
        assert entry.accept_heartbeat(at=at, received_ns=at)

    rank = entry.entry_rank(candidate, now=at + NS + 1)
    assert rank is None and entry.reason == reason
    decision = rank_gate().decide_rank_gate(enabled=True, rank=rank, consumer_reason=entry.reason)
    assert (decision.status.value, decision.reason) == ("refuse", reason)


@pytest.mark.parametrize(
    ("ranked", "expected"),
    [(True, ("pass", "fresh")), (False, ("refuse", "candidate_unranked"))],
    ids=("ranked", "unranked"),
)
def test_enabled_gate_requires_a_rank_in_fresh_evidence(
    ranked: bool, expected: tuple[str, str]
) -> None:
    pub = publisher()
    at = START + 360 * NS
    generation = published(pub, at=at)
    candidate = next(
        row.symbol for row in generation.rows if (row.focus_eligible is True) == ranked
    )
    entry = consumer(pub, heartbeat=1, generation=1, inputs=1)
    assert entry.handle(CustomData(ranking_data_type(pub.feed), generation), now=at)
    assert entry.accept_heartbeat(at=at, received_ns=at)

    rank = entry.entry_rank(candidate, now=at + NS)
    assert (rank is not None) == ranked and entry.reason == "fresh"
    gate = rank_gate()
    decision = gate.decide_rank_gate(enabled=True, rank=rank, consumer_reason=entry.reason)
    assert isinstance(decision, gate.RankGateDecision)
    assert isinstance(decision.status, gate.RankGateStatus)
    assert (decision.status.value, decision.reason) == expected


@pytest.mark.parametrize(
    ("enabled", "rank", "consumer_reason", "reason"),
    [
        (True, None, None, "consumer_result_missing"),
        (True, 1, "entry_generation_stale", "entry_generation_stale"),
        (True, 1, "generation sequence discontinuity", "generation sequence discontinuity"),
        (True, True, "fresh", "candidate_rank_invalid"),
        (True, 0, "fresh", "candidate_rank_invalid"),
        ("false", 1, "fresh", "rank_gate_setting_invalid"),
    ],
    ids=("no-result", "stale-rank", "invalid-generation", "bool-rank", "zero-rank", "bad-flag"),
)
def test_gate_refuses_incomplete_or_inconsistent_results(
    enabled: object, rank: object, consumer_reason: object, reason: str
) -> None:
    decision = rank_gate().decide_rank_gate(
        enabled=enabled, rank=rank, consumer_reason=consumer_reason
    )
    assert (decision.status.value, decision.reason) == ("refuse", reason)


def test_rank_gate_registry_records_the_uncalibrated_default() -> None:
    row = registry.RANK_GATE_ENABLED
    assert row.name == "rank_gate_enabled" and row.value is False
    assert row.unit == "boolean" and row.calibration_state == "UNCALIBRATED"
    assert row.source.strip()
    assert isinstance(row.last_change, date)

    gate = rank_gate()
    assert gate.decide_rank_gate(enabled=row.value) == gate.decide_rank_gate()

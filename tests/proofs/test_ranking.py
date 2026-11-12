"""Fixture witnesses for ADR-0003, not a market cohort or strategy trial."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from nautilus_trader.common import MessageBus
from nautilus_trader.model import (
    AggressorSide,
    CustomData,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TraderId,
    TradeTick,
)

from sextant.ranking.baseline import BaselineSample, BaselineTable
from sextant.ranking.engine import ReferencePrices, Segment, advance_mover, rank_movers
from sextant.ranking.generation import (
    Coverage,
    CoverageState,
    RankingGeneration,
    decode_arrow,
    encode_arrow,
    generation_digest,
)
from sextant.ranking.publisher import AssetState, RankingPublisher, ranking_data_type
from sextant.ranking.reader import GenerationReader
from sextant.ranking.registry import Registry, RegistryRow

ROOT = Path(__file__).parents[2]
D = Decimal
NS = 1_000_000_000
DAY = date(2026, 10, 27)
START = 1_793_088_000 * NS  # 2026-10-27 04:00 ET, explicit fixture boundary.
SCOPE = Segment("PRE", DAY, START, START + 19_800 * NS, "fixture XNYS boundary")


def registry() -> Registry:
    # These exercise mechanisms only. There is no production registry/default.
    values = {
        "mover_min_move": (D("0.03"), "fraction"),
        "mover_min_trades_accel": (D("1.5"), "ratio"),
        "mover_min_dvol_ratio": (D("3"), "ratio"),
        "move_1m_window_s": (60, "seconds"),
        "move_5m_window_s": (300, "seconds"),
        "trades_accel_window_s": (60, "seconds"),
        "trades_accel_baseline_min": (5, "minutes"),
        "publish_min_interval_s": (1, "seconds"),
        "generation_max_age_s": (2, "seconds"),
        "heartbeat_max_age_s": (2, "seconds"),
        "input_max_age_s": (2, "seconds"),
        "top_n": (2, "symbols"),
    }
    return Registry(
        tuple(
            RegistryRow(name, unit, value, "fixture only; ADR-0003", "UNCALIBRATED", DAY)
            for name, (value, unit) in values.items()
        )
    )


def baseline(*, feed: str = "sip", zero: bool = False) -> BaselineTable:
    return BaselineTable(
        tuple(
            BaselineSample(name, "PRE", "2026-10-26", offset, D(dollars), D(count))
            for name in ("PENNY", "ADR", "LARGE")
            for offset, dollars, count in ((0, 0, 0), (600, 0 if zero else 100, 100))
        ),
        "fixture explicit complete curves; linear within minute",
        START - NS,
        feed,
    )


def tick(second: int, price: str = "0.50", name: str = "PENNY", *, ns: int = 0) -> TradeTick:
    at = START + second * NS + ns
    return TradeTick(
        InstrumentId.from_str(name + ".NASDAQ"),
        Price.from_str(price),
        Quantity.from_str("10"),
        AggressorSide.BUY,
        TradeId(str(at)),
        at,
        at + 1,
    )


def publisher(epoch: str = "fixture-1") -> RankingPublisher:
    bus = MessageBus(TraderId.from_str("RANKING-001"))
    pub = RankingPublisher(
        bus=bus,
        epoch=epoch,
        feed="sip",
        segment=SCOPE,
        registry=registry(),
        universe=("PENNY", "ADR", "LARGE"),
        universe_source="fixture selected symbols; NOT PIT universe",
        baseline=baseline(),
    )
    pub.set_coverage(Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0))
    for name in pub.universe:
        pub.set_symbol(
            name,
            references=ReferencePrices(D("0.50"), START - NS, "fixture official close"),
            asset=AssetState(START - NS, SCOPE.end_ns, False, "common", "fixture vendor"),
        )
    return pub


def warm(pub: RankingPublisher, name: str = "PENNY") -> None:
    for second in (0, 60, 120, 180, 240, 300):
        pub.on_trade(tick(second, name=name))
    for second in (310, 320, 330, 340, 350, 360):
        pub.on_trade(tick(second, "0.55", name))


def test_per_trade_score_uses_own_normal_without_price_or_absolute_dollar_floor() -> None:
    pub = publisher()
    warm(pub)
    row = pub.states["PENNY"].latest
    assert row is not None
    # $30 + $33 against $60 time-matched normal, move 10%, acceleration 6x.
    assert row.move == D("0.1")
    assert row.dvol_ratio == D("1.05")
    assert row.score == D("0.105")
    assert row.trades_accel == D("6")
    assert row.observations["price"] == D("0.55")
    before = row.score
    pub.on_trade(tick(360, "0.60", ns=1))
    latest = pub.states["PENNY"].latest
    assert latest is not None and latest.score != before


def test_deterministic_rank_keeps_entry_exclusions_and_null_scores() -> None:
    pub = publisher()
    warm(pub)
    row = pub.states["PENNY"].latest
    assert row is not None
    rows = rank_movers(
        (
            replace(row, symbol="Z", trades_accel=D(2)),
            replace(row, symbol="B", trades_accel=D(3)),
            replace(row, symbol="A", trades_accel=D(3)),
            replace(row, symbol="UNKNOWN", score=None),
        )
    )
    assert [(r.symbol, r.rank) for r in rows] == [("A", 1), ("B", 2), ("Z", 3)]
    assert rows[0].gates_passed == row.gates_passed  # Failure of ratio gate still ranks.


@pytest.mark.parametrize("missing", ["reference", "prefix", "baseline", "zero_normal"])
def test_missing_inputs_never_manufacture_a_score(missing: str) -> None:
    pub = publisher()
    if missing == "reference":
        pub.set_symbol(
            "PENNY",
            references=ReferencePrices(None, START - NS, "fixture official close"),
            asset=None,
        )
    if missing == "baseline":
        pub.baseline = replace(pub.baseline, samples=())
    if missing == "zero_normal":
        pub.baseline = baseline(zero=True)
    if missing == "prefix":
        pub.on_trade(tick(1))
    else:
        pub.on_trade(tick(0))
    pub.on_trade(tick(360, "0.60"))
    row = pub.states["PENNY"].latest
    assert row is not None and row.score is None
    assert row.observations["unknown_reasons"]


def test_time_matched_baseline_is_median_of_session_windows_and_no_extrapolation() -> None:
    table = BaselineTable(
        tuple(
            BaselineSample("X", "PRE", day, offset, D(dv), D(0))
            for day, a, b in (
                ("2026-10-22", 0, 100),
                ("2026-10-23", 100, 105),
                ("2026-10-26", 50, 200),
            )
            for offset, dv in ((0, 0), (60, a), (120, b))
        ),
        "fixture",
        START - NS,
        "sip",
    )
    cumulative = table.cumulative("X", "PRE", 60)
    window = table.window("X", "PRE", 120, 60)
    assert cumulative is not None and cumulative.dollar_volume == D(50)
    assert window is not None and window.dollar_volume == D(100)
    assert table.cumulative("X", "PRE", 121) is None
    assert table.cumulative("X", "OVERNIGHT", 60) is None


def test_price_and_volume_masks_closing_cross_and_future_reference() -> None:
    pub = publisher()
    pub.on_trade(tick(0))
    initial = pub.states["PENNY"]
    for condition in ("6", "M", "9"):
        pub.on_trade(tick(10, "100"), conditions=(condition,))
        assert pub.states["PENNY"] == initial
    pub.on_trade(tick(20, "100"), price_eligible=False)
    row = pub.states["PENNY"].latest
    assert row is not None
    assert row.observations["price"] == D("0.50")
    assert row.observations["dollar_volume"] == D("1005")
    pub.on_trade(tick(30, "0.60"), volume_eligible=False)
    latest = pub.states["PENNY"].latest
    assert latest is not None and latest.observations["dollar_volume"] == D("1005")
    with pytest.raises(ValueError, match="future"):
        advance_mover(
            initial,
            tick(40),
            ReferencePrices(D("0.50"), START + 50 * NS, "fixture future close"),
            pub.baseline,
            segment=SCOPE,
            feed="sip",
            registry=registry(),
        )


def test_source_and_session_change_refuses_spliced_baselines() -> None:
    pub = publisher()
    warm(pub)
    with pytest.raises(ValueError, match="feed"):
        advance_mover(
            pub.states["PENNY"],
            tick(361),
            ReferencePrices(D("0.50"), START - NS, "fixture official close"),
            baseline(feed="boats"),
            segment=SCOPE,
            feed="sip",
            registry=registry(),
        )
    with pytest.raises(ValueError, match="session"):
        advance_mover(
            pub.states["PENNY"],
            tick(361),
            ReferencePrices(D("0.50"), START - NS, "fixture official close"),
            pub.baseline,
            segment=replace(SCOPE, start_ns=START + 300 * NS),
            feed="sip",
            registry=registry(),
        )


def test_publication_uses_elapsed_interval_and_latest_trade_not_clock_buckets() -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 900_000_000
    gen = pub.publish(now=now)
    assert gen is not None and len(gen.rows) == 3
    pub.on_trade(tick(361, "0.60"))
    assert pub.publish(now=now + 200_000_000) is None
    second = pub.publish(now=now + NS)
    assert second is not None
    assert second.generation_id.sequence == 1
    assert second.generation_id.previous_sequence == 0
    assert second.rows[0].score is not None and gen.rows[0].score is not None
    assert second.rows[0].score > gen.rows[0].score


def test_full_and_top_publications_share_identity_and_transport_roundtrips() -> None:
    pub = publisher()
    full, top = [], []
    pub.bus.subscribe(ranking_data_type("sip").topic, full.append)
    pub.bus.subscribe(ranking_data_type("sip", "top").topic, top.append)
    warm(pub)
    gen = pub.publish(now=START + 360 * NS + 100)
    assert gen is not None
    assert full[0].data == gen
    assert top[0].data.generation_id == gen.generation_id
    assert len(top[0].data.rows) <= 2 and not top[0].data.coverage.complete
    restored = RankingGeneration.from_json(gen.to_json())
    assert restored == gen
    assert generation_digest(restored) == sha256(gen.to_json().encode()).hexdigest()
    assert decode_arrow(encode_arrow([gen])) == [gen]


def test_asset_unknown_or_stale_and_halt_deny_entry_but_keep_rank() -> None:
    pub = publisher()
    warm(pub)
    pub.set_symbol(
        "PENNY",
        references=ReferencePrices(D("0.50"), START - NS, "fixture official close"),
        asset=None,
    )
    gen = pub.publish(now=START + 360 * NS + 100)
    assert gen is not None
    row = gen.rows[0]
    assert row.symbol == "PENNY" and row.rank == 1 and row.focus_eligible is False
    assert any(reason.code == "asset_or_halt_unknown" for reason in row.reasons)


def ready_reader(pub: RankingPublisher, now: int) -> GenerationReader:
    reader = GenerationReader(
        registry(),
        feed="sip",
        segment=SCOPE,
        universe_hash=pub.universe_hash,
        baseline_hash=pub.baseline.identity,
    )
    reader.accept_heartbeat(at=now, received_ns=now)
    return reader


def test_reader_refuses_missing_heartbeat_generation_and_stale_evidence() -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 100
    gen = pub.publish(now=now)
    assert gen is not None
    reader = GenerationReader(
        registry(),
        feed="sip",
        segment=SCOPE,
        universe_hash=pub.universe_hash,
        baseline_hash=pub.baseline.identity,
    )
    assert not reader.fresh(now)
    assert reader.accept_generation(CustomData(ranking_data_type("sip"), gen), now=now)
    assert not reader.fresh(now)
    assert reader.reason == "heartbeat_missing"
    reader.accept_heartbeat(at=now, received_ns=now)
    assert reader.fresh(now)
    assert not reader.fresh(now + 2 * NS + 1)
    assert reader.reason in {"heartbeat_stale", "generation_stale", "input_stale"}


@pytest.mark.parametrize(
    "bad", ["partial", "future", "hash", "wrong_feed", "projection", "session"]
)
def test_invalid_generation_replaces_no_previous_good_fallback(bad: str) -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 100
    gen = pub.publish(now=now)
    assert gen is not None
    reader = ready_reader(pub, now)
    assert reader.accept_generation(CustomData(ranking_data_type("sip"), gen), now=now)
    gen = replace(gen, generation_id=replace(gen.generation_id, sequence=1, previous_sequence=0))
    dtype = ranking_data_type("sip")
    if bad == "partial":
        gen = replace(gen, coverage=replace(gen.coverage, complete=False))
    elif bad == "future":
        gen = replace(gen, known_at=now + 1)
    elif bad == "hash":
        gen = replace(gen, profile_version="other")
    elif bad == "wrong_feed":
        dtype = ranking_data_type("boats")
    elif bad == "projection":
        dtype = ranking_data_type("sip", "top")
    elif bad == "session":
        gen = replace(gen, session_date=date(2026, 10, 28))
    assert not reader.accept_generation(CustomData(dtype, gen), now=now)
    assert not reader.fresh(now) and reader.generation is None


def test_epoch_restart_and_sequence_gap_require_new_coherent_evidence() -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 100
    first = pub.publish(now=now)
    assert first is not None
    reader = ready_reader(pub, now)
    dtype = ranking_data_type("sip")
    assert reader.accept_generation(CustomData(dtype, first), now=now)
    gap = replace(
        first, generation_id=replace(first.generation_id, sequence=3, previous_sequence=2)
    )
    assert not reader.accept_generation(CustomData(dtype, gap), now=now)
    assert not reader.fresh(now)
    restarted = replace(
        first, generation_id=replace(first.generation_id, producer_epoch="fixture-2")
    )
    assert not reader.accept_generation(CustomData(dtype, restarted), now=now)
    reader.accept_heartbeat(at=now + 1, received_ns=now + 1)
    resumed = replace(
        restarted, generation_id=replace(restarted.generation_id, sequence=1, previous_sequence=0)
    )
    assert reader.accept_generation(CustomData(dtype, resumed), now=now + 1)
    assert reader.fresh(now + 1)
    assert not reader.accept_generation(CustomData(dtype, gap), now=now + 1)


def test_registry_requires_every_explicit_sourced_row_and_refuses_float() -> None:
    reg = registry()
    with pytest.raises(ValueError, match="missing"):
        Registry(reg.rows[:-1])
    with pytest.raises(ValueError, match="source"):
        Registry((replace(reg.rows[0], source=""), *reg.rows[1:]))
    with pytest.raises((TypeError, ValueError), match="never float"):
        Registry((replace(reg.rows[0], value=0.03), *reg.rows[1:]))
    assert Registry(tuple(reversed(reg.rows))).params_hash == reg.params_hash


def test_reader_refuses_rank_without_features_or_with_a_fabricated_score() -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 100
    gen = pub.publish(now=now)
    assert gen is not None
    for row in (
        replace(gen.rows[0], features=(), focus_eligible=True),
        replace(gen.rows[0], score=D("999")),
        replace(
            gen.rows[0], features=tuple(replace(f, known_at=None) for f in gen.rows[0].features)
        ),
    ):
        reader = ready_reader(pub, now)
        tampered = replace(gen, rows=(row, *gen.rows[1:]))
        assert not reader.accept_generation(CustomData(ranking_data_type("sip"), tampered), now=now)
        assert reader.entry_rank("PENNY", now=now) is None


def test_reader_refuses_mixed_source_identity_and_duplicate_ranks() -> None:
    pub = publisher()
    warm(pub)
    warm(pub, "ADR")
    now = START + 360 * NS + 100
    gen = pub.publish(now=now)
    assert gen is not None
    for row in (
        replace(gen.rows[1], rank=1),
        replace(gen.rows[1], reasons=(replace(gen.rows[1].reasons[0], detail='{"feed":"sip"}'),)),
    ):
        reader = ready_reader(pub, now)
        assert not reader.accept_generation(
            CustomData(
                ranking_data_type("sip"), replace(gen, rows=(gen.rows[0], row, gen.rows[2]))
            ),
            now=now,
        )


def test_asset_expiry_and_symbol_price_freshness_apply_at_entry_time() -> None:
    pub = publisher()
    pub.baseline = replace(
        pub.baseline,
        samples=tuple(
            replace(sample, dollar_volume=sample.dollar_volume / 10)
            for sample in pub.baseline.samples
        ),
    )
    now = START + 360 * NS + 100
    pub.set_symbol(
        "PENNY",
        references=ReferencePrices(D("0.50"), START - NS, "fixture official close"),
        asset=AssetState(START, now + 1, False, "adr", "fixture asset"),
    )
    warm(pub)
    gen = pub.publish(now=now)
    assert gen is not None and gen.rows[0].focus_eligible
    reader = ready_reader(pub, now)
    assert reader.accept_generation(CustomData(ranking_data_type("sip"), gen), now=now)
    assert reader.entry_rank("PENNY", now=now) == 1
    assert reader.entry_rank("PENNY", now=now + 1) is None


def test_invalid_event_degrades_publication_instead_of_reusing_old_ranks() -> None:
    pub = publisher()
    warm(pub)
    with pytest.raises(ValueError, match="regressed"):
        pub.on_trade(tick(359, "0.90"))
    gen = pub.publish(now=START + 361 * NS)
    assert gen is not None and gen.coverage.state is CoverageState.DEGRADED
    assert all(row.rank is None and not row.focus_eligible for row in gen.rows)


@pytest.mark.parametrize("state", ["halted", "halt_unknown", "expired", "future"])
def test_asset_state_flags_never_remove_display_rank(state: str) -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 100
    asset = AssetState(START, SCOPE.end_ns, False, "common", "fixture")
    if state == "halted":
        asset = replace(asset, halted=True)
    elif state == "halt_unknown":
        asset = replace(asset, halted=None)
    elif state == "expired":
        asset = replace(asset, expires_at=now)
    else:
        asset = replace(asset, known_at=now + 1)
    pub.set_symbol(
        "PENNY", references=ReferencePrices(D("0.50"), START - NS, "fixture"), asset=asset
    )
    gen = pub.publish(now=now)
    assert gen is not None
    assert gen.rows[0].rank == 1 and gen.rows[0].focus_eligible is False


def test_generation_and_input_age_are_checked_with_a_fresh_heartbeat() -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 100
    gen = pub.publish(now=now)
    assert gen is not None
    reader = ready_reader(pub, now)
    assert reader.accept_generation(CustomData(ranking_data_type("sip"), gen), now=now)
    reader.accept_heartbeat(at=now + 3 * NS, received_ns=now + 3 * NS)
    assert not reader.fresh(now + 3 * NS) and reader.reason == "generation_stale"
    stale_input = replace(gen, known_at=now + 3 * NS, ts_init=now + 3 * NS)
    reader = ready_reader(pub, now + 3 * NS)
    assert not reader.accept_generation(
        CustomData(ranking_data_type("sip"), stale_input), now=now + 3 * NS
    )
    assert "stale" in reader.reason


def test_per_symbol_freshness_survives_other_symbols_refreshing_generation() -> None:
    pub = publisher()
    pub.baseline = replace(
        pub.baseline,
        samples=tuple(
            replace(sample, dollar_volume=sample.dollar_volume / 10)
            for sample in pub.baseline.samples
        ),
    )
    warm(pub)
    warm(pub, "ADR")
    pub.on_trade(tick(363, "0.60", "ADR"))
    now = START + 363 * NS + 100
    gen = pub.publish(now=now)
    assert gen is not None
    reader = ready_reader(pub, now)
    assert reader.accept_generation(CustomData(ranking_data_type("sip"), gen), now=now)
    assert reader.entry_rank("ADR", now=now) == 1
    assert reader.entry_rank("PENNY", now=now) is None
    assert reader.reason == "symbol_input_stale"


def test_heartbeat_future_or_regression_revokes_readiness() -> None:
    pub = publisher()
    warm(pub)
    now = START + 360 * NS + 100
    gen = pub.publish(now=now)
    assert gen is not None
    for at, received in ((now + 1, now), (now - 1, now)):
        reader = ready_reader(pub, now)
        assert reader.accept_generation(CustomData(ranking_data_type("sip"), gen), now=now)
        assert not reader.accept_heartbeat(at=at, received_ns=received)
        assert reader.entry_rank("PENNY", now=now) is None


def test_close_cross_without_conditions_is_not_a_post_trade() -> None:
    pub = publisher()
    state = pub.states["PENNY"]
    updated, row = advance_mover(
        state,
        tick(0, "1.50"),
        ReferencePrices(D("1"), START - NS, "fixture today's official close"),
        pub.baseline,
        segment=replace(SCOPE, name="POST"),
        feed="sip",
        registry=registry(),
    )
    assert updated == state and row is None


def test_changed_parameters_and_future_baseline_cannot_reuse_history() -> None:
    pub = publisher()
    warm(pub)
    rows = tuple(
        replace(r, value=2) if r.name == "publish_min_interval_s" else r for r in registry().rows
    )
    with pytest.raises(ValueError, match="parameters changed"):
        advance_mover(
            pub.states["PENNY"],
            tick(361),
            pub.references["PENNY"],
            pub.baseline,
            segment=SCOPE,
            feed="sip",
            registry=Registry(rows),
        )
    future = replace(
        pub.baseline,
        samples=tuple(replace(s, session=DAY.isoformat()) for s in pub.baseline.samples),
    )
    with pytest.raises(ValueError, match="baseline session"):
        advance_mover(
            pub.states["PENNY"],
            tick(361),
            pub.references["PENNY"],
            future,
            segment=SCOPE,
            feed="sip",
            registry=registry(),
        )


def test_missing_or_invalid_generation_emits_a_refusal_alarm() -> None:
    pub = publisher()
    alarms = []
    reader = GenerationReader(
        registry(),
        feed="sip",
        segment=SCOPE,
        universe_hash=pub.universe_hash,
        baseline_hash=pub.baseline.identity,
        alarm=alarms.append,
    )
    assert not reader.fresh(START)
    assert alarms == ["heartbeat_missing"]
    assert not reader.accept_generation(None, now=START)
    assert alarms[-1] == "wrong generation topic"


def test_k2_preregistration_stays_frozen_and_not_run() -> None:
    note = (ROOT / "docs/research/2026-11-06-ranking.md").read_bytes()
    title = b"# K2 historical ranking evaluation: preregistration (Wave W2)\n"
    section = note[note.rfind(title) :]
    digest = "fc019b18602429f0f641588eed45467ca555f4bbc568284327f1a346dd33426f"
    assert sha256(section).hexdigest() == digest
    text = section.decode()
    assert "**Status: frozen protocol; NOT_RUN.**" in text
    for heading in (
        "## 3. Point-in-time cohort (frozen)",
        "## 4. Truth and onset",
        "## 5. Generations and recall@30/100 per session (frozen)",
        "## 6. Matched non-mover controls (frozen)",
        "## 7. Trial count bound to the one TrialLog (frozen)",
        "## 9. Execution seal",
    ):
        assert heading in text
    folded = "docs/research/2026-11-09-k2-historical-ranking-preregistration.md"
    assert not (ROOT / folded).exists()


def test_k3_preregistration_stays_frozen_and_not_run() -> None:
    note = (ROOT / "docs/research/2026-11-06-ranking.md").read_bytes()
    title = b"# K3 early-stage detection: preregistration\n"
    end = b"## K2 preregistration, frozen (Wave W2 fold; NOT_RUN)"
    section = note[note.index(title) : note.index(end)]
    digest = "fecd378a5b40fc2d6d2e03b16dda605c9f82102dad8e95735ffef24a459aca0b"
    assert sha256(section).hexdigest() == digest
    text = section.decode()
    assert "**Status: frozen protocol; NOT_RUN.**" in text
    for heading in (
        "## K3.2 Estimand and pairing (frozen)",
        "## K3.4 Data, PIT and windows (frozen)",
        "## K3.5 Arms (frozen; all MODELED, rejects included)",
        "## K3.7 Costs (frozen: none consumed)",
        "## K3.8 Parameters (frozen: names and authority, no defaults)",
        "## K3.9 Dependence, replication, multiplicity; metrics and tests",
        "## K3.10 Trial count bound to the one TrialLog (frozen)",
        "## K3.11 Decision rule (frozen: HOLD)",
    ):
        assert heading in text
    for arm in ("**Arm (a)", "**Arm (b)", "**Arm (c)", "**Arm (d)", "**Arm K", "**Arm H"):
        assert arm in text
    assert "Rejected arms, recorded not deleted" in text
    for standalone in (
        "docs/research/2026-11-09-k3-onset.md",
        "docs/research/2026-11-09-k3-onset-preregistration.md",
        "docs/research/2026-11-09-k3-preregistration.md",
    ):
        assert not (ROOT / standalone).exists()


def test_successor_n1_preregistration_stays_frozen_and_not_run() -> None:
    note = (ROOT / "docs/research/2026-11-06-ranking.md").read_bytes()
    title = (
        "# Successor N1: first-leg entries into B's protection at trade-print "
        "fidelity — preregistration\n"
    ).encode()
    end = b"## K3 preregistration, frozen (NOT_RUN)"
    section = note[note.index(title) : note.index(end)]
    digest = "d51a9621403626f0bd6a06eec16fd7cae1b6581be5ad866fe4e3103e4ba33bed"
    assert sha256(section).hexdigest() == digest
    text = section.decode()
    assert "**Status: frozen protocol; NOT_RUN.**" in text
    assert "fecd378a5b40fc2d6d2e03b16dda605c9f82102dad8e95735ffef24a459aca0b" in text
    for heading in (
        "## N1.2 Estimand and pairing (frozen)",
        "## N1.4 Data, PIT and windows (frozen)",
        "## N1.5 Arms and the shared entry/protection chain (frozen)",
        "## N1.7 Costs (frozen: identities and levels; values at the seal)",
        "## N1.8 Parameters (frozen: names and authority, no defaults)",
        "## N1.9 Dependence, replication, multiplicity; metrics and tests",
        "## N1.10 Trial count bound to the one TrialLog (frozen)",
        "## N1.11 Decision rule (frozen: HOLD)",
    ):
        assert heading in text
    for arm in ("**Arm T-a", "**Arm T-b", "**Arm T-c", "**Arm T-d", "**Arm T-K", "**Arm T-H"):
        assert arm in text
    assert "entry minute against onset" in text
    assert "protected gain against hold-flat" in text
    assert "1\u00d7 and 2\u00d7 all costs" in text
    assert "Rejected flag sources stay rejected" in text
    for standalone in (
        "docs/research/2026-11-09-successor-n1.md",
        "docs/research/2026-11-09-successor-preregistration.md",
        "docs/research/2026-11-09-n1-b-protection.md",
    ):
        assert not (ROOT / standalone).exists()

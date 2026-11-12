"""N1c: instrument status into the ranking host, setter generations, the bounded pager.

Fixture statuses are published from the catalog into a real BacktestNode. A halt or an
unclassifiable status must never leave a name looking tradable: the ranking refuses it.
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any, cast

import pytest
from nautilus_trader.model import InstrumentId, InstrumentStatus, MarketStatusAction
from test_ranking import NS, START, registry
from test_ranking_actor import FakeHost, Logged, run

from sextant.ranking.node_actor import RankingActorConfig, classify

PENNY = InstrumentId.from_str("PENNY.ALPACA")
OTHER = InstrumentId.from_str("OTHER.ALPACA")


def status(at_s: int, action, iid=PENNY, is_trading=None) -> InstrumentStatus:
    at = START + at_s * NS
    return InstrumentStatus(iid, action, at, at, is_trading=is_trading)


class Recording(FakeHost):
    """Records every set_symbol after priming, with the asset and references it carried."""

    def __init__(self, log, fail_at=None) -> None:
        super().__init__(log, fail_at)
        self.assets: list[tuple[str, object, D | None]] = []

    def set_symbol(self, name, *, references, asset) -> Any:
        self.assets.append((name, asset.halted if asset else None, references.prior_close))
        return super().set_symbol(name, references=references, asset=asset)


class Publishing(Recording):
    """A host whose setters return the generation they publish (the later host change)."""

    def set_symbol(self, name, *, references, asset) -> Any:
        super().set_symbol(name, references=references, asset=asset)
        return self.publisher.publish(now=asset.known_at) if asset else None


def statuses_after_priming(host: Recording) -> list[tuple[str, object, D | None]]:
    return host.assets[3:]  # the first three are the primed universe


def test_halt_resume_and_unknown_reach_the_host_with_the_primed_references(tmp_path) -> None:
    feed = [
        status(30, MarketStatusAction.HALT),
        status(90, MarketStatusAction.TRADING, is_trading=True),
        status(150, MarketStatusAction.NONE),  # no classification: never "trading"
    ]
    out = run(tmp_path, statuses=feed, host_cls=Recording)
    assert statuses_after_priming(out["hosts"][0]) == [
        ("PENNY", True, D("0.50")),
        ("PENNY", False, D("0.50")),
        ("PENNY", None, D("0.50")),
    ]


@pytest.mark.parametrize(
    ("action", "is_trading", "halted"),
    [
        (MarketStatusAction.HALT, None, True),
        (MarketStatusAction.PAUSE, None, True),
        (MarketStatusAction.SUSPEND, None, True),
        (MarketStatusAction.NOT_AVAILABLE_FOR_TRADING, None, True),
        (MarketStatusAction.TRADING, True, False),
        (MarketStatusAction.TRADING, None, False),
        (MarketStatusAction.TRADING, False, None),  # contradictory: unknown
        (MarketStatusAction.NONE, None, None),
        (MarketStatusAction.NONE, True, None),
        (MarketStatusAction.PRE_OPEN, None, None),
        (MarketStatusAction.QUOTING, None, None),
        (MarketStatusAction.CLOSE, None, None),
        (MarketStatusAction.SHORT_SELL_RESTRICTION_CHANGE, True, None),
    ],
)
def test_the_classification_never_guesses_trading(action, is_trading, halted) -> None:
    assert classify(status(0, action, is_trading=is_trading)) is halted


def test_a_status_outside_the_universe_is_ignored_and_counted(tmp_path) -> None:
    class Wider(Logged):
        def on_start(self) -> None:
            super().on_start()
            self.subscribe_instrument_status(OTHER)  # as a wider data client push would

    from test_ranking_actor import equity

    feed = [status(30, MarketStatusAction.HALT, iid=OTHER)]
    out = run(tmp_path, statuses=feed, host_cls=Recording, actor_cls=Wider, extra=[equity(OTHER)])
    assert statuses_after_priming(out["hosts"][0]) == []
    assert out["actor"].ignored_statuses == 1


def test_a_generation_returned_by_a_setter_is_published_at_once(tmp_path) -> None:
    feed = [status(305, MarketStatusAction.HALT)]  # between trades at 300 s and 310 s
    out = run(tmp_path, statuses=feed, host_cls=Publishing)
    at = START + 305 * NS
    published = [g for g in out["receiver"].received if g.known_at == at]
    assert len(published) == 1  # reached the node bus before the next trade
    (penny,) = [row for row in published[0].rows if row.symbol == "PENNY"]
    assert penny.focus_eligible is False  # the halted name is refused by the ranking


def test_an_error_alternating_with_successes_pages_once_per_window(tmp_path) -> None:
    class Flaky(FakeHost):
        def on_trade(self, tick, *, facts=None):
            self.calls += 1
            if self.calls % 2:
                raise RuntimeError("fixture host failure")
            self.publisher.on_trade(tick)
            return self.publisher.publish(now=tick.ts_init)

    out = run(tmp_path, host_cls=Flaky, window=3600)
    assert out["actor"].errors["on_trade"] == 6 and [r for r, _ in out["pages"]] == ["on_trade"]
    out = run(tmp_path / "short", host_cls=Flaky, window=1)
    assert len(out["pages"]) == 6  # every failure is more than a second apart in the tape


def test_the_page_window_has_no_default() -> None:
    with pytest.raises(TypeError):
        RankingActorConfig(  # type: ignore[call-arg]
            instrument_ids=(PENNY,), feed="sip", host_factory=FakeHost, priming=lambda: None
        )
    with pytest.raises(TypeError, match="page_windows"):  # a misspelled key is refused
        RankingActorConfig(  # type: ignore[call-arg]
            instrument_ids=(PENNY,),
            feed="sip",
            host_factory=cast(Any, FakeHost),
            priming=cast(Any, lambda: None),
            page_window_s=60,
            trade_facts=None,
            page_windows=60,
        )


# --- Review round 1 (Codex xhigh at ba990fd): the exact-head witnesses as proofs ----------


def test_a_status_delivered_during_priming_is_kept_and_applied(tmp_path) -> None:  # F1
    class EarlyStatus(Logged):
        def on_start(self) -> None:
            original = self._priming

            def prime_with_status():
                self.on_instrument_status(status(-1, MarketStatusAction.HALT))
                assert not self.primed
                return original()

            self._priming = prime_with_status
            super().on_start()

    out = run(tmp_path, actor_cls=EarlyStatus, host_cls=Recording)
    assert out["hosts"][0].publisher.assets["PENNY"].halted is True
    assert statuses_after_priming(out["hosts"][0]) == [("PENNY", True, D("0.50"))]


@pytest.mark.parametrize("offset", [-1, -2], ids=["reused", "regressed"])
def test_a_setter_generation_never_reuses_or_regresses(tmp_path, offset) -> None:  # F3
    class OldGeneration(FakeHost):
        def __init__(self, log, fail_at=None) -> None:
            super().__init__(log, fail_at)
            self.history: list[Any] = []

        def on_trade(self, tick, *, facts=None):
            generation = super().on_trade(tick)
            if generation is not None:
                self.history.append(generation)
            return generation

        def set_symbol(self, name, *, references, asset) -> Any:
            super().set_symbol(name, references=references, asset=asset)
            return self.history[offset] if len(self.history) >= abs(offset) else None

    out = run(tmp_path, host_cls=OldGeneration, statuses=[status(361, MarketStatusAction.HALT)])
    sequences = [g.generation_id.sequence for g in out["receiver"].received]
    assert sequences == sorted(set(sequences)) and out["actor"].errors == {"lineage": 1}


def test_a_status_the_host_cannot_apply_leaves_the_name_unknown(tmp_path) -> None:  # F2
    class FailingStatusSetter(Recording):
        def set_symbol(self, name, *, references, asset) -> Any:
            # N1d: priming and the first print apply; every later non-clear state fails.
            if name == "PENNY" and asset.halted is not False and asset.known_at > START:
                raise RuntimeError("fixture status setter unavailable")
            return super().set_symbol(name, references=references, asset=asset)

    out = run(
        tmp_path,
        host_cls=FailingStatusSetter,
        statuses=[status(355, MarketStatusAction.HALT)],
        facts=lambda _tick: (True, ()),  # PENNY is cleared by its first print, then HALT fails
    )
    last = out["receiver"].received[-1]
    (penny,) = [row for row in last.rows if row.symbol == "PENNY"]
    assert penny.focus_eligible is False and penny.rank is None
    assert last.coverage.state.name == "DEGRADED"  # withheld while the status is unapplied
    assert out["actor"].errors["instrument_status"] >= 1 and out["actor"].vetoed == {"PENNY"}
    assert out["consumer"].reader.generation is None  # no readable rank


def test_a_status_veto_lifts_once_the_host_applies_it(tmp_path) -> None:  # F2 recovery
    class FailsOnce(Recording):
        failed = False

        def set_symbol(self, name, *, references, asset) -> Any:
            unknown = asset.halted is None and asset.known_at > START
            if name == "PENNY" and unknown and not self.failed:
                self.failed = True
                raise RuntimeError("fixture status setter unavailable once")
            return super().set_symbol(name, references=references, asset=asset)

    out = run(
        tmp_path,
        host_cls=FailsOnce,
        statuses=[status(305, MarketStatusAction.NONE)],
        facts=lambda _tick: (True, ()),
    )
    assert out["actor"].vetoed == set() and out["actor"].errors == {"instrument_status": 1}
    moves = statuses_after_priming(out["hosts"][0])
    assert ("PENNY", None, D("0.50")) in moves  # the unknown state applied on the retry


def test_a_refused_halt_withdraws_the_readable_rank_at_once(tmp_path) -> None:  # review P2
    from sextant.ranking.heartbeat import HeartbeatEmitter

    class RefusesHalt(Recording):
        def set_symbol(self, name, *, references, asset) -> Any:
            if name == "PENNY" and asset.halted is True:
                raise RuntimeError("fixture halt setter unavailable")
            return super().set_symbol(name, references=references, asset=asset)

    out = run(
        tmp_path,
        host_cls=RefusesHalt,
        statuses=[status(361, MarketStatusAction.HALT)],
        facts=lambda _tick: (True, ()),  # PENNY cleared by its first print, as in N1d
    )
    consumer, at = out["consumer"], START + 361 * NS
    beat = HeartbeatEmitter(registry=registry(), clock=lambda: at, sink=consumer.accept_heartbeat)
    beat.emit(out["receiver"].received[-2])  # a beat for the last READY generation
    assert out["actor"].vetoed == {"PENNY"}
    assert out["receiver"].received[-1].coverage.state.name == "DEGRADED"  # the veto, at once
    assert consumer.entry_rank("PENNY", now=at) is None

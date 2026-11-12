"""Pure projection from `sextant.j5-ranked/1` to the display unit `inputs.py` reads.

Sources: northstar-trading@8256c16c31dcb8e53fc726e0c1c77dc2c8d0d4fa
`scanner/display_lists.py:validate_display_contract` and
`tradingview/research.py` via `inputs.py`; `producer.LiveProducer.build_unit`
for the j5 document; `ranking/reader.py` stateless checks where no registry,
heartbeat or sequence state is available. No I/O, clock, ranking or sync here.
Display bounds have no j5 source and no default: the caller supplies sourced
`DisplayBounds` (ADR-0018), or the projection stays unavailable. The target
contract carries no separate expiry field: the projection narrows the display
bound's `max_age_s` to the remaining J5 lifetime, and `read_research_inputs`
refuses a generation past that bound; this projection never extends the j5
document's `expires_at_ns`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sextant.ranking.generation import CoverageState, RankingGeneration, generation_digest
from sextant.ranking.publisher import METHOD, RANKER, universe_digest
from sextant.tradingview.inputs import (
    DISPLAY_PAPER_SLOT,
    EPOCH,
    QUALIFIED,
    TICKER,
    InputError,
    _ns,
    _session,
    timestamp,
)

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED = {
    "schema",
    "display_only",
    "paper_slot",
    "status",
    "generation_sha256",
    "params_hash",
    "generated_at_ns",
    "expires_at_ns",
    "ranking_generation",
    "watchlists",
    "provenance",
}
KEY_FEATURES = ("price", "move", "dvol_ratio", "trades_accel")
MAX_MEMBERS = 100
MAX_LAG_NS = 600 * 1_000_000_000


class BridgeError(InputError):
    """A refused or unavailable j5 document; never an empty desired list."""


@dataclass(frozen=True, slots=True)
class DisplayBounds:
    """Legacy display bounds, validated as metadata exactly as `inputs.py` does."""

    gap_bound: Decimal
    rvol_bound: Decimal
    max_age_s: int

    def __post_init__(self) -> None:
        if (
            type(self.gap_bound) is not Decimal
            or type(self.rvol_bound) is not Decimal
            or not self.gap_bound.is_finite()
            or not self.rvol_bound.is_finite()
            or self.gap_bound <= 0
            or self.rvol_bound <= 0
            or type(self.max_age_s) is not int
            or self.max_age_s <= 0
        ):
            raise BridgeError("Sourced positive Decimal display bounds required")


@dataclass(frozen=True, slots=True)
class DisplayUnit:
    """The three JSON-ready documents of one producer unit, in rank order."""

    top100: dict[str, Any]
    focus: dict[str, Any]
    display_lists: dict[str, Any]

    def documents(self) -> dict[str, dict[str, Any]]:
        return {
            "top100.json": self.top100,
            "focus.json": self.focus,
            "display-lists.json": self.display_lists,
        }


def _rfc(ns: int) -> str:
    if type(ns) is not int or ns < 0:
        raise BridgeError("Generation clocks must be nonnegative nanoseconds")
    try:
        seconds, remainder = divmod(ns, 1_000_000_000)
        at = EPOCH + timedelta(seconds=seconds)
        fraction = f".{remainder:09d}" if remainder else ""
        text = (
            f"{at.year:04d}-{at.month:02d}-{at.day:02d}T"
            f"{at.hour:02d}:{at.minute:02d}:{at.second:02d}{fraction}+00:00"
        )
        if _ns(text) != ns:
            raise BridgeError("Generation clock does not round-trip as RFC3339")
        return text
    except (OverflowError, InputError) as error:
        if isinstance(error, BridgeError):
            raise
        raise BridgeError("Generation clock does not round-trip as RFC3339") from None


def _member(symbol: str, qualified: str, rank: int) -> dict[str, Any]:
    return {"symbol": symbol, "tradingview_symbol": qualified, "labels": [f"rank:{rank}"]}


def _generation(j5: Mapping[str, Any]) -> RankingGeneration:
    raw = j5["ranking_generation"]
    if not isinstance(raw, Mapping):
        raise BridgeError("j5 ranking generation must be one document")
    try:
        generation = RankingGeneration.from_json(dict(raw))
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        raise BridgeError("Invalid j5 ranking generation; lists NOT synced") from None
    if generation_digest(generation) != j5["generation_sha256"]:
        raise BridgeError("j5 generation digest mismatch; lists NOT synced")
    coverage = generation.coverage
    if not (
        coverage.state is CoverageState.READY
        and coverage.complete
        and coverage.expected_symbols == coverage.observed_symbols == len(generation.rows)
        and coverage.missing_symbols == coverage.invalid_symbols == coverage.late_symbols == 0
    ):
        raise BridgeError("j5 generation coverage incomplete; lists NOT synced")
    if (
        generation.method_version != METHOD
        or generation.ranker_version != RANKER
        or generation.profile_version != "registry/" + str(j5["params_hash"])
    ):
        raise BridgeError("j5 generation identity mismatch; lists NOT synced")
    if not (
        generation.ts_event <= generation.input_cutoff <= generation.known_at <= generation.ts_init
    ):
        raise BridgeError("j5 generation clocks inconsistent; lists NOT synced")
    _sources(generation, str(j5["params_hash"]))
    _ranks(generation)
    return generation


def _sources(generation: RankingGeneration, params_hash: str) -> dict[str, Any]:
    first: dict[str, Any] | None = None
    for row in generation.rows:
        details = [reason.detail for reason in row.reasons if reason.code == "SOURCE"]
        if len(details) != 1 or details[0] is None:
            raise BridgeError("j5 generation source missing; lists NOT synced")
        try:
            source = json.loads(details[0])
        except ValueError:
            raise BridgeError("j5 generation source invalid; lists NOT synced") from None
        if not isinstance(source, dict) or (first is not None and source != first):
            raise BridgeError("j5 generation sources mixed; lists NOT synced")
        first = source
    if first is None:
        raise BridgeError("j5 generation has no rows; lists NOT synced")
    start, end = first.get("start_ns"), first.get("end_ns")
    if (
        first.get("feed") != "sip"
        or first.get("segment") not in {"PRE", "RTH", "POST"}
        or first.get("params_hash") != params_hash
        or first.get("universe_hash")
        != universe_digest(tuple(row.symbol for row in generation.rows))
        or type(start) is not int
        or type(end) is not int
        or not start <= generation.ts_event
        or not generation.ts_init < end
        or not all(
            isinstance(first.get(key), str) and first[key]
            for key in ("session_source", "universe_source", "baseline_source", "baseline_hash")
        )
    ):
        raise BridgeError("j5 generation source mismatch; lists NOT synced")
    for row in generation.rows:
        for feature in row.features:
            if (
                feature.value is not None and (feature.ts_event is None or feature.known_at is None)
            ) or (
                feature.known_at is not None
                and (
                    feature.ts_event is None
                    or not start <= feature.ts_event <= feature.known_at <= generation.known_at
                )
            ):
                raise BridgeError("j5 feature clocks invalid; lists NOT synced")
    return first


def _ranks(generation: RankingGeneration) -> None:
    scored = []
    for row in generation.rows:
        values = {feature.name: feature.value for feature in row.features}
        if row.score is not None:
            move, ratio = values.get("move"), values.get("dvol_ratio")
            if move is None or ratio is None or row.score != move * ratio:
                raise BridgeError("j5 score does not match its features; lists NOT synced")
            if values.get("trades_accel") is not None:
                scored.append(row)
        elif row.rank is not None:
            raise BridgeError("j5 rank without score; lists NOT synced")
    expected = {
        row.symbol: index
        for index, row in enumerate(
            sorted(
                scored,
                key=lambda item: (
                    -item.score,  # type: ignore[operator]
                    -next(f.value for f in item.features if f.name == "trades_accel"),  # type: ignore[arg-type]
                    item.symbol,
                ),
            ),
            1,
        )
    }
    if any(row.rank != expected.get(row.symbol) for row in generation.rows):
        raise BridgeError("j5 rank order mismatch; lists NOT synced")


def project_display_unit(j5: Mapping[str, Any], *, bounds: DisplayBounds) -> DisplayUnit:
    """Project one fresh j5 document, or raise `BridgeError` with no output.

    Movers are the ranked rows paired in order with `NS Ranked`; focus is the
    paired subset whose `focus_eligible` is true. No armed name, bound, clock
    or mapping is invented, and more than 100 members refuse untruncated.
    """
    try:
        if not isinstance(bounds, DisplayBounds) or not isinstance(j5, Mapping):
            raise BridgeError("One j5 document and sourced display bounds required")
        if j5.get("schema") != "sextant.j5-ranked/1" or j5.get("status") != "fresh":
            raise BridgeError("j5 generation is unavailable; lists NOT synced")
        if (
            not REQUIRED <= j5.keys()
            or j5["display_only"] is not True
            or type(j5["paper_slot"]) is not int
            or j5["paper_slot"] != DISPLAY_PAPER_SLOT
            or not isinstance(j5["generation_sha256"], str)
            or not SHA256.fullmatch(j5["generation_sha256"])
            or not isinstance(j5["params_hash"], str)
            or not SHA256.fullmatch(j5["params_hash"])
            or type(j5["generated_at_ns"]) is not int
            or type(j5["expires_at_ns"]) is not int
        ):
            raise BridgeError("Invalid j5 document identity; lists NOT synced")
        generation = _generation(j5)
        if (
            j5["generated_at_ns"] != generation.known_at
            or not generation.known_at < j5["expires_at_ns"]
            or generation.input_cutoff - generation.ts_event > MAX_LAG_NS
        ):
            raise BridgeError("j5 generation expired or lagged; lists NOT synced")
        clocks = (
            _rfc(generation.ts_event),
            _rfc(generation.input_cutoff),
            _rfc(generation.known_at),
        )
        session = generation.session_date.isoformat()
        if any(_session(timestamp(text)) != (session, False) for text in clocks):
            raise BridgeError("j5 generation session mismatch; lists NOT synced")
        watchlists = j5["watchlists"]
        ranked = sorted(
            (row for row in generation.rows if row.rank is not None),
            key=lambda row: row.rank,  # type: ignore[arg-type]
        )
        if (
            not isinstance(watchlists, Mapping)
            or set(watchlists) != {"NS Ranked"}
            or not isinstance(watchlists["NS Ranked"], list)
        ):
            raise BridgeError("j5 ranked membership missing; lists NOT synced")
        qualified_members: list[str] = watchlists["NS Ranked"]
        if not 1 <= len(qualified_members) <= MAX_MEMBERS or len(qualified_members) > len(ranked):
            raise BridgeError("j5 ranked membership empty or oversize; lists NOT synced")
        paired = ranked[: len(qualified_members)]
        if (
            len(set(qualified_members)) != len(qualified_members)
            or any(
                not isinstance(member, str)
                or not QUALIFIED.fullmatch(member)
                or member.split(":", 1)[0] in {"SMART", "IB", "IBKR", "ALPACA"}
                for member in qualified_members
            )
            or any(not TICKER.fullmatch(row.symbol) for row in paired)
        ):
            raise BridgeError("j5 ranked mappings inexact or aliased; lists NOT synced")
        for row in paired:
            features = {feature.name: feature for feature in row.features}
            if (
                row.partial
                or not row.completed
                or any(
                    name not in features
                    or features[name].value is None
                    or features[name].ts_event is None
                    or features[name].known_at is None
                    for name in KEY_FEATURES
                )
            ):
                raise BridgeError("j5 projected row incomplete; lists NOT synced")
            if row.focus_eligible is True:
                move_1m = features.get("move_1m")
                price = features["price"].value
                if (
                    move_1m is None
                    or move_1m.value is None
                    or move_1m.value <= 0
                    or price is None
                    or price <= 0
                ):
                    raise BridgeError("j5 focus qualification incomplete; lists NOT synced")
        pairs = list(zip(paired, qualified_members, strict=True))

        def members(focus_only: bool) -> list[dict[str, Any]]:
            return [
                _member(row.symbol, member, row.rank)  # type: ignore[arg-type]
                for row, member in pairs
                if not focus_only or row.focus_eligible is True
            ]

        movers, focus = members(False), members(True)
        identity = {"as_of": clocks[0], "sha256": j5["generation_sha256"]}
        remaining_ns = j5["expires_at_ns"] - generation.ts_event
        if remaining_ns <= 0:
            raise BridgeError("j5 generation expired; lists NOT synced")
        display_age_s = min(bounds.max_age_s, remaining_ns // 1_000_000_000)
        if display_age_s <= 0:
            raise BridgeError("j5 generation has no display lifetime; lists NOT synced")
        common = {
            "session_date": session,
            "generated_at": clocks[2],
            "as_of": clocks[1],
            "as_of_snapshot": clocks[0],
            "snapshot_status": "observed",
            "status": "fresh",
            "generation": identity,
        }
        display = {
            "schema": "ns-display-lists/1",
            **common,
            "generation": dict(identity),
            "ns_movers": members(False),
            "ns_focus": members(True),
            "bounds": {
                "gap_bound": str(bounds.gap_bound),
                "rvol_bound": str(bounds.rvol_bound),
                "max_age_s": display_age_s,
            },
            "counts": {"ns_movers": len(movers), "ns_focus": len(focus)},
            "excluded_unknown": sum(row.rank is None for row in generation.rows),
            "armed": "UNVERIFIED",
            "sources": {"snapshot": dict(identity), "armed_sets": []},
        }
        return DisplayUnit(
            {
                "schema": "morning-pack.candidates/1",
                **common,
                "generation": dict(identity),
                "candidates": movers,
            },
            {
                "schema": "top100.focus/1",
                **common,
                "generation": dict(identity),
                "candidates": focus,
            },
            display,
        )
    except BridgeError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OverflowError):
        raise BridgeError("Invalid j5-ranked document; lists NOT synced") from None

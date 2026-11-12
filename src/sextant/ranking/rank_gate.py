"""Default-off entry rank veto over an ADR-0003 consumer result."""

from dataclasses import dataclass
from enum import StrEnum, auto


class RankGateStatus(StrEnum):
    DISABLED = auto()
    PASS = auto()
    REFUSE = auto()


@dataclass(frozen=True, slots=True)
class RankGateDecision:
    status: RankGateStatus
    reason: str


def decide_rank_gate(
    *, enabled: object = False, rank: int | None = None, consumer_reason: str | None = None
) -> RankGateDecision:
    """Evaluate one candidate's rank and reason from the same current ``entry_rank`` call.

    ``DISABLED`` leaves ranking out of this decision. ``PASS`` clears the rank veto only.
    The caller obtains a new consumer result at each enabled decision time.
    """
    if enabled is False:
        return RankGateDecision(RankGateStatus.DISABLED, "rank_gate_disabled")
    if enabled is not True:
        return RankGateDecision(RankGateStatus.REFUSE, "rank_gate_setting_invalid")
    if type(consumer_reason) is not str or not consumer_reason:
        return RankGateDecision(RankGateStatus.REFUSE, "consumer_result_missing")
    if consumer_reason != "fresh":
        return RankGateDecision(RankGateStatus.REFUSE, consumer_reason)
    if rank is None:
        return RankGateDecision(RankGateStatus.REFUSE, "candidate_unranked")
    if type(rank) is not int or rank <= 0:
        return RankGateDecision(RankGateStatus.REFUSE, "candidate_rank_invalid")
    return RankGateDecision(RankGateStatus.PASS, consumer_reason)

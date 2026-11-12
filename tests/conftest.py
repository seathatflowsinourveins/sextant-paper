"""Explicit holds for unchanged Sextant proofs during the paper-only import.

Source: pytest's documented collection-time skip pattern, read 2026-11-11:
https://docs.pytest.org/en/stable/example/simple.html#control-skipping-of-tests-according-to-command-line-option
LEAD-1915 permits held proofs; the dispatch coordinator authorizes this file.
"""

import pytest

HELD_PROOFS = {
    "tests/proofs/test_ranking.py::test_k2_preregistration_stays_frozen_and_not_run": (
        "HELD: reads Sextant's research preregistration, outside the paper import"
    ),
    "tests/proofs/test_ranking.py::test_k3_preregistration_stays_frozen_and_not_run": (
        "HELD: reads Sextant's research preregistration, outside the paper import"
    ),
    "tests/proofs/test_ranking.py::test_successor_n1_preregistration_stays_frozen_and_not_run": (
        "HELD: reads Sextant's research preregistration, outside the paper import"
    ),
    "tests/proofs/test_ranking_heartbeat.py::test_the_entry_age_authority_stays_unavailable": (
        "HELD: reads Sextant's manifest and ranking ADR, replaced by the landscape"
    ),
    "tests/proofs/test_tradingview.py::test_preset_roster_keeps_managed_tickets_plain_limit": (
        "HELD: reads Sextant's mixed-topic TradingView manual, including IBKR"
    ),
    (
        "tests/proofs/test_tradingview.py::"
        "test_preset_import_card_matches_the_reviewed_manifest_hashes"
    ): ("HELD: reads the owner import card in Sextant's excluded TradingView manual"),
    (
        "tests/proofs/test_tradingview_prestage_followups.py::"
        "test_the_3a_arrival_note_is_gone_once_the_loop_ships"
    ): ("HELD: reads Sextant's manifest and excluded TradingView manual"),
    (
        "tests/proofs/test_tradingview_listings.py::"
        "test_the_unit_runs_the_stage_then_restarts_only_a_running_producer"
    ): ("HELD: mixed-file deployment assertion; seat E owns the listings units"),
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        reason = HELD_PROOFS.get(item.nodeid.split("[", 1)[0])
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))

# ADR-0001: Paper loop with ci-only landing

Status: accepted, 2026-11-11. This replaces the template's gate-specific decision; its history remains in Git and [ORIGINS.md](../../ORIGINS.md).

## Context

The owner directed a clean cited harness in the new paper repository, with no landing gate. The repository was created from sextant-harness at initial commit `cc156d07e6d8fda8263a7c1323c641aae65765ac`. Trading code comes later, from a measured import closure.

## Decision

1. Scope is Paper B through NautilusTrader and the Alpaca adapter, plus TradingView ranked-list display. [Design](../design.md) defines the loop and three lanes.
2. The only required check is ci. Keep the template's hosted lint/tests/budgets/secrets fan-in, squash auto-merge, PR-only changes, deletion protection and no force-push. Remove the review/source workflows, Danger window and API reviewers.
3. Clean citations stay inside ci as a test of [landscape](../landscape.md) package coverage. The table/test shape comes from sextant-movers. Upstream references, manifest row and author family remain local checks in `just pr`.
4. Keep all domain hard lines in [AGENTS.md](../../AGENTS.md). Every real paper start, attended or not, requires a Codex max review PASS for the exact SHA's order-sending, lease and key-handling code. That review gates the run, not the merge.
5. Keep short-rule/design budgets from the template. The source-line cap is a single closed placeholder until the lead replaces it from the measured closure report; no production budget is claimed.
6. Preserve session-profile and generated-index checks. Dependabot may propose updates without review secrets; secret-backed upkeep is removed.

## Sources and consequences

Build on the template's ADAPT.md and ORIGINS.md at the initial commit above, and `seathatflowsinourveins/sextant-movers` `85efd95be3ec52dcaee1915ce652c93be2c85bd6`, `tests/repo/test_landscape.py`, read 2026-11-11. [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets), read 2026-11-11, supplies the required-check and allowed-merge-method semantics.

A green merge authorizes no account start. The administrator still applies repository settings/rulesets and the exact Actions allowlist in SETUP.md; this local adaptation does not change GitHub settings.

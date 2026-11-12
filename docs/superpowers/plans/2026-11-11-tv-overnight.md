# TradingView overnight segment implementation plan

> **For agentic workers:** Use executing-plans to implement this plan natively with test-driven-development. No helper agents; the roster arranges the later Codex xhigh review.

**Goal:** Carry a disabled-by-default OVERNIGHT display segment through the existing REST producer and consented cadence.

**Architecture:** Extend the existing five-session display baseline, retaining its close-times-volume recipe and using each historical trading date's previous evening. Select BOATS for intraday bars and SIP for official completed daily closes; preserve publisher, reader, bridge, freshness and consent guards.

**Tech Stack:** alpaca-py 0.44.0, exchange-calendars, existing ranking publisher and display bridge.

**Spec:** LEAD-2218-gate-lineage-and-overnight, section 4; lookup report digest ae8f6cf69be969b740331798e0606811171bd9895ac188473d981a3f0819d486.

## Global constraints

- Local commits only; no push or PR. Do not edit account, auth, optional_get or stop_record.
- No account, TradingView, stream or entitlement probe. Offline fixtures only.
- OVERNIGHT is D-1 20:00 through D 04:00 New York, on XNYS trading dates, including full overnight spans on half-days.
- One sourced overnight_enabled registry row remains zero. New policy rows are UNCALIBRATED.
- Source: alpaca-py v0.44.0 at cc4cb3b7ba50ae250e621983c2779047fb16bb28; official Alpaca hours/history documentation read 2026-11-11. Never select DataFeed.OVERNIGHT.
- Run focused tests with four workers in the roster-approved isolated home and memory scope; full checks through ci-local.sh.

## Review focus

- Midnight continuity and Sunday-to-Monday trading-date identity.
- Historical EST/EDT anchors, missing whole dates and premature known-at receipts.
- Half-day official close availability and holiday/weekend closures.
- BOATS/SIP mismatches and explicit overnight provenance accepted by inputs.
- Disabled producer performs no fetch; overnight scheduling preserves existing consent and stale-input refusal.

### Task 1: Complete overnight display integration

**Files:** Modify tradingview baseline, prestage, producer, host, bridge, cadence and loop; checked-in cadence registry; add tests/proofs/test_tradingview_overnight.py; update manifest and handbook.

**Interfaces:** Keep daytime defaults. Add explicit BOATS feed selection and feed authority at the REST host seam; pass the same feed into publisher and reader. Cadence owns the enable flag and sourced baseline count. Scheduling uses existing XNYS segment resolution.

- [x] Write failing tests for D-1 historical baselines, actual feed identity, missing-date refusal, Sunday/midnight/holiday/half-day clocks, disabled no-fetch, BOATS end-to-end publication and bridge/input admission.
- [x] Run focused tests and retain RED output before implementation.
- [x] Extend existing seams, using BOATS minute requests and SIP daily references with calendar close clocks. Preserve five-session defaults; refuse unavailable history.
- [x] Run focused tests through GREEN; inspect the integration and protected-file diff.
- [ ] Run ci-local, record exact counts, source net and raw diff size; commit locally with the required Codex trailer.
- [ ] Hand review evidence and unknown entitlement/coverage to roster; retain worktree and branch for later review.

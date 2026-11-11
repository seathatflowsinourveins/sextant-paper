# Philosophy

> Copied from Sextant's `docs/handbook/philosophy.md` (see [ORIGINS.md](../../ORIGINS.md)). The sections marked **[Sextant-specific]** are Sextant's trading philosophy, kept as the worked example of a philosophy page: one focus, what never changes, how the project learns, what it refuses and what is still unknown. Write your own in the same shape. **How we learn** and **One practice, one home** carry over as written.

**[Sextant-specific]** How Sextant trades, condensed from the owner's finalized record (a private repository): [northstar-lanes ADR-0003](https://github.com/seathatflowsinourveins/northstar-lanes/blob/00203275944a22655b3e14b221cfde9a265d549a/docs/adr/0003-philosophy-and-operating-principles.md) at `00203275944a22655b3e14b221cfde9a265d549a`. The pillar details are in that repo's ADR-0004 to ADR-0011 at the same commit. If this page and a harvested ADR here ever disagree, the ADR wins. Say so in a PR.

## The four moves [Sextant-specific]
1. **Rank.** Trade only the most potential movers, found as they start. The score is the move times unusual volume against the symbol's own normal, never dollar volume. A stale ranking means no entry.
2. **Enter early.** Enter at the first rising second of the first leg. A late, extended move is entered only on a pullback and reclaim, never at a fresh spike high. Size comes from risk.
3. **Protect.** A sell trigger sits just below the entry, outside the tape's noise, and only ever rises with the high. Once the gain covers its costs, a gain lock holds it. A breach must persist: no exit fires on one quote, one print or one bar. The fast sell then chases within a cap. A wide broker stop is only the outage backstop.
4. **Re-enter.** After every fast sell and a cooldown, buy back on a reclaim while the name still ranks, or rotate into the new top mover. Missing the rest of a move is a loss too.

The owner's emphasis, 2026-11-03: "exit at first non noise signal before and reentry if needed". The order rate is a budget to use, within the broker's limits and through the pacer, with protection first.

## What never changes [Sextant-specific]
- **Custody:** owner orders are never cancelled, sold back or resized. An owner fill is claimed at once and protected. One writer per quantity and per account. Protective actions come first in every pacer.
- **Paper is the proving ground and is never gated.** P&L, loss budgets and counts are recorded with every action and never block. The hard lines on paper are custody, one writer, the pacer, the broker's own limits, the owner's halt, nothing live, no secrets, and money in `Decimal` or the engine's types.
- **Live** moves only through evidence-gated promotion, within owner-approved limits.

## How we learn
- **Every rule comes from the tape, in this order:** capture the incident's exact trades and quotes; write a replay test that fails first; write the rule so the test passes; write the decision, the registry row and the handbook page; record a paper run against hold-flat. No rule is adopted on opinion.
- **Numbers are calibrated, not argued.** Every threshold is a registry row, uncalibrated until a calibration record exists. A row changes between sessions only, when a walk-forward replay beats both the incumbent and hold-flat at stressed costs, beyond its measured noise.
- **Compounding by self-reflection.** Every session ends with a reflection on what each rule did against hold-flat and which movers it missed. Every exit leaves a counterfactual and a lesson. The nightly replay writes proposals, and only the rule above can adopt them.
- **The evaluation loop never idles.** Sandbox and broker-paper tiers run beside an open session on live data, and replay continues otherwise. Outside regular hours every tier sends limit orders only. The loop produces evidence and proposals only.

## What we refuse [Sextant-specific]
- Churn, fake protection, hindsight tuning, live leakage, and TradingView data as a machine-decision input. TradingView stays display-only.
- One-day anecdotes as grounds for a change. Outcomes are read only as of their cutoff, and negative results are kept.
- Short selling or options trading without a further decision first. Options data as a zero-weight research factor is not admission to options trading.

## What is still unknown [Sextant-specific]
Whether early entry, a tight trigger and re-entry beat hold-flat net of costs. The record rates its confidence medium-high for the direction, because it rests on the owner's statements, and low for the economics. The rules above exist to measure that.

## Lessons [Sextant-specific, not included]

Sextant seeds its journal with an append-only `lessons.jsonl` of lesson-v1 records and their JSON schema, carried verbatim from its predecessor project. The records are trading lessons and are not included; the transferable ones are in [LESSONS.md](../LESSONS.md). Records are append-only: a correction is a new record.

## One practice, one home

Each rule of the practice has exactly one home in this repository, and everything else links to it. The order of authority is this page (why) → [the design](../design.md) (the trade path) → [the ADRs](../adr/) (decisions with sources) → [the manifest](../manifest.md) (each pick, its source, its proof) → [AGENTS.md](../../AGENTS.md) and [CLAUDE.md](../../CLAUDE.md) (short operating rules that link, never restate) → settings, hooks, launch definitions and `just` recipes (the rule as mechanism). A rule stated in two places is a defect; the lower layer links upward. A relaunched session needs only this repository, with no ruling file, memory or hand-typed step in between.

Authority (a private ruling file, named by basename and hash): owner direction D-13, LEAD-2359-consolidation-and-relaunch-gate.md, SHA-256 `1f357de2cf709978d46800eb1632c8adaf4841916c84a156e4bd2328b9432d1a`.

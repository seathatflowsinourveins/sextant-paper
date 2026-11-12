# Ecosystem

This repository owns Paper B's engine loop, the TradingView ranked-list display and their harness. It does not own options, IBKR, research, the trial-ledger writer or sim coordination. Start at [design](../design.md), [landscape](../landscape.md) and [delivery lifecycle](lifecycle.md).

## The model mix

The owner names workhorses; pinned model moves need the owner's word. Family rankings follow the vendor and Artificial Analysis Intelligence Index, ignoring gaps under 3 points. Effort settings remain uncalibrated until measured. A real paper start requires the exact-SHA Codex max review PASS in AGENTS.md.

The inherited `config/dsh/` profile and lock remain unchanged, including the snapshot used by `just session-start`. This is local session infrastructure, not a hosted reviewer. Native Codex uses the subscription pool. No API reviewer or review secrets are installed by this repository's CI.

Dependabot proposes dependency updates at the template's retained cooldown. Models and broker/engine pins need a sourced lead review; there is no unattended secret-backed upkeep workflow. Sources: [template provenance](../../ORIGINS.md), [manifest](../manifest.md).

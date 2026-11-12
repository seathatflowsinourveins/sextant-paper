# Paper loop

Rank movers as they start, enter and protect on the paper account through NautilusTrader and the Alpaca adapter (Paper B), and display the ranked list in TradingView. The package name stays `sextant` so the later import can preserve imports and proofs.

1. Rank the most potential movers as they start; a stale or refused ranking supplies no entry.
2. Enter with a plain LIMIT.
3. Protect with broker-held following protection.
4. Sell fast on a persistent breach, then re-enter on a reclaim or rotate to the next ranked mover.
5. Project the ranked list into TradingView for display only, never as a machine signal or an order path.

Paper (`lane-paper`) owns the engine entry, protection, exit and custody steps. TradingView (`lane-tradingview`) owns the display. Harness (`lane-harness`) owns CI, citations and repository practice. No options, IBKR, research lane, trial-ledger writer or sim coordinator lives here.

[AGENTS.md](../AGENTS.md) holds the hard lines and the exact-SHA Codex max PASS rule before any real paper start. `ci` is the only merge check; a green merge is not permission to start the account.

## Baseline and sources

The first adaptation contains the harness only. A separate measured import closure determines the source-line budget and package PRs; later move-only splits preserve function bodies and proofs. No source is copied in this adaptation.

[Landscape](landscape.md) records the official engine, broker SDK and TradingView surface. The trade path follows Sextant's inherited philosophy and the owner's 2026-11-11 direction, recorded in [ADR-0001](adr/0001-sextant-foundation.md); it is not a new strategy or a claim of measured profitability.

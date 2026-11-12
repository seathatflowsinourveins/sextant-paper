# Landscape

One short row per upstream integration in the planned paper loop. Package cells name paths relative to `src/`; `tests/repo/test_landscape.py` refuses uncovered packages and modules, including namespace packages. No trading implementation is present yet. Import PRs must extend these rows for their actual closure before adding code.

Verdicts: **use as is**, **build on**, **keep ours**. Every row names the official upstream and either a full commit with a source location or a docs page with its recorded read date. Community controller pins remain candidates; this table does not authorize their installation or a display session.

The table/test shape is adopted from `seathatflowsinourveins/sextant-movers`, `tests/repo/test_landscape.py` (`_cells`, `_tables`, `_sources`, `uncovered`); the exact read commit is recorded in [the manifest](manifest.md).

## Capabilities

| Capability | Package | Upstream | Pin | Source | Verdict |
|---|---|---|---|---|---|
| Engine, sandbox and live-node API used for paper only | `sextant/paper/`, `sextant/execution/`, `sextant/entry/` | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | v2.0.0rc8 `d53849b216e20f2c122ca01b4dd0da377638b186` | `nautilus_trader/live/__init__.pyi:1191-1193` (LiveNodeBuilder); `nautilus_trader/adapters/sandbox/__init__.pyi:15,102` (sandbox config/factory), recorded in Sextant's manifest | build on |
| Alpaca SDK | `sextant/alpaca/`, `sextant/data/`, `sextant/ranking/` | [alpacahq/alpaca-py](https://github.com/alpacahq/alpaca-py) | v0.44.0 `cc4cb3b7ba50ae250e621983c2779047fb16bb28` | `alpaca/trading/client.py:48`; `alpaca/data/live/stock.py:12` | build on |
| TradingView Desktop/MCP ranked-list display | `sextant/tradingview/` | [TradingView official MCP docs](https://www.tradingview.com/mcp/docs) | docs, read 2026-11-09 | Official MCP inventory and watchlist surface, recorded in Sextant `docs/manifest.md`, “TradingView research membership sync and Pine displays”; Desktop controller candidates are recorded there separately | use as is |

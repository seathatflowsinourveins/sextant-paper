# Landscape

One short row per upstream integration in the planned paper loop. Package cells name paths relative to `src/`; `tests/repo/test_landscape.py` refuses uncovered packages and modules, including namespace packages. The approved source closure is imported byte-identical at the baseline recorded below.

Verdicts: **use as is**, **build on**, **keep ours**. Every row names the official upstream and either a full commit with a source location or a docs page with its recorded read date. Community controller pins remain candidates; this table does not authorize their installation or a display session.

The table/test shape is adopted from `seathatflowsinourveins/sextant-movers`, `tests/repo/test_landscape.py` (`_cells`, `_tables`, `_sources`, `uncovered`); the exact read commit is recorded in [the manifest](manifest.md).

## Capabilities

| Capability | Package | Upstream | Pin | Source | Verdict |
|---|---|---|---|---|---|
| Engine, sandbox and live-node API used for paper only | `sextant/paper/`, `sextant/execution/`, `sextant/entry/` | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | v2.0.0rc8 `d53849b216e20f2c122ca01b4dd0da377638b186` | `python/nautilus_trader/live/__init__.pyi:1145-1193` (LiveNodeBuilder); `python/nautilus_trader/adapters/sandbox/__init__.pyi:15-102` (sandbox config/factory), rechecked at the pin | build on |
| Alpaca SDK | `sextant/alpaca/`, `sextant/data/`, `sextant/ranking/` | [alpacahq/alpaca-py](https://github.com/alpacahq/alpaca-py) | v0.44.0 `cc4cb3b7ba50ae250e621983c2779047fb16bb28` | `alpaca/trading/client.py:48`; `alpaca/data/live/stock.py:12` | build on |
| TradingView MCP ranked-list display | `sextant/tradingview/` | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) | v2.3.0 `2118f14f8a19bc158d8a1cf90af58d85d187f849` | `src/mcp/client/session.py:993-1052` (call_tool); `src/mcp/shared/_httpx_utils.py:13` (default timeout), rechecked at the pin | build on |
| Imported package namespace | `sextant/` | [seathatflowsinourveins/sextant](https://github.com/seathatflowsinourveins/sextant) | `bbbe7f5e51159c346b28117bff93361439d2e5c9` | `src/sextant/__init__.py:1`; keep ours, moved byte-identical | keep ours |
| Exit policy and native lifecycle | `sextant/exits/` | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | v2.0.0rc8 `d53849b216e20f2c122ca01b4dd0da377638b186` | `python/nautilus_trader/trading/__init__.pyi:470-473,538-552`; searched engine strategy/position APIs and carried house-policy origins below; keep ours, moved byte-identical | keep ours |
| Durable filesystem primitives | `sextant/persist.py` | [seathatflowsinourveins/northstar-trading](https://github.com/seathatflowsinourveins/northstar-trading) | `8256c16c31dcb8e53fc726e0c1c77dc2c8d0d4fa` | `src/northstar_trading/persist.py:22-42`; searched the pinned durability implementation (full_fsync/fsync_dir); keep ours, moved byte-identical | keep ours |
| Explicit custody-control registry | `sextant/risk/` | [seathatflowsinourveins/northstar-lanes](https://github.com/seathatflowsinourveins/northstar-lanes) | `00203275944a22655b3e14b221cfde9a265d549a` | `src/northstar_lanes/contracts.py:358-392`; searched the pinned Params registry and carried controls source; keep ours, moved byte-identical | keep ours |

## Imported baseline and budget

Sextant baseline **`bbbe7f5e51159c346b28117bff93361439d2e5c9`** was frozen from origin/main when seat C started. All imported files are **keep ours, moved byte-identical**; integration verdicts above describe the upstream APIs they use. [Import evidence](import-evidence.md) records the scanner, list difference and SHA-256 table for 85 source files and 14 configuration files. The ten runtime defaults and four deployment CLI parameters preserve their original paths.

`SOURCE_LINE_CAP = 25,300` is the lead's uncalibrated cap: 25,000 at the start (LEAD-1915, 2026-11-11, based on the approved 24,071-line closure), raised to 25,300 for the remaining reviewed chain links (LEAD-2046-addendum-2); this baseline has 24,276 source lines after the intervening source change. It ratchets down after move-only splits and is never a measured success criterion. The enforcing row is “Short rules and measured source budget” in [the manifest](manifest.md).

House-policy search provenance: the carried `exits/policy.py:1-13` cites northstar-lanes `00203275944a22655b3e14b221cfde9a265d549a`, `docs/adr/0006-exit-engine.md`, and northstar-trading `8256c16c31dcb8e53fc726e0c1c77dc2c8d0d4fa`, `src/northstar_trading/strategies/exit_manager/rules.py`. The former records that no upstream combines the policy and the owner's existing approval; the native lifecycle uses the engine APIs above. This move preserves that existing policy and authority. The pinned local upstream copies were inspected for the actual API/registry/durability seams; the Sextant manifest was used only to find them, and was not imported.

## Dependency pins

Requirements are copied from Sextant's pyproject at the baseline. The optional `tradingview` extra and development dependencies follow that same baseline. Direct imports supplied transitively by the source lock remain transitive. Offline lock regeneration only pruned unrelated packages and changed destination root metadata: every retained non-root package record, including artifacts/hashes, is identical to the source lock.

| Imported module | Distribution | Exact source-lock version | Availability |
|---|---|---|---|
| `alpaca` | `alpaca-py` | `0.44.0` | runtime |
| `exchange_calendars` | `exchange-calendars` | `4.13.2` | runtime |
| `nautilus_trader` | `nautilus-trader` | `2.0.0rc8` | runtime, source visualization extra |
| `pandas` | `pandas` | `3.1.0` | runtime |
| `pyarrow` | `pyarrow` | `26.0.0` | runtime |
| `yaml` | `pyyaml` | `6.0.3` | runtime |
| `requests` | `requests` | `2.34.2` | runtime, source Git revision retained |
| `mcp` | `mcp` | `2.3.0` | tradingview extra and dev |
| `httpx2` | `httpx2` | `2.16.0` | tradingview extra and dev |
| `jsonschema` | `jsonschema` | `4.26.0` | tradingview extra and dev |
| `msgpack` | `msgpack` | `1.2.4` | transitive through alpaca-py |
| `pydantic` | `pydantic` | `2.14.3` | transitive through alpaca-py/MCP |

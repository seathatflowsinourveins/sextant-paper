# Pinned upstreams

Read-only sources for the paper integration and retained local harness. The table is inherited from the template's verified 2026-11-11 upstream inventory; no clone or install is performed by this adaptation. The official TradingView MCP docs page and its recorded read date are in [landscape](docs/landscape.md). Import PRs extend this table only for their actual dependencies.

Use `bash scripts/clone-upstreams.sh TARGET` when a read-only clone is needed. Each tag is checked against its full commit before use. Do not edit source clones. [SOURCE-HIERARCHY.md](SOURCE-HIERARCHY.md) sets authority; the harness source and package-test source are separately pinned in [manifest](docs/manifest.md).

| Directory | Repository | Tag | Commit | History |
|---|---|---|---|---|
| nautilus_trader | nautechsystems/nautilus_trader | v2.0.0rc8 | d53849b216e20f2c122ca01b4dd0da377638b186 | full |
| alpaca-py | alpacahq/alpaca-py | v0.44.0 | cc4cb3b7ba50ae250e621983c2779047fb16bb28 | full |
| codex | openai/codex | rust-v0.167.0 | 086ccc833bc9cf01024ad2dd6af7dbd421b6bf9a | shallow |
| deepseek-harness | deepseek-ai/deepseek-harness | dsh-v0.2.2-rc.1 | 66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db | shallow |
| just | casey/just | 1.59.0 | 2638e9c0941edb1f409da7426fa7842887656973 | shallow |

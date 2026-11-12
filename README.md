# Sextant paper

The paper loop on NautilusTrader with our Alpaca adapter (Paper B), plus the ranked-list display in TradingView. [Design](docs/design.md) defines the scope; [AGENTS.md](AGENTS.md) defines the hard lines. The initial adaptation contains the harness only; trading source arrives through later measured package imports.

Squash auto-merge requires **ci only**: lint, tests, secret scan and repository size budgets. Package citations are checked by a repo test inside ci. The local PR helper checks upstream references, a manifest row and the author marker before pushing. No review secrets or hosted reviewer are needed.

Start with `just session-start`, then `just ci` and `just prove`. To submit authorized work from a branch, fill the [PR template](.github/pull_request_template.md) and run `just pr "title" body.md`. A real paper start separately needs the exact-SHA Codex max review PASS specified in AGENTS.md.

- [Setup](SETUP.md): settings, pinned Actions allowlist and local tools.
- [Landscape](docs/landscape.md): official upstream integrations and package coverage.
- [Manifest](docs/manifest.md): short harness rows with sources and proof references.
- [Origins](ORIGINS.md): template provenance and adaptation history.
- [Adaptation](ADAPT.md): completed changes and the remaining import-budget handoff.

Licence is not yet chosen; the owner decides. Until a LICENSE file is added, no licence is granted. Dependencies keep their own terms.

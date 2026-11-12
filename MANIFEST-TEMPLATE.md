# The manifest pattern

[docs/manifest.md](docs/manifest.md) holds short harness-practice rows with a pinned source, application and proof reference. [docs/landscape.md](docs/landscape.md) holds one short row per package integration with its official upstream, pin/source location and verdict. Tests discover proof files directly.

The manifest's first cell is a unique key. A PR names it in double quotes under `## Manifest row`; scripts/pr-check.sh checks the committed manifest locally, without a required status or reviewer. Upstream references accept repo@tag plus full SHA, a docs URL and read YYYY-MM-DD, or a DOI/arXiv citation, using the template's inherited body rules.

Source: sextant-harness initial paper commit `cc156d07e6d8fda8263a7c1323c641aae65765ac`, dangerfile.js and tests/repo/test_pr_check.py; the current local port is scripts/pr-check.sh. Package coverage adopts sextant-movers as cited in docs/manifest.md.

## Registry rows for tunable values

The rule (Sextant's ADR-0018, line 22, and AGENTS.md Rule 2): "Every tunable number has one registry row: name, unit, value, source, calibration state and last change. The consuming runtime owns it; reviewed changes occur between sessions. Outputs carry `params_hash`. Missing evidence is labelled starting value (cited), never measured success."

Three states, and only three:

| State | Meaning | Value |
|---|---|---|
| sourced, `UNCALIBRATED` | The value comes from a cited source (an upstream default, a docs page with its read date, a paper), but no calibration record on your own data exists yet. It is a starting value, never measured success. | set |
| calibrated | A calibration record exists, and the row cites it. Sextant changes a row only between sessions and only by a reviewed PR; nightly tuning only proposes. | set |
| `UNAVAILABLE` | No source was found, or the authority is missing. The consumer treats the feature as unavailable; no fixture or default fills the gap. | `null` |

The example file `examples/registry/parameters.example.json` is a real Sextant registry file, copied verbatim: two `UNCALIBRATED` rows, each sourced to a docs page with its read date or to an existing row, and one `UNAVAILABLE` row with `value: null`, whose source says where the authors looked and found nothing. In Sextant's code, the ranking registry (`src/sextant/ranking/registry.py`, `_require_source`, not included here) refuses a row whose source is empty or whose last change is not a date.

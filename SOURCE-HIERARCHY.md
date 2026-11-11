# Source hierarchy

Which source may decide a design, a fix or a review. This sharpens Rule 1 in [AGENTS.md](AGENTS.md) ("never self-invent").

Origin: a lead ruling in the Sextant project, 2026-11-11, recording the owner's direction of that morning: use the official upstream repositories as the source of truth, and give every other repository a clean end-to-end run and an evaluation first. The ruling also corrected the lead's own direction from three minutes earlier, which had called a non-official repository "the upstream" for one code path.

## The rule

1. **The official upstream is the source of truth.** For every design, fix and review, the authority is the official upstream at your pinned commit: the framework's or engine's own repository and documentation (its source, official adapters and official examples), the vendor's own SDK and documentation (with read dates), the published standards and regulators' rules you cite, and each service's own documentation.
2. **Any other repository is a candidate.** That includes your own earlier projects and community repositories. A candidate may be read and reported on, but nothing rests on it alone until it has had a clean end-to-end run and an evaluation, both recorded with its commit.

## How it is applied

1. Line maps, port plans, state tables and review briefs carry the official source as their first column. A candidate repository goes in a second column labelled "candidate, not yet evaluated". A port needs an official source that says the same thing, or a recorded evaluation of the candidate.
2. Where the official upstream already provides a mechanism (in Sextant's case: order status and the in-flight check, reconciliation, command ids, duplicate-fill handling, emulated and contingent orders, state persistence), use it; do not rebuild it. A hand-built mechanism stays only where the line map shows the upstream has none, and it cites the line that shows it.
3. Existing registry rows whose source is a candidate keep their values as UNCALIBRATED starting values: they were registered as sourced starts, not as measured values. No new row may cite a candidate repository alone. (Registry rows: [MANIFEST-TEMPLATE.md](MANIFEST-TEMPLATE.md#registry-rows-for-tunable-values).)
4. Evaluating a candidate means running its own test suite offline in an isolated environment, with no key and no network to any production service, and reporting what passes, how much of the behaviour you would take the suites cover, which versions it pins and its licence.
5. Give every worker read-only copies of the official upstreams at the pins, verified against the pinned commit. [UPSTREAMS.md](UPSTREAMS.md) lists Sextant's, and `scripts/clone-upstreams.sh` reproduces the folder. Candidates are listed and cloned separately, under `candidates/`.

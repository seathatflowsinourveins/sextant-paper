# The manifest pattern

Every pick and every practice in the harness has one row in `docs/manifest.md`: what it is, the pinned source it comes from, how it is applied, and the proof that checks it. Nothing enters without a row. A practice that stops is deleted, and git keeps the history. This page shows the pattern with Sextant's own header and four real rows of different kinds, copied verbatim from Sextant's `docs/manifest.md` (see [ORIGINS.md](ORIGINS.md)). The seed `docs/manifest.md` in this tree holds the gate's own rows.

## The header

# Manifest

What we do and where the practice comes from. Nothing enters the harness without a row. A practice we stop using is deleted; git keeps the history. `just prove` runs every proof whose claim holds in a host checkout; the devcontainer build runs the container proofs and the maintainer runs the GitHub-settings proofs with the admin token. CI publishes the count.

| Practice or pick | Source, pinned | How it is applied | Proof |
|---|---|---|---|

The first cell is the row's key. A PR names it in double quotes under `## Manifest row` (see `.github/pull_request_template.md`), and `dangerfile.js` refuses the PR unless a row with that key exists in the manifest of the commit it checks. The PR may add the row itself.

## Four example rows

**A pinned repository** (owner/repo@tag with the full 40-character commit):

| Practice or pick | Source, pinned | How it is applied | Proof |
|---|---|---|---|
| Secret scan as a required check and the only local hook | [gitleaks/gitleaks](https://github.com/gitleaks/gitleaks)@v8.30.1 `83d9cd684c87d95d656c1458ef04895a7f1cbd8e` | `just secrets`, `.githooks/pre-commit`; WSL `setup.sh` installs the Dockerfile's pinned, checksum-verified release and selects `core.hooksPath .githooks` | `test_foundation.py::test_gitleaks_is_wired` |

**A pinned repository with file paths, plus a docs page with its read date:**

| Practice or pick | Source, pinned | How it is applied | Proof |
|---|---|---|---|
| Danger through the API, with automatic other-family review for gate edits | danger/danger-js@14.1.0 `72540533c4f99bad772ba303be0eb4af6eddbdc4`:`source/ci_source/providers/GitHubActions.ts` constructor/pullRequestID/repoSlug, `source/platforms/github/GitHubUtils.ts` fileContents; reuses main's `.devcontainer/agents/package.json:11`; [GitHub reserved variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#default-environment-variables), read 2026-11-09 | The native GitHubActions provider receives the API-sourced event through a process-scoped GITHUB_EVENT_PATH assignment; workflow-level overrides of reserved variables are ignored. The default-branch dangerfile reads PR metadata, files and manifest through the API. Gate/prompt/dangerfile edits, deletions and renames route to one extra API reviewer, except that a Codex author adds one only when it selects Claude (D-34, 2026-11-10); ordinary source rules remain in force for all PRs | `test_public_gate.py::test_danger_reads_api_data_and_never_checks_out_the_head`, `tests/repo/test_dangerfile.py::test_a_sourced_gate_edit_needs_no_owner_label`, `test_public_gate.py::test_key_policy_and_failure_paths_execute_against_the_actual_adapter` |

**A docs page with its read date:**

| Practice or pick | Source, pinned | How it is applied | Proof |
|---|---|---|---|
| Parallel CI jobs fanned into one required check | [GitHub workflow syntax: `jobs` and `jobs.<job_id>.needs`](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds), read 2026-11-09; [zizmor `self-repository` audit](https://docs.zizmor.sh/audits/#self-repository) at zizmor 1.32.0, read 2026-11-09 | `.github/workflows/ci.yml` runs `lint`, `tests`, `budgets` and `secrets` as independent jobs, then a job named `ci` with `needs: [lint, tests, budgets, secrets]` and `if: always()` fails unless every `toJSON(needs)` result is `success`, keeping the required context name. Its pinned tools come from one shell script, `.github/actions/ci-tools/install.sh`, which `just lint` runs through ShellCheck, so no in-repo action reference exists: the latest actionlint, 1.7.12 of 2026-03-30, rejects GitHub's July-2026 `$/` self-repository form that zizmor 1.32.0 asks for, and a plain script needs neither form | `test_foundation.py::test_ci_fans_out_into_parallel_jobs_and_the_fan_in_fails_closed` |

**A paper (DOI and arXiv) with its read date.** This row is from Sextant's trading research and is kept only to show the form; its paths are Sextant's.

| Practice or pick | Source, pinned | How it is applied | Proof |
|---|---|---|---|
| Bounded SIP L1 quote frontier and receipt-time OFI for arm e | Cont, Kukanov & Stoikov (2014), "The price impact of order book events", Journal of Financial Econometrics 12(1):47-88, https://doi.org/10.1093/jjfinec/nbt003, arXiv:1011.6402v3 (read 2026-11-09); ADR-0003:13, ADR-0024:38; https://docs.alpaca.markets/docs/real-time-stock-pricing-data (read 2026-11-09) | `src/sextant/data/quotes.py` names the frontier from top-N, holdings and catalyst lists only (wildcard refused), selects exactly those instruments for the existing SIP feed, joins each `QuoteTick` on the local `ts_init` receipt rather than the vendor timestamp, sums the paper's Level-I e_n on a ten-second receipt grid, and refuses arm e when an authority list is unavailable or resident bytes reach the 8 GiB one-session command cap. No whole-market subscription and no order authority. | `tests/proofs/test_bounded_quotes.py::test_frontier_names_the_union_and_keeps_every_role` |

## The rules a source must meet

`dangerfile.js` (run by the `source` check from the default branch, and locally by `scripts/pr-check.sh` through `just pr`) accepts the PR body's `## Upstream references` only when it contains one of:

- a repository at a tag with its full commit: `owner/repo@tag` and a 40-hex SHA (a Markdown-linked `[owner/repo](url)@tag` also counts);
- a docs page URL followed by `read YYYY-MM-DD`;
- a paper: a `doi.org/10.` link, `doi:10.`, an `arxiv.org/abs/` link or `arXiv:` and its number.

It also refuses an empty `What changed`, `Manifest row`, `Upstream references`, `How verified` or `How to revert` section, and, on a governed path, a source list that names none of that path's authorities (`AUTHORITY` in `dangerfile.js`; Sextant's surfaces, to be replaced by yours). Dependabot's generated bodies are exempt from the body rules.

## The proofs that check it

- `tests/repo/test_dangerfile.py`: the source forms (`test_a_docs_page_with_a_read_date_is_a_source`, `test_a_paper_is_a_source`, `test_a_markdown_linked_repo_at_a_tag_with_its_full_sha_is_a_source`, `test_no_source_is_refused_as_self_invention`, `test_a_url_without_a_read_date_is_refused`, `test_a_commit_shorter_than_full_is_refused`), the row rules (`test_a_manifest_row_that_does_not_exist_is_refused`, `test_the_table_header_is_not_a_row`, `test_a_quoted_word_from_another_cell_is_not_a_row`, `test_a_manifest_row_must_be_named_in_quotes`), the sections (`test_an_empty_section_is_refused`, `test_a_template_comment_is_not_content`) and the governed paths (`test_a_governed_path_must_name_its_authority`, `test_a_lookalike_owner_does_not_pass_for_its_prefix`).
- `tests/repo/test_pr_check.py`: the same rules run locally before a push (`test_the_local_manifest_is_read_from_head`, `test_an_empty_body_file_still_fails_every_section`, `test_the_script_refuses_a_review_window_over_the_limit`) and the row for that check (`test_the_manifest_row_names_the_local_check_and_its_proofs`).
- `tests/proofs/test_manifest_union.py`: the manifest merges with git's built-in `union` driver (`.gitattributes`), so parallel PRs that each append a row do not conflict (`test_the_manifest_merges_by_union`, `test_union_keeps_both_sides_rows`); no row key repeats (`test_no_row_key_is_duplicated`); and no retired row comes back (`test_no_retired_row_returns`). Its `KNOWN_REPEATS` and `RETIRED` lists and `test_manifest_records_every_adr_0021_display_pin` are Sextant's; empty the lists and delete that test when you adopt it.

## Registry rows for tunable values

The rule (Sextant's ADR-0018, line 22, and AGENTS.md Rule 2): "Every tunable number has one registry row: name, unit, value, source, calibration state and last change. The consuming runtime owns it; reviewed changes occur between sessions. Outputs carry `params_hash`. Missing evidence is labelled starting value (cited), never measured success."

Three states, and only three:

| State | Meaning | Value |
|---|---|---|
| sourced, `UNCALIBRATED` | The value comes from a cited source (an upstream default, a docs page with its read date, a paper), but no calibration record on your own data exists yet. It is a starting value, never measured success. | set |
| calibrated | A calibration record exists, and the row cites it. Sextant changes a row only between sessions and only by a reviewed PR; nightly tuning only proposes. | set |
| `UNAVAILABLE` | No source was found, or the authority is missing. The consumer treats the feature as unavailable; no fixture or default fills the gap. | `null` |

The example file `examples/registry/parameters.example.json` is a real Sextant registry file, copied verbatim: two `UNCALIBRATED` rows, each sourced to a docs page with its read date or to an existing row, and one `UNAVAILABLE` row with `value: null`, whose source says where the authors looked and found nothing. In Sextant's code, the ranking registry (`src/sextant/ranking/registry.py`, `_require_source`, not included here) refuses a row whose source is empty or whose last change is not a date.

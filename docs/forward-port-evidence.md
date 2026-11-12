# Forward-port evidence

Source baseline: Sextant `bbbe7f5e51159c346b28117bff93361439d2e5c9`. Destination baseline: paper `878b6388f8e084c584263045683f6a200104ddac` (C/E/D imports landed). The approved 85-file source closure is unchanged through the two carried source merges.

Roster-ops ruling 2026-11-11, accepted by the lead: cut/held files stay cut/held. Carry B hunks onto recorded cut files; record the upstream `git diff PARENT MERGE -- PATH` SHA-256 and destination digest. Extract independent B-unit proof nodes into a new file; keep the mixed upstream unit proof held whole. No difference is silently called identical. Locally worded provenance/evidence documents are repository documentation, not imported source files.

## Sextant #702

Source commit: `671509973b798912cf0aa8cfa37c80056866f700`.

| Path | Identity kind | Source SHA-256 or hunk SHA-256 | Destination SHA-256 | Result / recorded cut |
|---|---|---|---|---|
| `tests/proofs/custody/test_b_attended_entry.py` | byte-identical | `06411ae06357835dc4597b1eca84c28cba9b190cc53574637f2f1b1437949ab1` | `06411ae06357835dc4597b1eca84c28cba9b190cc53574637f2f1b1437949ab1` | yes |
| `tests/proofs/custody/test_b_attended_stop.py` | byte-identical | `bf7c3724df937392493c5b3a7a08b1e195f631d3c261ffae085e563c570a6c54` | `bf7c3724df937392493c5b3a7a08b1e195f631d3c261ffae085e563c570a6c54` | yes |
| `docs/handbook/practices.md` | same-hunk-on-a-recorded-cut-file | diff `1cef4f41b838fbf840af1ab708dcd5309d47a54436f3e110654475eab7692941` | `e360416bfb70bb2c44a7267a504475a43b7899557b7ae1846679abc3a408962f` | B sentence carried onto paper #2 handbook at `878b6388f8e084c584263045683f6a200104ddac`; unrelated trading practices remain cut |

The source plan `docs/superpowers/plans/2026-11-11-b-attended-entry.md` is not an imported runtime/proof/deploy file and is not carried. This tests-only merge does not change runtime behavior. The independent import acceptance failed on both proof identities before copying and passed afterward.

## Sextant #704

Source commit: `33cdee7b65d53c93ab106b7b3196fc790b14a5a8` (last carried Sextant SHA).

| Path | Identity kind | Source SHA-256 or hunk SHA-256 | Destination SHA-256 | Result / recorded cut |
|---|---|---|---|---|
| `deploy/paper/paper-b-test-entry.params.example` | byte-identical | `0ae90e1c0b2e6df0961d57e7414a5f3924bad3a50303b2345320c51a8e36ddd6` | `0ae90e1c0b2e6df0961d57e7414a5f3924bad3a50303b2345320c51a8e36ddd6` | yes |
| `deploy/paper/sextant-paper-b-test-entry.service` | byte-identical | `9ac2c1253a252932068c644af21b996d94c27c9aee9cc9c91c6761e13fd5e2f5` | `9ac2c1253a252932068c644af21b996d94c27c9aee9cc9c91c6761e13fd5e2f5` | yes |
| `deploy/paper/README.md` | same-hunk-on-a-recorded-cut-file | diff `b631ebd0bb7c2d0de3a2f420ea7f3a83ab524c621c8abbda8a2e4be5495a70ef` | `a3cc57864de62d6127e3f5c86ad83dfed2db714987daa9171704fbfe6b71a1b2` | B hunks over E cut from paper #4, preserved in `878b6388f8e084c584263045683f6a200104ddac`; no IBKR restoration |
| `deploy/paper/install.sh` | same-hunk-on-a-recorded-cut-file | diff `ea1fe05d0a65b1f0293e4c258b0972bce14fd06a39c7033d0eaa996e76c0bdbe` | `1b7f0e6cc8a1634c7704452f3fe620e7df3e7cf11611ca2f1041d92c779292e5` | B hunks over E cut from paper #4, preserved in `878b6388f8e084c584263045683f6a200104ddac`; no IBKR restoration |
| `tests/proofs/test_b_paper_units.py` | new-file-exception | parent mixed proof `8d052dd54f15d5dd5f9e2ced8e54a2888191b47b119200abeae5c9a0817ee436` | `582c607afea7393d3a56b2964b8a14cf97928b33e0043eadb76b98de0e1de8c8` | Function bodies/decorators copied unchanged; B-only imports/constants replace mixed module setup; exact extracted ranges below |

`git apply --3way` reports the expected overlaps in both cut files. Resolve by retaining E's exclusions: the installer iterates over the two B services only; README carries the two-unit introduction, B test-entry section and its B source references. The rest of E's cut content remains. The upstream single-file diffs above are hashed verbatim, including context, as `git diff SOURCE^ SOURCE -- PATH`; these are source-hunk identities, not full-file equality claims.

### Extracted proof ranges at #704

| Function | Upstream lines |
|---|---|
| `_unit` | 30–34 |
| `_install` | 86–111 |
| `test_installer_only_renders_and_reloads_never_starts_or_enables` | 114–130 |
| `test_installer_refuses_paths_systemd_would_split_or_expand` | 133–139 |
| `_arguments` | 142–146 |
| `test_paper_installer_is_shellcheck_clean` | 213–220 |
| `test_b_test_entry_unit_only_adds_the_fixed_instrument_opt_in` | 280–299 |
| `test_b_test_entry_parameters_are_separate_complete_and_blank` | 302–317 |
| `test_b_test_entry_instrument_is_one_literal_argument_even_when_missing` | 320–325 |
| `test_paper_installer_has_no_start_or_enable_command` | 328–330 |

All ten function/decorator blocks match the upstream text byte-for-byte. The four added B-unit checks plus the existing offline installer and shellcheck checks run with only the two B units in module setup. Renderer checks invoke a fake manager in temporary directories; no real unit is installed, reloaded, enabled or started.

### Held / excluded paths and nodes

| Source path or nodes | Disposition / reason |
|---|---|
| `tests/proofs/test_paper_units.py` | HELD whole, as in E. Mixed setup requires excluded IBKR units, examples and parser. Never copied here. |
| `test_ibkr_command_parses_as_lifecycle_dry_run_without_reading_account`, `test_ibkr_lifecycle_unit_only_adds_the_fixed_second_opt_in`, `test_ibkr_lifecycle_command_uses_node_opt_in_without_reading_account` | HELD: require excluded IBKR module/assets. |
| `test_attended_units_never_restart_or_enable_and_inject_only_named_keys` | HELD unchanged: every parameter also reads the excluded IBKR dry-run asset. The B unit-delta check preserves the existing B unit contract. |
| `docs/manifest.md` (Sextant) | Excluded source manifest; the matching deployment practice is written locally in this repository's manifest instead. |
| IBKR README sections, installer service names | Retain E's cuts. |

RED before deployment: `11 failed, 2 passed in 1.44s`, including missing separate attended test-entry unit and missing parameter example. The failing proof is committed separately before the deployment fix. No source file or cap changes occur.

Targeted GREEN after deployment: **13 passed in 1.25s**. Start-path comparison at the last carried source SHA covers all 85 Python files, both B units, the paper installer and all three key wrapper scripts: **90/91 byte-identical**; the sole non-identical start-path file is `deploy/paper/install.sh`, the recorded E-cut hunk carry. The lead's new exact-SHA Codex max start review must cover that installer. This attests scope and identity, not permission to start.

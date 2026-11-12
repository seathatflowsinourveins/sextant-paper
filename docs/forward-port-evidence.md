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

# Paper loop lessons

Lessons from the attended runs and lead rulings of 2026-11-11. Keep each lesson short, with its evidence; record the resulting practice in [practices](practices.md).

## 1. Run before adding review depth

After the required start gates pass, make the first attended run before further review rounds; build what the next run needs.

Evidence: LEAD-1836, measured findings and R-A/R-C, 2026-11-11: attended starts found defects that repeated reviews against fixtures had missed. Required gates remain in force.

## 2. A guard records why it stops

Record the stop class as class data only, so the next decision follows a cause rather than a guess. Keep response bodies, headers and sensitive content out of the record.

Evidence: LEAD-1904 and addendum 3, 2026-11-11: the initial stop had no recorded cause; the diagnostic record identified a JSON parsing exception on an accepted empty response.

## 3. Follow the SDK's response handling

When the pinned SDK accepts a response without reading its body, the guard must not parse that body.

Evidence: modelcontextprotocol/python-sdk v2.3.0 at `2118f14f8a19bc158d8a1cf90af58d85d187f849`, [src/mcp/client/streamable_http.py:377-388](https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/src/mcp/client/streamable_http.py#L377-L388): the 202 branch returns without reading the body. LEAD-1904 addendum 3, 2026-11-11, traced the false stop to parsing that empty body.

## 4. A denied action goes back to the owner

Stop and return blocked work to the owner; never retry it through another session or edit deny rules to pass it.

Evidence: the lead's 2026-11-11 state record at 12:17–12:24 withdrew a request for a peer to perform a denied action; the peer refused. The 12:57 ruling cites Claude Code 2.1.322's SendMessage permission boundary and [permission rule precedence](https://code.claude.com/docs/en/permissions), read 2026-11-11.

## 5. After Enter, keep the old writer's module out of command text

After sending Enter to the old writer's tab, run no command whose text names its module; check the lease by its lock name only.

Evidence: the lead's 2026-11-11 state record at 17:59–18:00: a wait command containing the module name matched the single-writer guard and refused the restart. The corrected check used the lock name.

## 6. A restart repeats admission

Supervision retries the same command; it never inherits admission from the previous
process. Prove consecutive starts against different incumbent leases using the real
wrapper/CLI and fixture credentials. Guard exit 2 stays stopped; a non-flat restart
refuses, so automatic restart does not establish continuous protection.

Evidence: Sextant@`9f3464456da25945a0e4a4802dc119132692f57f`, the reviewed stage 2
unit and `test_stage2_repeated_wrapper_starts_recheck_both_incumbent_leases`, ported
to `tests/proofs/test_b_paper_units.py`; [systemd.service(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
`Restart=`/`RestartPreventExitStatus=`, read 2026-11-11. The
[operator instructions](../../deploy/paper/README.md#unattended-stage-2-protection)
retain the admission boundary.

## 7. A rejected print stops its entry path

Honor the entry state's rejection before downstream range/surge processing or
entry submission. A previously ready run can survive a duplicate; later ranking
availability does not make that duplicate an accepted trigger.

Evidence: LEAD-2218 addendum 1 and R3's native offline witness, retained in
`test_b_lifecycle_entry_duplicate.py`; `EntryState.on_trade` retains its boolean
contract. NautilusTrader `d53849b216e20f2c122ca01b4dd0da377638b186`,
`crates/trading/src/python/strategy.rs:1214-1217`, dispatches the native callback.

## 8. Subscription opt-out preserves other consumers

Skip a duplicate bar subscription only when the other consumer actually subscribed.
Turning off entry auto-subscription must retain the independent protection-width
subscription even when both consumers use the same bar type.

Evidence: the native `test_disabling_entry_subscription_preserves_shared_protection_bars`
failed with the original flag and passed after restoring the width subscription;
lead ruling 2026-11-11 22:51. NautilusTrader
`d53849b216e20f2c122ca01b4dd0da377638b186`,
`crates/common/src/actor/data_actor.rs:1938-1964`, routes bars through subscriptions.

## 9. Admission walks both sides of reconciliation

A terminal predecessor does not end its replacement lineage. Retain the venue links
that native reports omit, then walk cached and withheld rows together and count a
shared working endpoint once. A resolved terminal lineage admits; a missing or
cyclic terminal link refuses. Admission evidence never grants owner-order mutation
authority.

Evidence: LEAD-2218 section 1, 2026-11-11; R2's two retained native-node witnesses in
`tests/proofs/custody/test_r2_review_witnesses.py` failed before this fix. Native cache
and report shapes: NautilusTrader `d53849b216e20f2c122ca01b4dd0da377638b186`,
`crates/common/src/python/cache.rs:756-800`,
`python/nautilus_trader/model/__init__.pyi:4862-4901`; venue links and replacement
client-id semantics: alpaca-py `cc4cb3b7ba50ae250e621983c2779047fb16bb28`,
`alpaca/trading/models.py:223-224`, `requests.py:300`.

## 10. Prove normal loading through composition

A composition proof that injects its dependencies cannot detect a missing loader.
Load entry-buffer exit rows and the representable sizing fee rate during normal row
admission; keep basis-path admission explicit. Validate each dependency independently:
missing authority refuses ordinary entries without becoming a new startup gate for
protection or the separately attended test intent. Loading buffer inputs never enables
gain-lock or exit chase.

Decimal conversion and cent-rounding can raise `decimal.InvalidOperation` rather
than `ValueError`. Optional dependency admission must catch that exact validator
exception too, leaving ordinary entries refused while startup remains admitted.

Evidence: LEAD-2259, 2026-11-11; the two G3 normal-loader assertions failed before
the fix. `custody/test_b_node_entries.py` now carries those inputs from the loader
through composition to the native entry replay, plus unavailable-row startup proofs.
Build on the existing validators at sextant-paper
`438c90a409620132c38a6a864f0a53cfb315ddc9`,
`src/sextant/paper/b_entry_dependencies.py:57-84`.
The G3 exact-head review at `52fb96c2f2aa1285d4ffc7f5bac196230f31c497`
reproduced this trap for malformed exit rows; fix-round witnesses also cover the
sizing-fee rounding trap and both native startup modes.

# The batch runner: design, requirements and implementation

`scripts/batch` builds, flashes, benches, sets up and QAs batches of
council-approved changes on the river test phone, and merges a clean batch
through one batch PR per repo. This file is the binding design (revision 6,
approved by Opus and Gemini at council round 6), the implementation
requirements I1-I21 that override it where they differ, and a map from each
rule to the code and its tests. Usage for agents is in `AGENTS.md` and
`workspace/AGENTS.md`; the harness it drives is in `README.md` and
`DESIGN.md`.

## Using it

```sh
S=.repo/manifests/scripts
$S/batch status                       # the queue, the running batch, what waits on Zak
$S/batch enqueue --record record.json # after the council approved the exact heads
$S/batch tick                         # the tester thread, every 2 hours
$S/batch requeue NAME --reason "..."  # tester: a failed entry was the desk's fault
$S/batch tried NAME --reason "..."    # tester: it was the change's fault
```

Zak-only (`--zak`): `batch resume`, `batch settle ID`, `batch cleanup drop
ID`, `batch requeue NAME --zak`. A person qualifies the runner with `batch
qualify` and builds calibration and baseline candidates with `batch stage`.

### Approval record format (I14)

A council record is `record.json` (R5.2) with its briefs and replies next to
it, named by **basename** only. Every brief and every reply carries the
round's **scope block** (I14, Sol B3):

```text
Scope: full
Head: zrm625/android_device_motorola_river#6 <40-hex head>
Head: zrm625/android_vendor_lineage#5 <40-hex head>
```

or, for a delta round, one line per PR the round names:

```text
Scope: delta
Delta: zrm625/android_device_motorola_river#6 <40-hex from> -> <40-hex to>
```

The `Head:` or `Delta:` lines must bind exactly the round's PRs to exactly
its heads: every PR once, with its own head (or its own `from` and `to`, in
that order), and no other PR. A SHA that merely appears in the text binds
nothing, so a reversed delta, swapped PRs or an extra PR is refused. Every
reply also carries a line `Verdict: APPROVE <score>` equal to the provider's
recorded verdict.
`batch enqueue` copies the record and its files into the immutable bundle
`$BENCH_HOME/runner/council/<name>/<sha256 of record.json>/` before the
state transaction. The briefs and replies of the 2026-11-23 GNSS and Perch
rounds do not have these lines or full heads, so `batch migrate --record
FILE...` needs new council rounds for them (I14 accepts this).

## Module map

| Module | What |
| --- | --- |
| `scripts/batch`, `riverbatch/cli.py` | Subcommands, exit codes (R2.3, R3.1), the last-line JSON, runner errors (R11.5) |
| `riverbatch/state.py` | The state file and its transactions (R4), entries, failure history by identity (I13), the waiting-on-Zak list |
| `riverbatch/records.py` | Approval records, evidence binding (R5.2, I14), immutable bundles (R5.1) |
| `riverbatch/github.py` | Every GitHub and remote git call, typed (R11.6, H11, I21 GraphQL) |
| `riverbatch/eligibility.py` | R5.3, R5.4 (camera sets, manifest PRs with renames, I21) |
| `riverbatch/commands.py` | enqueue, withdraw, requeue, tried, resume, cleanup drop, status, migrate (R5.5-R5.7, R9.4) |
| `riverbatch/kb.py` | KB without root (I1, I2) and checked phone reads |
| `riverbatch/compose.py` | Member selection (R7.1), composition (R7.3), content key |
| `riverbatch/tc.py` | The whole-tree check (R7.4) |
| `riverbatch/phases.py` | Member checks and claim, checkout, build, stage, restore, flash, bench, setup, QA (R7.2-R7.8, R8.3-R8.6) |
| `riverbatch/outcomes.py` | Build classes, flash adoption and rescue evidence (R8.6, I3), binding (R8.5), desk suspect (R9.5), applying outcomes (R9) |
| `riverbatch/merge.py` | Batch PRs, CS, reconciliation (R10.4a, I4, I5), tags, promotion, KW (I7), change PRs, branch deletion (R10) |
| `riverbatch/recover.py` | R11.2, the recovery entry point (R13.2, I15, I18), the paused pass (I19), cleanup items (R13.3, I12), finish (R10.8), retention (R15.2, I17) |
| `riverbatch/tick.py` | The tick (R8.1), preflight (R8.2), one batch (R8.3), the merge path to `merge_started` (R10.2, R10.3) |
| `riverbatch/settle.py` | `batch settle` (R13.4, I5, I9, I11) |
| `riverbatch/qualify.py`, `abtest.py` | `batch stage` (R14.1, I6, I10), `batch qualify` (R14.3, R14.4, I20), part 1 A->B->A (R14.2, I8) |
| `riverbatch/report.py` | HTML reports (R15.1) |
| `riverbatch/hold.py`, `ports.py`, `ctx.py` | The holder (R6.2), tool and git ports, locks (R6.1, R6.3), control flow |
| `riverbatch/tooling.py` | `change merge NAME --record FILE` checks (H4, R5.8) |
| `riverbatch/version.py` | `runner_build` and `runner_tools` (R2.2) |
| `riverbatch/*.json`, `manifest_stub.xml` | TC allowlist, the effective-manifest stub, part 1's config, masks and allowlist (all `runner_build` inputs) |
| `riverqa/children.py`, `riverqa/runnerstate.py` | Live child records (H15); the shared, fail-closed reader of the state file (H4, H12) |

Tests: `qa/tests/test_batch_*.py` (runner) and `qa/tests/test_runner_harness.py`
(harness changes), with `qa/tests/batchfakes.py` (its TC fakes keep the real
`tc.check` contract: a fork missing from `expected` fails) and
`qa/tests/ghworld.py` (one recorded GitHub for the harness lookup). They use a throwaway
`$BENCH_HOME` and never touch a phone, GitHub or the real tree.

## How each implementation requirement is met

| Req | Code | Tests |
| --- | --- | --- |
| I1 | `kb.check`, `kb.READ_CMD`: `ro.river.build_id` plus `ro.vendor.build.date.utc` against `batches/<known-good batch>/batch.json` `dates.vendor`; never `ro.vendor.river.build_id` or `/vendor/build.prop`. Live check on ZY225DPT7F with non-root adbd (2026-11-23): `getprop ro.vendor.river.build_id` is empty and `/vendor/build.prop` is denied, also from `/vendor/bin/sh` (vendor_shell); `ro.river.build_id` and `ro.vendor.build.date.utc` read correctly | `test_batch_kb.KB.test_holds_with_one_non_elevating_read`, `test_vendor_matched_by_staged_vendor_date`, `test_missing_staged_date_blocks` |
| I2 | `kb.read_props`: one `sh_rc`, no `guard()`/`root()`; HALT only on rc 0 with a non-empty mismatched product or serial; a failed read or an empty product, serial or slot re-checks USB (pause or blocked); an empty or `0` `sys.boot_completed` and empty stamps also re-check USB, then are "phone still booting" or "unstamped" (blocked) | `test_batch_kb.KB.test_halt_only_on_rc0_identity_mismatch`, `test_empty_values_recheck_state`, `test_empty_boot_completed_is_booting_not_a_pause`, `test_empty_stamps_recheck_the_usb_state`, `test_usb_states` (FakeDev fails the test on `root()` or `guard()`) |
| I3 | `flashing.flash_batch` sends the bootloader-fallback path through `rescue()`; `outcomes.rescue_evidence`, `outcomes.adopt_flash`, `phases._bench` | `test_runner_harness.RescueAttemptIDs.*`, `test_batch_outcomes.Rescue.*`, `test_batch_flow.Crash.test_rescue_evidence_first` |
| I4 | `merge.classify` (`lag` is never landed), `merge.reconcile_one`; `slots.landed_but_open`: `flash --promote` and `flash --recover` exit 75 for a PR still OPEN while its merge is river-22.2's tip, before or after the tag, and only if that lag is the sole gap; `merge_all` sends any non-OPEN PR through R10.4a | `test_batch_merge.Classify.test_i4_open_after_landed_tip_is_not_landed`, `Reconcile.test_i4_lag_is_unknown_even_when_reads_agree`, `MergeAll.test_unexpected_merged_goes_through_reconciliation`, `MergeAll.test_merged_after_the_pending_read_is_reconciled_not_halted`, `test_runner_harness.PromoteTyped.test_pr_still_open_with_its_merge_tagged_is_75`, `test_open_at_landed_tip_before_any_tag_is_75`, `test_lag_next_to_another_mismatch_halts` |
| I5 | `merge.reconcile_one` (agreeing non-fitting observations: mismatch; disagreeing or failed reads: unknown); `merge.unknown_started` (failed reads too, from the state file); `settle.preconditions` (unknown > 24 h) | `test_batch_merge.Reconcile.test_i5_*`, `ReconcileClock.test_a_failed_read_starts_the_i5_clock`, `test_batch_recover.Settle.test_i5_preconditions` |
| I6 | `qualify.stage_cmd` ends with the `staged` transaction clearing `running` and `lease`; `recover.owns_pair`: a pair is the runner's only for a batch, experiment or qualify run at its own attempt, so a pair with a stage run's ID is foreign | `test_batch_qualify.StageFinish.test_running_and_lease_cleared_after_staging`, `test_batch_restore.StageRunOwnership.*` |
| I7 | `merge.kw_commit_set` (running.merge, then batch.json `promotion`, then GitHub), `merge.write_known_good` | `test_batch_merge.KW.*` (incl. `test_i7_transport_at_the_known_good_write_is_75_then_written`) |
| I8 | `abtest.normalize_build_manifest`, `abtest.producer_excluded` (the producer's `grep -Ev proprietary_`), `abtest.hash_tree` (missing XML fails), `abtest.measure` (filtered forks checked in the trial checkout), `abtest.compare` | `test_batch_qualify.ABTest.*`, `BuildManifestProducer.*` |
| I9 | `settle.drift`, `settle.settle` (re-baseline owed on landed content or drift; a partial merge's open PRs held for Zak's repair); the settle advice names both repairs | `test_batch_recover.Settle.test_i9_drift_owes_a_rebaseline_even_if_nothing_landed`, `test_not_promoted_landed_owes_a_rebaseline`, `test_batch_settle.PartialSettle.*` |
| I10 | `phases.stage_run(kg=None)` for calibration and baseline; the requalification sequence below | `test_batch_qualify.StageFinish.test_camera_backstop_for_production_only` |
| I11 | `settle.settle` writes `known-good.json` from the local proof before any GitHub call; `flash --promote` writes the proven merge commits into batch.json `promotion` before slot-state | `test_batch_recover.Settle.test_i11_known_good_written_before_any_github_call`, `test_i11_promotion_record_is_the_local_proof`, `test_runner_harness.PromoteProof.test_clean_merge_promotes_and_records_the_merge_commits` |
| I12 | `recover.cleanup_items`: PRs by exact head branch in every touched repo; repo, branch, base and the item's `tested` head (every touched repo, Sol M5) before closing; `expected_merged` and held items (Sol M4) | `test_batch_merge.Cleanup.*`, `test_batch_settle.PartialSettle.*` |
| I13 | `state.failures`, `state.alias_conflicts`, `commands._admissible`, `commands._migrate_admission` | `test_batch_outcomes.Transactions.test_failure_history_follows_identity_and_name`, `test_batch_commands.Enqueue.test_i13_alias_refused`, `test_third_failure_needs_zak_across_withdraw_and_reenqueue`, `Migrate.*` |
| I14 | `records.validate`, `records.check_reply`, `records.check_binding` (the scope block) | `test_batch_records.Validate.test_i14_*`, `test_b3_*`, `test_delta_reply_binds_from_and_to` |
| I15 | `tick.tick` passes `local_only` to the end-of-tick pass after an unknown; `recover.forward(local_only)` stops after KW; no cleanup items | `test_batch_recover.LocalOnly.*` |
| I16 | `qa.cmd_stage` with `lock.BuildLock`, `qa.stage_refusal` | `test_runner_harness.StageLock.*` |
| I17 | `tick.preflight` runs `recover.retention` before the free-space check, under the tick's locks; a failed change's newest evidence by time is kept | `test_batch_recover.Retention.*` |
| I18 | `recover.entry_point` step 5: fastboot without an fb_mode obligation is blocked | `test_batch_recover.EntryPoint.test_i18_*` |
| I19 | `recover.paused_pass`, `recover.strike(quiet_pause)`, `tick.commit_stop` (never replaces a pause; a blocker is persisted with a report), `tick.tick` flushes reports when paused or blocked | `test_batch_recover.PausedPass.*`, `BlockedTick.*` |
| I20 | `qualify.refusals`; `qualify.qualify_cmd` checks the tree (`local_base`) before the entry point's phone recovery | `test_batch_qualify.Refusals.*`, `TreeBeforePhone.test_qualify_and_stage_refuse_a_dirty_tree_before_the_phone` |
| I21 | R2.4/R2.3 split (below); R14.1 runs only R13.2 steps 2 and 3 (`qualify.stage_cmd`); qualify's record transaction (`qualify.qualify_cmd`, event `qualified`); the solo-B pause (R9.5, `outcomes.desk_suspect`); settle advice (m8); `state.add_report` is idempotent (m9); basenames (`records._basename`); H4 fetches first (`tooling.check`); `previous_filename` (`eligibility.manifest_outcome`); GraphQL classes, explicit refusals included (`recovery.classify`); `bench.run` (H17); full SHAs (I14); §19 E3 row | `test_batch_outcomes.Transactions.test_reports_pending_is_idempotent`, `test_batch_records.Validate.test_sha_mismatch_and_basenames`, `test_batch_commands.Eligibility.test_manifest_prs_i21_renames`, `test_runner_harness.TypedLookup.test_classes`, `test_batch_github.GraphQLRefusals.*`, `test_batch_tools.Tooling.*` |

### Code review round 1: findings, code and tests

Each row names the finding (Sol S-, Gemini G-, Opus O-), the code that
closes it and the tests that fail when the fix is reverted (the mutation
check is in the round-1 report).

| Finding | Code | Tests |
| --- | --- | --- |
| Sol B1 | `abtest.producer_excluded`, `normalize_build_manifest(excluded)`, `hash_tree` (missing XML), `measure(checkout_heads)`, `one_build` reads the filtered forks' HEADs | `test_batch_qualify.BuildManifestProducer.*` |
| Sol B2 | `recovery.lookup` (tips, tip commits, `isCrossRepository`), `slots.commit_set_proof`, `slots.landed_but_open`, `flashing.promote_batch`, `flashing.recover`, `flashing.merge_started` (freezes `base`), `merge.record_prs` | `test_runner_harness.PromoteProof.*`, `PromoteTyped.*`, `test_promote.MergeStartedCommand.test_base_is_frozen_with_the_pr_set` |
| Sol B3 | `records.scope_block`, `records.check_binding` | `test_batch_records.Validate.test_b3_*` |
| Sol B4 | `github.GitHub.merge_pr` (REST merge with `sha`) | `test_batch_github.SynchronousMerge.*` |
| Sol M1, Gemini GB1, Opus OM2 | `tick.run_batch` catches an `Abort` from `merge_path` | `test_batch_flow.MergePathAbort.*` |
| Sol M2, Gemini GM3 | `merge.merge_all` step 3, `merge.reconcile`, `merge.unknown_started` | `test_batch_merge.MergeAll.test_merged_after_the_pending_read_is_reconciled_not_halted`, `ReconcileClock.*` |
| Sol M3 | `recover.forward`: the pair before the first tag | `test_batch_mergepath.MergePath.test_pair_lost_after_the_final_merge_halts_before_any_tag` |
| Sol M4, Gemini GM6, Opus OM3 | `settle.settle` (hold, `expected_merged`, a camera mismatch is waiting-zak), `recover.cleanup_items`, `recover.finish_merged` | `test_batch_settle.PartialSettle.*`, `test_batch_merge.Cleanup.test_expected_merged_batch_pr_is_not_a_pause` |
| Sol M5, Gemini GM4 | `state.cleanup_item` (`tested` for every touched repo), `recover.cleanup_items` | `test_batch_merge.Cleanup.test_pr_only_item_still_checks_the_tested_head`, `test_no_tested_head_never_closes` |
| Sol M6 | `merge.change_prs` (close intent in `running.closing`) | `test_batch_merge.ChangePRs.test_interrupted_close_is_checked_again`, `test_closed_at_the_approved_head_clears_the_intent` |
| Sol M7, Gemini GM5 | `merge.promote` (`step="lookup"`), `commands.resume` (every strike) | `test_batch_mergepath.MergePath.test_lookup_pause_has_its_strike_key_and_resume_clears_it`, `test_batch_commands.Enqueue.test_resume` |
| Sol M8, Gemini GB3, Opus OB1 | `abtest.one_build` (abort through `recover.abort`), `abtest.run` (no cleanup on interrupt), `ports.Tools.run`/`stop`, `cli.main` (SIGINT) | `test_batch_qualify.OneBuild.*`, `test_batch_deadlines.Interrupt.*` |
| Sol M9 | `ctx.TOOL_TIMEOUTS`, `ctx.Ctx.tool`, `ports.Tools.run` (timeout), `phases.setup_candidate`, `scripts/phone-setup` (`ADB_CALL_TIMEOUT`) | `test_batch_deadlines.Deadline.*` |
| Sol M10 | `scripts/build` (lock, then commit set) | `test_build_script.LockBeforeProvenance.*` |
| Sol M11, Gemini GM1 | `github.GitHub.pages` (`--paginate --slurp`) | `test_batch_github.Pagination.*` |
| Sol M12 | `tick.commit_stop` (`blocker`), `report.render`, `commands.status` | `test_batch_recover.BlockedTick.*` |
| Sol M13 | `commands.migrate`, `commands._migrate_admission` | `test_batch_commands.Migrate.*` |
| Gemini GB2, Opus OB1 | `phases.new_lease` (`restore_to`), `phases.restore` | `test_batch_restore.*` (with `batchfakes.FakeTree`/`tc_contract`, which fail a fork missing from `expected` as `tc.check` does) |
| Gemini GM2, Opus OM4 | `kb.read_props`, `kb.check` | `test_batch_kb.KB.test_empty_boot_completed_is_booting_not_a_pause` |
| Gemini GM7 | `tc.status_problems` | `test_batch_tools.TreeCheck.test_allowlist_covers_untracked_and_ignored_only` |
| Gemini GM8, Opus OM1 | `phases.test` (an experiment's clean run), `recover.abort` (pass to void only for a batch) | `test_batch_flow.RealQA.*` |
| Minors | `phases.gate_reasons`; `abtest.mask_prop` (full match); `recover.abort` (own cause names the pause); `qualify.run_part2` (QA result); `cli` (last_error only on 0, RecordError a refusal); `merge.reconcile_one` (lag that clears); `phases.checkout` (`git lfs pull` through `remote_git`); `tc.lfs_problems`; `records.files_of`, `store_bundle`, `load_bundle`; `commands.enqueue` (upstream flag); `scripts/change` (`--record` FILE); `qualify.stage_cmd`, `paths.batch_dir` (IDs); `merge.pr_body`; `state.add_failure` (`record_sha256`, `ref`) | `test_batch_flow.RealQA.test_gate_reasons_are_whole_reasons`, `test_batch_qualify.BuildManifestProducer.test_mask_is_a_full_match`, `test_batch_flow.Flow.test_auth_names_the_pause_over_the_transport_strike`, `test_batch_qualify.Part2.*`, `test_batch_deadlines.LastError.*`, `test_batch_commands.CLI.test_a_bad_record_is_a_refusal_not_a_runner_error`, `test_batch_merge.ReconcileClock.test_lag_that_clears_on_the_second_read_is_landed`, `test_batch_flow.Checkout.test_lfs_pull_without_network_is_a_transport_abort`, `test_batch_tools.TreeCheck.test_lfs_pointer_is_the_dash_token`, `test_batch_records.Bundles.*`, `test_batch_commands.Enqueue.test_replacement_refreshes_the_upstream_flag`, `test_batch_tools.Tooling.test_change_merge_record_needs_a_file`, `test_batch_tools.Tooling.test_fetches_before_it_reads`, `test_batch_qualify.Refusals.test_stage_id_rules`, `test_batch_mergepath.MergePath.test_clean_batch_merges_tags_promotes_and_finishes`, `test_batch_flow.Flow.test_single_fail_rolls_back_and_records`, `test_batch_merge.Cleanup.test_third_same_refusal_pauses_and_stops` |

### Requalification after a camera re-baseline (I10)

1. `batch stage --kind baseline --id <new>` (a changed camera set is allowed).
2. Flash, 3-boot bench and `phone-qa --baseline` (README step 3).
3. Harness requalification, README steps 4 to 8; step 8's hand calibration
   pair ends with a verified `flash --rollback`.
4. `batch qualify --run-only` (runner part 2, zero members).
5. `batch resume --zak`.

### Partial first merge (I9)

When a settle leaves part of a batch landed (for example, river's Perch PR
merged and vendor/lineage's not), the authorized repair, before any
qualification, is one of: Zak completes the missing merges of the same batch
at the exact tested heads (`gh pr merge --merge --match-head-commit
<tested head>`, never `--auto`), or reverts the landed ones through reviewed PRs. Then the
re-baseline sequence. A re-baseline is also owed whenever a fork's
`river-22.2` tree has drifted from `known-good.json`, even if none of the
batch's heads landed.

### Implementation notes and deviations

- **Returns from a batch defer eligibility to the claim.** A member returned
  to `queued` by a split, a void, a transport abort or a settle is checked
  by R7.2 at the next claim, before it is used (R5.4's last paragraph).
- **Reconciliation reads.** A PR that is pending or landed on the first
  observation is accepted; anything else is observed again 30 s later and
  then classified (I4, I5).
- **`batch migrate --record FILE...`** takes the council records of the
  entries in `queue/approved.jsonl`; without one for every queued entry it
  writes nothing.
- **Part 1 (A->B->A)** stages each build as `<qid>-ab-<label>` (kind
  calibration) so the kernel is read from the staged `boot.img`. The edits
  are in `abtest_config.json`: `audio_ext_spkr.conf` and a new
  `PRODUCT_COPY_FILES` file in device/motorola/river, the `dropbox.c` string
  (`CONFIG_DROPBOX=y`, built in), a property in
  `sdm632-river-common-overlay.dtsi`, and `CONFIG_LOCALVERSION` in
  `river_defconfig`. `abtest_allowlist.json` starts empty: part 1 has not
  run yet, and any file it finds that differs between identical builds is
  added there (a `runner_build` change) with its reason.
- **`ensure_image` with a pin.** `scripts/build` gets `RIVER_IMAGE_ID` from
  the runner; `ensure_image` then refuses a missing or different image
  instead of building one (R7.5).
- **TC on the live tree** (checked read-only on 2026-11-23): everything
  passes except item 7, `git lfs ls-files` shows pointer files in
  `external/chromium-webview/prebuilt/x86` and `x86_64`. Preflight stays
  blocked until they are pulled (`git lfs pull` in those projects) or the
  rule is changed.
- **Synchronous merges.** The runner and `change merge` merge through the
  REST endpoint with the tested `sha`; a merge queue or auto-merge is never
  used, so a merge either happened when the call returned or did not.
- **Promotion proof.** `flash --promote` and `flash --recover` require the
  runner's own landed proof (R10.4a) besides the tags: a same-repository
  PR, river-22.2 at its merge commit, parents `[base, tested head]` (the
  base frozen at `merge_started`) and the tested tree; an untouched repo is
  still at its tested commit. A PR still reported OPEN while its merge is
  the tip is 75, before or after the tag, but only if nothing else is
  wrong. `flash --promote` writes the proven merge commits into batch.json
  `promotion` (I11).
- **The lease names the whole tree.** `lease.restore_to` holds every fork
  and the manifest at the base, so the restore can run TC from the lease
  alone, with or without `running`.
- **Held cleanup after a partial settle.** The open batch PRs and their
  tested branches stay untouched until the re-baseline closes (Zak's repair
  may merge them); then cleanup accepts the PRs that merged and closes the
  rest at their tested heads. `batch cleanup drop ID --zak` ends an item by
  hand.
- **Deadlines.** Every tool runs with a deadline (`ctx.TOOL_TIMEOUTS`,
  `tool_timeouts` in `runner/config.json`); past it, or on SIGTERM, SIGHUP
  or SIGINT, the holder is stopped and reaped before the runner releases
  its locks. `phone-setup` bounds each adb call (`ADB_CALL_TIMEOUT`).
- **Blockers.** A blocked or not-qualified tick records `blocker` in the
  state file and writes a report when it changes; `batch status` shows it.
- **Not exercised on hardware.** The runner has not built, flashed or run a
  batch; every path is covered by unit tests with fakes. Qualification
  (R14) is the first real run.

---

# Design: the batch runner (`scripts/batch`), revision 6

Status: approved at council round 6 (Opus, Gemini); implemented in `qa/lib/riverbatch/` with I1-I21 above.

This is charter "Order of work" item 2 (the queue and batch tools) and the
interface for item 3 (the tester thread). It assumes the charter amendment in
zrm625/android_manifest PR #11 and uses its wording. The runner drives the
merged QA harness (`qa/README.md`, `qa/DESIGN.md`) through its existing
interfaces (`batch.json`, `flash_attempt`, `merge_started`, the frozen PR set,
`flash --promote`). Section 17 lists the harness and script changes it needs.
Section 19 maps every round-5 item, and every earlier item that still applies,
to the rule that closes it.

## 0. What changed from revision 5

The structure is unchanged. Every change closes a round-5 item (§19).

- **KB reads without root** (§1, G-C4): no `dev.root()`, no adbd restart;
  only an identity mismatch HALTs.
- **Foreign pairs by kind** (R13.2 step 4, O-C1): a hand-run calibration,
  baseline or experiment pair blocks; a foreign `batch` pair still pauses.
- **Known-good write first, and agreement** (R11.2 step 3, R13.2 steps 1
  and 3, O-C2, S-M2).
- **GitHub auth and rate limits** (R8.2 step 9, R11.6, O-C3); typed lookups
  (H11, S-M7); `mergeable: UNKNOWN` is inconclusive, not a void (R10.2).
- **Merge reconciliation** (R10.4a, S-B2): PR state, branch tip, parents and
  tree read together; disagreement is unknown (75).
- **Paused ticks** serve setup and reports; transport failures go through the
  end-of-tick pass (R8.1, G-C5).
- **Rescue evidence** is matched by attempt ID and reconciled before the
  generic abort (R8.6, R11.2 step 2, H17, G-C6).
- **`qualify` and `stage` refusals** (R2.3, R14.1, G-C1); `stage` pins its own
  image and does no phone work (R14.1, G-C3); `stage --kind baseline` (Z7).
- **Failure history by change name** survives replacement and withdrawal;
  every return to `queued` after a third failure needs Zak (R9.4, S-B4).
- **Approval bundles** stored by digest; replies must cover the round's heads
  (R5.1, R5.2, S-M8).
- **Interlocks:** `change merge` always needs `--record` (G-C2); `change pr`
  takes the build lock (S-M9); renames are checked on both sides (S-B3);
  orphans are checked after each lock (S-M5).
- **Partial settle forces a re-baseline** (R13.4, S-M2).
- **Kernel qualification** reads the kernel from the staged `boot.img`; B4
  waits for a B-affected object file (R14.2, S-M4).

## 1. Terms

- **Fork:** one of the 9 projects in the `zrm625-fork` group. The manifest
  project (`.repo/manifests`, remote `origin`) is not a fork. The other 1,138
  projects are pinned by SHA in `default.xml`.
- **Manifest XML:** the `*.xml` files at the top level of the manifest repo
  (today only `default.xml`; `qa/helper/AndroidManifest.xml` is not one).
- **Baseline XML:** the manifest XML at
  `$BENCH_HOME/qa/known-good.json` `latest.commit_set[".repo/manifests"]`.
- **Change:** one queue entry: one PR per fork it touches, all from one head
  branch, bound to exact heads by a council record (R5).
- **Batch:** changes built and tested together under an ID `bNNNN`, never
  reused. Qualification runs use `qNNNN`, calibration runs the ID given.
- **Commit set:** every fork's commit plus the manifest's, as in
  `river-build.json` and `batch.json` `commit_set`.
- **Base:** the `zrm625/river-22.2` tip of every fork and the
  `origin/river-22.2` tip of the manifest, recorded at composition. Merge,
  no-PR and landed checks compare bases by **exact commit**.
- **Verified base:** every fork's `river-22.2^{tree}` equals the tree of
  `known-good.json` `latest.commit_set[path]` (fetched), and the manifest XML
  equals the baseline XML. The XML half is required for every tick and
  qualification (R8.2 step 2). The tree half is required by qualification
  part 2 (R14.3) and otherwise only reported ("base unverified").
- **Content key:** SHA-256 over the sorted `(fork path, tree)` pairs of the
  composed commit set plus the manifest XML bytes.
- **Failing checks** of a run: for a QA run, the required check IDs in
  `results.json` whose state is not `PASS`; otherwise one pseudo-check naming
  the step: `build`, `flash.boot`, `bench.boot`, `setup`.
- **Known-good build check (KB)** (G-C4): checked, **non-elevating** reads
  through `Device("ZY225DPT7F")`, in `qa/lib/riverbatch/kb.py`. It never calls
  `dev.root()`, `adb root` or `fbmode.running_identity` (whose
  `partition_build_ids` roots the phone and restarts adbd). In order:
  1. `dev.state()`. Empty: `dev.in_fastboot()`; in fastboot: blocked ("phone
     in fastboot"); neither adb nor fastboot: pause ("phone not answering on
     USB: hold the power button", charter).
  2. `unauthorized` or `recovery`: blocked ("phone is <state> on USB").
  3. `device`: `dev.guard()` (getprop only, no root). A `GuardError` (product
     or serial is not this phone's) is the **only** HALT.
  4. One `dev.sh_rc` call reads `ro.boot.slot_suffix`, `sys.boot_completed`,
     `ro.river.build_id` and `ro.vendor.river.build_id` with `getprop` (init
     loads them from `/system/build.prop` and `/vendor/build.prop`, the
     files the root read greps). A failed read: pause as in 1. Boot not
     completed: blocked ("phone still booting").
  5. A missing or `unstamped` ID (`fbmode.stamp` is None): blocked ("phone
     runs an unstamped dev build: return it with `flash --rollback`").
  6. KB holds only if the slot is `slot-state.json` `known_good_slot` and
     both IDs equal `known_good.build_id`; otherwise blocked ("phone runs a
     dev build on slot X: return it with `flash --rollback`").

  "Blocked" exits 0 and leaves the tick loop; the tester notifies after 24 h
  (R16.1).

## 2. The command

R2.1. **Location.** `.repo/manifests/scripts/batch`, standard-library Python 3
run as `python3 -I`, code in `qa/lib/riverbatch/`. It imports `riverqa` but is
not part of it, so runner edits never change `harness_version()`
(`qa/lib/riverqa/qa.py`: `scripts/phone-qa`, `qa/thresholds.json`,
`qa/fixtures.json`, every `.py` under `qa/lib/riverqa/`, every file under
`qa/helper/`, the `paths` and `chain` blocks of `boot-timing.json`, and
`known-good.json` `calibration_seed`).

R2.2. **Runner version: two hashes.** Each is SHA-256 over the listed files'
paths and bytes, sorted by path. Only tracked files count: under
`qa/lib/riverbatch/`, the `*.py`, `*.json` and `*.xml` files (the expected
manifest stub, R7.4) that `git ls-files` lists, never `__pycache__` or
untracked files.
- `runner_build`: `scripts/batch`, `qa/lib/riverbatch/` (as above),
  `scripts/build`, `scripts/bench/lib.sh`, `scripts/bench/Containerfile`,
  `scripts/common.sh`. A change needs both parts of qualification (R14).
- `runner_tools`: `scripts/flash`, `scripts/bench/device-bench`,
  `scripts/device-lock`, `scripts/phone-setup`, `scripts/change`,
  `scripts/merge-upstream`, `scripts/setup-forks`. A change needs part 2.

R2.3. **Subcommands.** A Zak-only command refuses without `--zak`. The flag is
not authentication; it makes the call deliberate, and H7 forbids agents from
passing it.

| Command | Who | What it does |
| --- | --- | --- |
| `batch tick [--max-batches N]` | tester thread | R8. Default N = 3. |
| `batch status [--json]` | anyone | Read-only view of the state file and the waiting-on-Zak list. |
| `batch enqueue --record FILE` | the agent that ran the council | R5.5, R5.6. |
| `batch withdraw NAME` | anyone | Any entry not `in-batch` and not final becomes `withdrawn`. |
| `batch requeue NAME --reason TEXT` | tester thread or Zak | `failed` → eligibility (R9.4). |
| `batch requeue NAME --reason TEXT --zak` | Zak | `failed` or `waiting-zak` → eligibility. |
| `batch tried NAME --reason TEXT` | tester thread or Zak | `failed` → `tried` (R9.4). |
| `batch settle ID --zak` | Zak | R13.4. |
| `batch cleanup drop ID --zak` | Zak | Delete the `cleanup` item of batch ID (R13.3). |
| `batch resume --zak` | Zak | Clear a HALT or a pause; nothing else. |
| `batch stage --kind calibration\|baseline --id ID [--change NAME...]` | a person | R14.1. |
| `batch qualify [--run-only] [--change NAME...]` | a person | R14. |
| `batch migrate --record FILE...` | once | R5.7. |
| `batch report [ID]` | anyone | Rewrite the HTML report. |

R2.4. **`qualify` and `stage` refusals** (G-C1). Both exit 3 if `halt` is
set, and exit 1 if `running` has purpose `batch` or `experiment`, or if any
`batches/<id>/batch.json` named by `running` has `merge_started`. The only
interrupted run they may abort (through R11.2 step 2) is one with purpose
`qualify`, `calibration` or `baseline`, which has no members and no merge
state; `stage` also refuses (exit 1, "roll it back with `batch qualify` or
`batch tick`") if that run still holds a pair, because `stage` does no phone
work (R14.1). Both run while `pause` is set: requalification comes before
`batch resume`. **I20:** both still exit 1 on a live blocking condition (a
disturbed tree or a held lease, a foreign pair, known-good records that
disagree, a strike at its limit) rather than proceeding under the pause. They run their own recovery (R14.1, R14.3), not the paused
pass: a person runs them deliberately.

## 3. Exit codes and configuration

R3.1. **Exit codes.** 0: done, idle, paused, blocked, not qualified, or
waiting on Zak. 1: runner error (R11.5) or a refused command. 3: HALT, now or
still. 75: **resumable**: a lock or the build container is busy, a child of an
earlier run is alive, or GitHub or the network failed or gave no answer; the
state file is consistent and the next call continues from it. The last stdout
line is a JSON summary.

R3.2. `$BENCH_HOME/runner/config.json`: Wi-Fi SSID and PSK file paths, batch
size (4), batches per tick (3), free-space floor (60 GB). No secrets in state
or reports.

## 4. The state file

R4.1. `$BENCH_HOME/runner/state.json` holds the queue and the runner state.
Every change is one **transaction**: `flock` on `runner/state.lock`, read,
apply one transition, write a temp file, fsync, rename, fsync the directory.
Claim, finish, abort, enqueue, withdraw, requeue, tried, settle, cleanup drop
and each phase change are one transaction each. A line then goes to
`runner/events.jsonl` (for reports; never read back). Outside readers (H4,
H12, H15) take the lock shared and **fail closed** if the file exists and
does not parse.

```json
{"version": 6, "next_batch": 1, "next_qual": 1,
 "migrated": null,                      // {"at", "source_sha256"} (R5.7)
 "halt": null, "pause": null,           // {"at", "step"|"reason", "message", "advice"}
 "last_error": null, "voids_in_a_row": 0,
 "last_fail": null,                     // {"members", "checks", "batch", "content_key"} (R9.5)
 "strikes": {"recover": 0, "journal": 0, "setup": 0,    // consecutive failures (R13.2)
             "transport_prs": 0,        // transport aborts at or after prs-open (R11.6)
             "lookup": 0},              // refused or malformed promote lookups (R10.6)
 "setup_wipe_seq": null,                // wipes.jsonl seq at the last verified setup (R13.2)
 "rebaseline": null,                    // {"settle", "at"} until a newer baseline (R13.4)
 "failure_history": {},                 // change name -> [evidence], append-only (R9.4)
 "cleanup": [],                         // items (R13.3), at most one per batch
 "reports_pending": [],                 // R15.1
 "lease": null,                         // {"batch", "since", "expect": {"<fork>": "<sha>"}, "base": {...}}
 "running": null,                       // R4.3
 "entries": []}                         // R4.2
```

R4.2. **Entry:** `name`, `id` (immutable, `e<position>`; Sol SB1),
`branch`, `record_sha256` (the bundle, R5.1),
`kind` (`change` or `experiment`), `prs` (`key` `owner/name#n`, `path`,
approved `head`), `position` (fixed at first enqueue, kept forever), `flags`
(`alone`, `solo`, `upstream`), `status`, `status_at`, `reason`, `rev`
(incremented by every transition of this entry), `decisions`
(`{at, by, action, reason, evidence_index}`, kept forever), `records_before`
(earlier bundle hashes), `batch`, `merged`, `followup`. Failure evidence is
not on the entry: it is `failure_history[name]` (S-B4), which no transition
clears, so it survives record replacement and `withdraw` followed by a new
`enqueue` under the same name.

R4.3. **`running`:** `id`, `purpose` (`batch`, `experiment`, `qualify`,
`calibration`, `baseline`), `members` (`name`, `id`, `record_sha256` and
`rev` at claim; `batch` and `experiment` only; every later lookup of a member
is by `id` and requires the entry still claimed by this run under that
record), `borrowed` (names composed by `qualify` or `stage`, whose entries are
never changed), `phase`, `intent` (R11.1), `versions` (harness,
`runner_build`, `runner_tools`, image ID, manifest HEAD), `base`,
`base_verified`, `composed` (per path: sha, tree), `content_key`,
`attempt_id`, `qa` (run_id, exit, eligible, halted, results path), `ram`,
`merge` (prs, merged, tag), `outcome` (set when decided, applied at finish).

R4.4. **Statuses.**

| Status | Meaning | Leaves by |
| --- | --- | --- |
| `queued` | Runnable | claim → `in-batch`; `withdraw` |
| `in-batch` | Member of `running` | finish or abort only |
| `failed` | A singleton run of this change failed; evidence in `failure_history` (R9.4) | `requeue` or `tried` with a reason; `withdraw` |
| `merged` | Approved heads landed (tagged, or settled by Zak, R13.4) | final |
| `tried` | Decided to be the change's fault, with a written reason | `enqueue` with a new record whose heads differ (R5.6) |
| `needs-reapproval` | Head moved; PR closed, merged or retargeted; PR set changed; conflicts with `river-22.2` alone; partly landed in a settle | `enqueue` with a new record |
| `waiting-zak` | Outside v1 (R5.4); experiment result; any return to `queued` after a third failure (R9.4) | `requeue --zak` or `withdraw` |
| `withdrawn` | Removed | final |

## 5. Approval records, eligibility and enqueue

R5.1. **Approval bundles** (S-M8). A council record and every brief and
cited reviewer reply it names are stored as one immutable bundle,
`$BENCH_HOME/runner/council/<name>/<sha>/`, where `<sha>` is the SHA-256 of
its `record.json`. `enqueue` (and `migrate`) validates the draft record
(R5.2), copies it and its files into a temp directory, fsyncs them, renames
the directory to its final name and fsyncs the parent, all before the
transaction. If that directory already exists, every file must match byte for
byte, or the enqueue is refused. Nothing in a bundle is ever rewritten or
deleted. An entry names its bundle by `record_sha256`; replacing a record
switches that reference inside the transaction, and the old hash goes to
`records_before`.

R5.2. **Record:**

```json
{"version": 2, "name": "perch-launcher", "kind": "change", "alone": false,
 "needs_zak": null, "branch": "perch-launcher",
 "prs": [{"key": "zrm625/android_device_motorola_river#6", "path": "device/motorola/river"},
         {"key": "zrm625/android_vendor_lineage#5", "path": "vendor/lineage"}],
 "rounds": [
  {"scope": "full", "at": "…", "brief": "council-perch-r2-brief.md", "brief_sha256": "…",
   "heads": {"zrm625/android_device_motorola_river#6": "71e7a87ccf8e85a0b38909783fa4732954d83a00",
             "zrm625/android_vendor_lineage#5": "7622c2ff287498cc9f2b4242f8d802bd542bf361"},
   "verdicts": {"opus": "APPROVE 9", "gemini": "APPROVE 8", "sol": "APPROVE 9"},
   "replies": {"opus": {"file": "r2-opus.txt", "sha256": "…"}, "gemini": {…}, "sol": {…}}},
  {"scope": "delta", "at": "…", "brief": "council-perch-delta2-brief.md", "brief_sha256": "…",
   "heads": {"zrm625/android_device_motorola_river#6": {"from": "71e7a87c…", "to": "4f48431d…"}},
   "verdicts": {"opus": "APPROVE 9", "gemini": "APPROVE 9", "sol": "APPROVE 9"},
   "replies": {…}}],
 "prior": []}                            // objected rounds; reported, never validated
```

`kind` is `change`, `experiment` or `tooling` (H4 only). A PR key is
`<owner>/<name>#<number>` exactly as `gh api repos/<repo> --jq .full_name`
spells it. A record is valid only if:
0. `prs` is non-empty.
1. `rounds[0]` is `full` and names exactly the record's PR keys, each with a
   40-hex head.
2. Every later round is `delta`; for each key it names, `from` equals the
   head approved so far, which then becomes `to`. A delta cannot add or drop
   a PR.
3. Every round has APPROVE from each of `opus`, `gemini` and `sol`. An
   objected round goes in `prior`; the chain restarts with a full round.
4. Every brief and reply file exists with its recorded SHA-256.
5. **The evidence binds the round's heads** (S-M8, I14, Sol B3). For each
   round, the brief and each cited reply carry the round's scope block: the
   `Scope:` line and one `Head: <key> <40-hex>` line per PR (full) or one
   `Delta: <key> <40-hex from> -> <40-hex to>` line per PR (delta), equal
   to the round's `heads` exactly; each reply also has the provider's
   recorded `Verdict:`. A reply from another round, about other heads, with
   a reversed delta or swapped PRs, does not count.
6. **One reply per provider** (Sol SB3). No file name and no SHA-256
   appears twice among the record's briefs and replies, in any round: a
   reply reused under another provider's name, or a copy of it, is refused.

R5.3. **PR set check:** the open PRs in every fork and the manifest with head
branch `branch` and base `river-22.2` are exactly the record's PRs, each at
its approved head, each with `isCrossRepository` false.

R5.4. **Eligibility.** One function, `eligible(entry, base)`, run at **every
transition into `queued`** (enqueue, replace, `requeue`, the return of members
after a split, a void, a settle or an abort) and again for every member at
claim (R7.2). It reads GitHub first, outside the state lock; a transport
error exits 75 and changes nothing. In order:
1. **Base camera set** (E12): the base's camera set (R7.3) must equal
   `known-good.json` `latest.cameras`. If not, no entry is classified: enqueue
   and requeue refuse (exit 1, nothing written, "river-22.2 camera set
   differs from known-good: re-baseline (Z8) first"), and the tick pauses
   (R7.2).
2. **needs-reapproval**, only on a confirmed fact: the record no longer
   hashes to `record_sha256` or is invalid, or R5.3 fails (head moved, PR
   closed, merged or retargeted, PR set changed).
3. **Manifest PRs** (E5). A runnable entry holds only PRs in the 9 forks. An
   entry with any `.repo/manifests` PR is never `ok`:
   - **refused** (enqueue only, nothing written): the manifest PR touches
     only tooling (R5.8), alone or with fork PRs;
   - **waiting-zak**: it touches manifest XML ("manifest source changes are
     not supported by runner v1"), `CHARTER.md`, or any other path outside
     the tooling list, alone or with fork PRs.
4. **waiting-zak**, with a reason: a **camera-set change**, whose camera set
   differs from `latest.cameras`; or a record with `needs_zak` (must-work
   removal, fork, money, another device, low-RAM shipping, charter).
5. **ok.**

A change's camera set is read from `stage.CAMERA_SWITCH_FILE`
(`device/motorola/river/qa/cameras`) at the change's approved river head if
that file differs between the merge base and the head, else at the base, and
parsed with `stage.parse_camera_set` (H13). An edit to the switch file that
keeps the enabled set is not a camera-set change. **Eligibility reads
GitHub only and never fetches into the tree** (minor): the merge base is
`gh api repos/<r>/compare/<base>...<head> --jq .merge_base_commit.sha`, and
the file's blob SHA and content at the merge base, the head and the base come
from `gh api repos/<r>/contents/<path>?ref=<sha>` (a 404 is "absent", which
`parse_camera_set` reads as None). So `enqueue` and `requeue`, which hold only
the state lock, never touch a fork while a batch runs.

A return path that cannot complete the check (a transport error, or an abort
inside a crashed tick) puts the members in `queued` and leaves the check to
claim, which runs it before any member is used. No transition writes `queued`
for an entry whose last completed check said otherwise. The staged-set
backstop stays (R7.7).

R5.5. **`batch enqueue --record FILE`.** Eligibility first; then one
transaction. Refused (exit 1, nothing written) unless the record is valid,
its `kind` is `change` or `experiment`, the PR set check passes, eligibility
is not `refused`, and no non-final entry has the name (except R5.6). A
`waiting-zak` outcome is written with its reason, so it shows on the
waiting-on-Zak list and never runs. An `ok` outcome for a name with three or
more entries in `failure_history` is written as `waiting-zak` ("failed
<n> times: queueing it needs Zak", R9.4). Otherwise the entry is `queued` at
the next position, with `upstream` set for `upstream-*` branches and `alone`
from the record. A new entry under a name that was withdrawn takes the next
position but keeps the name's `failure_history`.

R5.6. **Replacing.** `enqueue` on a `tried` or `needs-reapproval` name
replaces the entry in one transaction: new record and heads (fully
validated), eligibility as R5.4, same `position`, `decisions` kept, old record
hash appended to `records_before`, `failure_history` kept, `solo` set, and
the third-failure rule of R5.5 applied. For a `tried` entry the new record's
approved heads must differ from the tried ones (a fix and a new council
round). `enqueue` on any other non-final status is refused.

R5.7. **Migration** (`batch migrate`, once). If `migrated` is set it only
finishes the rename below and exits 0. Otherwise `queue/approved.jsonl`
becomes records and entries through exactly R5.4 and R5.5, in one
transaction that also sets `migrated` with the file's SHA-256 and
`setup_wipe_seq` null; any failure writes nothing. Then the old file becomes
`approved.jsonl.migrated`. `gnss-standalone`: one full round,
`…sdm632-common#3` at `1bf2a8b7822a010a0c70d082818cd82cdae8a55d`
(`council-perch-gps-brief.md`). `perch-launcher`: the record shown in R5.2;
round 1 (Sol OBJECT) goes in `prior`. Briefs are copied from
`/tmp/strip-tools/`, and the reviewer replies that cite the reviewed heads
are copied with their SHA-256 into bundles (R5.1). If a cited reply cannot
be found, or a brief or reply fails R5.2 item 5, migration writes nothing; a missing reply of a delta round needs a new delta round, and
a missing reply of the initial full round needs a **new full round** (a
delta cannot repair the start of the chain). On 2026-11-23 all three PRs were
open on `river-22.2` at exactly these heads.

R5.8. **Tooling changes** (Z4). A manifest PR whose changed paths are all
under `qa/`, `scripts/` or `workspace/`, or are `*.md` files other than
`CHARTER.md`, goes through council code review and `change merge --record`
(H4 pins the reviewed head, enforces the path list on both sides of every
rename and refuses any fork PR or manifest XML). `change merge` refuses
without `--record` in every repo (G-C2), so it can merge nothing but a
council-reviewed tooling-only manifest PR. So every image change ends at
`batch enqueue`. **This path
is not used in production until Zak merges PR #11.** Until then Zak merges
every tooling PR, §17's included. Zak merges charter edits and camera-set
changes in the GitHub web UI, while `batch status` shows nothing running. A
change to a harness-version input needs harness requalification; to a
runner-version input, the parts of R14.4. `tick` refuses until both hold.

## 6. Locks and writers

R6.1. **Locks.** Device lock (`$BENCH_HOME/.device-lock`), then build lock
(`$BENCH_HOME/.lock`), both non-blocking (busy: 75). A tick holds both from
the recovery entry point to the end of a batch, phone phase included, and
releases them after each batch; the next iteration takes them again
(decision Z2). `settle` and `cleanup drop` take both too. The state lock is
held only for one transaction, always last. `enqueue`, `withdraw`,
`requeue`, `tried` and `status` take only the state lock.

R6.2. **The holder.** The runner opens its lock files close-on-exec and
passes them to no tool. Every child runs through
`python3 -I qa/lib/riverbatch/hold.py`, started with `pass_fds` = the two
lock fds and `start_new_session=True`. The holder:
1. creates a pipe and starts a stub (`close_fds=True`, new session,
   `BENCH_LOCK_TOKEN`, `BENCH_BUILD_LOCK_TOKEN` and `RIVER_BATCH_ID` set) that
   blocks reading the pipe; on one byte it `exec`s the real command, so pid,
   pgid and start time do not change; on EOF (the holder died) it exits 1
   without `exec`;
2. writes `runner/children/<pid>.json` (`cmd`, `pid`, `pgid`, host
   `boot_id` from `/proc/sys/kernel/random/boot_id`, start time from
   `/proc/<pid>/stat` field 22) through a temp file named `.<pid>.tmp` and
   rename, and fsyncs it and the directory (readers list only names matching
   `^[0-9]+\.json$`, so a temp file never counts or blocks);
3. writes one byte to the pipe, releasing the stub;
4. waits for it, then SIGTERMs whatever is left in its process group,
   SIGKILLs it after 10 s, reaps, deletes its file and exits with the child's
   status. On SIGTERM it does the same at once.

**Live child records** (`riverqa.children`, H15): a file counts only if its
`boot_id` is the current host boot and its pid's start time matches (or, with
the leader gone, a live process still has that pgid). Others are deleted. A
tick exits 75 while any counted file remains or while a podman container named
`lineageos-build` is running. A `lineageos-build` container in any other state
(created, exited, dead) is removed with `podman rm lineageos-build` (never
`--force`); if that fails, 75. So the locks live exactly as long as the child,
no tool holds a lock fd, and daemons that tools start (the adb server,
`conmon`, podman's pause process) inherit none. The runner, on SIGTERM, SIGINT
or SIGHUP, signals its holder, waits, commits state and exits.

R6.3. **Tokens** (H1, H2, H15). A lock is taken by opening the file without
truncating it, then `flock`, then the **orphan check** (`live_children()`,
R6.2) again, then writing a fresh token (S-M5). A live orphan found after
`flock` releases the lock without writing anything and refuses (the runner:
75; a tool: exit 1). This holds for every lock, in Python (`DeviceLock`, the
runner) and in shell (`acquire_build_lock`, `device_lock`); a check before
`flock` is optional and never replaces the one after. A tool that fails to
lock never writes the file. A tool given a token reuses the lock only if the
file holds it and someone holds the lock. Device tools treat a build lock held
under `BENCH_BUILD_LOCK_TOKEN` as "no build running".

R6.4. **Tree lease.** `lease` is set in the same transaction as phase
`checking-out`, before the first checkout, with each touched fork's composed
commit (`expect`) and its base (`base`). It is cleared only after restore and a
passing TC against `river-22.2`, and it survives the runner's death.

R6.5. **Cooperating tools** (H4, H15). `scripts/build`, `flash`,
`device-bench`, `phone-qa`, `phone-setup`, `change`, `merge-upstream` and
`setup-forks` refuse (exit 1) while a live child record exists whose pgid is
not the caller's session ID (so the runner's own child and its descendants
pass; an orphan of a killed runner blocks everyone). `change start|pr|merge|tag`,
`merge-upstream` and `setup-forks` also take the build lock once, at the
start, for their whole run (around every `repo forall`; the per-project
commands take no lock), read the state file, and refuse while `running` or
`lease` is set or the file does not parse. So none moves a base or the tree
from claim to finish. Because the runner holds the build lock from claim to
finish, `change pr` can never push to a member's branch while the runner
finalizes it (S-M9; R10.7 also re-reads the head after closing). A hand-run
`repo sync`, `git` or `git push` command is not interlocked: TC (R7.4) and R8.2 step 2 catch it, and H7 forbids it while
`batch status` shows a batch or a lease. A web-UI merge is caught by CS
(R10.4); the seconds before the merge call are the known residual (Z6).

## 7. Composing and building

R7.1. **Members.** Candidates are `queued` entries in `position` order. If
the first has `solo`, `alone` or `upstream`, or is an experiment, it runs
alone. Otherwise take consecutive candidates with none of those, up to 4.
Order is never changed.

R7.2. **Member checks**, before claim. First the base camera set (R5.4 step
1): a mismatch pauses ("river-22.2 camera set differs from known-good:
re-baseline, Z8") and no entry is touched. Then, for each member,
eligibility (R5.4), and each PR's `refs/pull/<n>/head` fetched from `zrm625`
into `refs/river-batches/pr/<path>/<n>` equals the approved head. A transport
error exits 75 with nothing written. A confirmed failure moves that entry
(`needs-reapproval` or `waiting-zak`) in its own transaction, and selection
starts again. The runner never merges a branch name.

R7.3. **Compose, one change at a time across all its forks**, from
`current[repo] = base`. The manifest is never composed: it stays at its base.

```text
for change in members:
    trial = {}
    for repo in change.repos:
        tree = git merge-tree --write-tree current[repo] <approved head>
        if conflict: drop the whole change; continue with the next change
        trial[repo] = git commit-tree tree -p current[repo] -p <approved head> -m "batch bNNNN: merge …"
    if camera set of (current + trial)[river] != known_good.latest.cameras: drop it (waiting-zak); continue
    current.update(trial)
```

A change that conflicts with an earlier member stays `queued`; one that
conflicts with the base alone becomes `needs-reapproval`. The runner writes
`refs/river-batches/<id>/<repo>` and local `batch/<id>` branches (overwriting
leftovers of an unclaimed earlier try at this ID), using
`git -c user.name=river-batch -c user.email=river-batch@localhost` and the
same `GIT_AUTHOR_*`/`GIT_COMMITTER_*`.

The **claim transaction** re-reads every member **by its immutable `id`**
and requires the same name, `status` `queued`, its `rev` and its
`record_sha256` unchanged since the member checks (a concurrent `withdraw`
wins, and a new entry enqueued under the withdrawn one's name is another
entry, Sol SB1); otherwise it writes nothing and selection starts again.
Every other compare-and-update of an entry (member checks, compose drops,
requeue, enqueue's replacement) checks `id` and `rev` the same way, and the
merge rule (R10.1) requires every member still claimed under its record.
It records members with their `id`, `record_sha256`, `rev`, base, `base_verified`, composed SHAs and
trees, content key and versions, advances `next_batch`, and makes members
`in-batch`.

R7.4. **Whole-tree check (TC).** Passes only if:
1. **Effective manifest:** `.repo/manifest.xml` is exactly the stub that
   includes `default.xml` (compared byte for byte with
   `qa/lib/riverbatch/manifest_stub.xml`), and `.repo/local_manifests` does
   not exist or is empty.
2. Every fork's HEAD is its expected commit (composed or `river-22.2`), the
   manifest's is the base's manifest commit, and every other project's HEAD
   equals its `default.xml` revision, which must be a SHA.
3. `git status --porcelain --ignored=matching --untracked-files=normal` is
   empty in every project and the manifest, except the entries of
   `qa/lib/riverbatch/tc_allowlist.json` (a runner-version input). An entry
   ending in `/` is a directory prefix. Today:
   `tools/extract-utils/extract_utils/__pycache__/`, and in the manifest
   `qa/lib/riverqa/__pycache__/`, `qa/lib/riverbatch/__pycache__/`,
   `qa/tests/__pycache__/` and `qa/.ruff_cache/`. An allowlisted
   `__pycache__/` may hold only `*.cpython-*.pyc` files named after a `.py`
   in its parent; `qa/.ruff_cache/` may hold anything. Nothing in the build
   reads these paths. A failure lists every blocking path.
4. `git ls-files -v` shows no assume-unchanged (lower-case tag) or
   skip-worktree (`S`) entry in any project or the manifest.
5. `.repo/project.list` equals the manifest's project paths.
6. Outside the projects there is only `.repo`, `.repo-bootstrap`, `.claude`,
   an empty `out/`, the `AGENTS.md` and `CLAUDE.md` links to
   `.repo/manifests/AGENTS.md`, and the manifest's `copyfile` and `linkfile`
   destinations. Each `copyfile` destination is byte-identical to its source;
   each `linkfile` destination is a symlink with the relative target `repo`
   writes.
7. `git lfs ls-files` shows no pointer files.

TC runs over the projects in parallel (8 workers) and records its duration.
It runs at preflight, after checkout, after the build (before stage) and after
restore. Preflight also requires git ≥ 2.38 and `git lfs version`, with
`$TOP/.repo-bootstrap/bin` first on `PATH`.

R7.5. **Checkout and build**, under the lease: `git checkout batch/<id>` and
`git lfs pull` in each touched fork, TC, then the image check, the
intermediate clearing, then `scripts/build --installclean --batch <id>`.
- **Image check:** `podman image inspect --format '{{.Id}}'` of the tag
  `ensure_image` would use must exist and equal the pinned image ID
  (`runner/qualified.json` `image_id`, the qualification pin, R14.4, or `stage`'s own pin, R14.1)
  **before** the build. Missing or different: void and pause ("build image
  changed: requalify, R14.4"); the runner never lets `ensure_image` build a
  new image.
- **Intermediate clearing** (E1), before every batch, qualification and
  calibration build, and before the R7.6 rebuild, under the build lock. The
  runner deletes these directories under
  `out-stripped/target/product/river/obj/` if present, then checks that none
  exists:
  - `KERNEL_OBJ` (`kernel.mk` `KERNEL_OUT`: `.config`, the kernel and
    `Image.gz-dtb` with its appended DTBs, the in-tree modules);
  - `DTBO_OBJ` (`DTBO_OUT`; river sets `BOARD_KERNEL_SEPARATED_DTBO`, so the
    overlays and `BOARD_PREBUILT_DTBOIMAGE` = `DTBO_OBJ/arch/arm64/boot/dtbo.img`
    are built here, `BoardConfigKernel.mk`);
  - `DTB_OBJ` (`DTB_OUT`; only with `BOARD_INCLUDE_DTB_IN_BOOTIMG`, which
    river does not set);
  - `RECOVERY_KERNEL_OBJ` (only with `TARGET_KERNEL_RECOVERY_CONFIG`, which
    river does not set).

  `m installclean` (`cleanbuild.go` `installClean`) removes the rest that a
  kernel or device-tree change reaches: `obj/PACKAGING` (the
  `kernel_modules` and `depmod_*` staging), the product `kernel`, every
  product `*.img` (boot, dtbo, recovery), `root`, `ramdisk` and `recovery`.
  The list is code in `qa/lib/riverbatch/` (a `runner_build` input). Cost:
  the kernel and overlays are rebuilt for every build, a few minutes.
  `scripts/build` deletes `river-build.json` and `.bench-result` right after
  taking the lock (H1), so a killed build leaves nothing the runner trusts.
- **Result:** the new `river-build.json` must name `<id>-<hash>`, the composed
  commit set, and `image_id` equal to the pinned image ID (H1). A mismatch
  voids the batch and pauses ("build image changed").

R7.6. **Build failures.** `m` failing (`.bench-result` code 1-89 or 92-124)
gets one immediate rebuild of the same tree in a fresh container, after the
image check and the intermediate clearing again. A passing rebuild is used and
nothing is recorded. A second failure is a `fail` whose evidence is both logs
and fingerprints (the first `FAILED:` line and the first `error:` line after
it, ANSI codes and `/src/` stripped). No `.bench-result`, codes 90
(breakfast), 91 (installclean), 125-127 and 128 or more (killed; 137 is OOM),
`scripts/build` exit 1 before the container ran, and low disk are `void`.

R7.7. **Stage**, after a passing TC: `phone-qa stage <id> --kind batch` (plus
`--experiment` for an experiment). The runner never writes
`camera_set_change`. A staged `cameras.set` that differs from
`known-good.json` `latest.cameras` voids the batch and pauses ("camera set
differs from known-good; needs Zak").

R7.8. **Restore**, from `lease` alone: for each touched fork,
- HEAD at `lease.expect[fork]` and clean: `git checkout river-22.2`;
- HEAD already on `river-22.2` at `lease.base[fork]` and clean: accepted;
- anything else (dirty, another commit, another branch): left alone.

Then TC against `river-22.2`. If every fork is restored or accepted and TC
passes, the lease is cleared. Otherwise the lease stays, the batch is void
(`tree-disturbed`) and the runner pauses naming each fork and what it found.
The runner never uses `--force`, `reset --hard`, `clean` or `stash`, and never
discards uncommitted work. Restore needs no GitHub.

## 8. The tick

R8.1. **Order.**
1. Read the state file. Write any `reports_pending` (R15.1), halted or not.
   `halt`: exit 3.
2. Take both locks (busy: 75). Read `halt` again (exit 3). The children and
   container check (R6.2).
3. **`pause` set** (G-C5): the **paused pass** only, then exit 0. It does
   R13.2 step 4 (a foreign pair: nothing touches the phone) and then step 7
   (setup on known-good), each only under its own preconditions and only if
   its strike count is below 2; then `reports_pending`. Nothing else runs:
   no R11.2, no `flash --recover`, no journal restore, no `cleanup` item,
   no GitHub call. HALT stays fail-closed (step 2).
4. The recovery entry point (R13.2), without setup.
5. Up to N times: preflight (R8.2), members (R7.1, R7.2), compose and claim
   (R7.3), the batch (R8.3), release the locks, then steps 2 to 4 again.
   Nothing runnable, a blocker, or a pause: leave the loop. **Any unknown
   outcome (R11.6) in step 4 or the loop** (a forward path's exit 75,
   preflight step 9, member checks, a batch's transport abort) skips to
   step 6; it never exits 75 directly.
6. The end-of-tick pass: the recovery entry point with setup (or the paused
   pass, if a pause was set in this tick); then `reports_pending`. Exit 75 if
   an unknown outcome left the loop, else 0 ("idle" if nothing ran).

Every tick runs the recovery entry point or the paused pass, and their local
steps never wait for GitHub, so nothing the phone or host owes waits for a
runnable entry or for the network.

R8.2. **Preflight.** A blocker exits 0 "blocked: <what>" and goes in the
report. A pause condition that still holds after `batch resume` pauses again
at the next preflight.
1. Git, git-lfs, `PATH` (R7.4).
2. **Manifest** (E4). Fetch `origin`. **First** compare the top-level
   `*.xml` at `origin/river-22.2` with the baseline XML: any difference
   pauses at once ("manifest pins changed: run a new baseline (charter:
   council review and a new baseline), Z7"), whatever the checkout's state.
   Then `.repo/manifests` must be on `default`, clean, at
   `origin/river-22.2`; behind and clean: fast-forward, release the locks and
   re-exec once (`RIVER_BATCH_REEXEC=1`). Then the checkout's XML must equal
   the baseline XML too (same pause). This holds whoever did the
   fast-forward or `repo sync`.
3. Every fork on `river-22.2`, equal to `zrm625/river-22.2` after a fetch.
   Behind and clean: fast-forward. Ahead, dirty, or on another branch:
   blocked.
4. TC against `river-22.2`; blocked on failure, listing the blocking paths.
5. At least 60 GB free in `$BENCH_HOME`.
6. `phone-qa qualified` exits 0 (H3) and `runner/qualified.json` matches
   (R14.4); otherwise exit 0 "not qualified: <reasons>".
7. No pair in `slot-state.json`, `known-good.json` present, and KB (§1, E3,
   G-C4). A null `known_good.build_id` is blocked ("known-good build
   unstamped").
8. **RAM reference** (E12): if `known-good.json` `latest.batch` equals
   `calibration_seed.batch` (the known-good is a baseline) and
   `calibration_seed.median_mb` exceeds `ram_reference.median_mb` + 5 MB
   (`RAM_MEDIAN_MARGIN_MB`), pause ("the re-baselined RAM <x> MB cannot meet
   the preserved reference <y> MB from <tag>; raising it is Zak's decision").
   **Re-baseline after a settle** (S-M2): if `rebaseline` is set, it is
   cleared (one transaction) only when `slot-state.json` `baseline_closed`
   is later than `rebaseline.at` and `known-good.json` `latest.batch` is that
   baseline; otherwise pause ("base unverified after settle of <id>:
   re-baseline, R13.4").
9. **GitHub** (O-C3): `gh api rate_limit` answers, and
   `gh api repos/<r> --jq .permissions.push` is `true` for the 9 forks and
   `zrm625/android_manifest`. `false`, 401, or 403 without rate-limit
   evidence: pause ("GitHub auth: <repo>: <status>"). 403 with rate-limit
   evidence, 429, or no answer: unknown, leave the loop (R8.1, exit 75).

R8.3. **One batch.** Each phase is committed before its step (R11.1).

| # | Phase | Step |
| --- | --- | --- |
| 1 | `claimed` | The claim transaction (R7.3). |
| 2 | `checking-out` | Lease; checkout, LFS, TC. |
| 3 | `building` | Image check, intermediate clearing, build, maybe rebuild (R7.5, R7.6). |
| 4 | `staging` | TC; `phone-qa stage` (R7.7). |
| 5 | `restoring` | R7.8. |
| 6 | `flashing` | Assert `batch.json` `flash_attempt` is unset; `flash --batch <id> --wipe`; then R8.6. |
| 7 | `benching` | R8.4; a `device-bench` exit 4 goes through R8.6. |
| 8 | `setting-up` | `phone-setup --wifi SSID_FILE PSK_FILE`, then the setup readback (R13.2 step 7); a passing readback sets `setup_wipe_seq` to the current wipe seq in the same transaction as phase 9. |
| 9 | `testing` | `phone-qa --batch <id>`; read the H6 line and `results.json`; bind (R8.5). |
| 10 | `decided` | Commit `outcome` (R9). Pass: R10. Otherwise phase 11. |
| 11 | `rolling-back` | `flash --rollback`, unless R8.6 verified a rescue rollback. |
| 12 | `finished` | Finish transaction (apply `outcome`, clear `running`), then report, retention, pruning (R10.8). |

`flash` never writes a second attempt for an ID: a batch interrupted before
`merge_started` is aborted, and the ID is never reused.

R8.4. **Bench and RAM.** `device-bench current --boots 1 --batch <id>`
measures bench boot 1; the harness adds its sysrq bench boots (one for every
batch, two for a kernel batch: `stage.sysrq_boots_required`). Then
`regression.ram_decision` on this attempt's rows (`bench.attempt_rows`:
`batch.json` `bench_runs` whose `flash_attempt` is `attempt_id`) and
`known-good.json`; if 3 boots are required and fewer rows exist,
`device-bench current --boots <3 - rows> --batch <id>` once.

R8.5. **Binding.** A QA result counts only if `results.json` has
`mode: batch`, `batch` = the ID, `run_id` = the H6 line, `flash_attempt` =
`attempt_id`, `harness_version` = `running.versions.harness`, serial
`ZY225DPT7F`, and `slot-state.json` still reserves this batch's pair. Bench
rows count only with `flash_attempt` = `attempt_id`. Otherwise the run is void.

R8.6. **Adopting the flash attempt** (E10), by the tool's exit code:
- **`flash` exit 0:** `batch.json` `flash_attempt`, the last attempt in
  `batches/<id>/flash.json` (result `booted`; `bench.current_attempt`
  returns it) and the last entry of the `slot-state.json` pair's `attempts`
  are the same ID, and that attempt's `target` is the pair's target. Commit it
  as `attempt_id`.
- **`flash` or `device-bench` exit 4 (rescued):** after `flash`, commit the
  durable attempt ID from `batch.json` `flash_attempt`, which must equal the
  last attempt in `flash.json` (after `device-bench`, `attempt_id` is already
  committed); then verify the rescue with the **rescue evidence** check
  below. The class is `fail` (`flash.boot` or `bench.boot`); phase 11 is
  skipped.
- **Rescue evidence** (G-C6), used here and by R11.2 step 2a: `slot-state.json`
  has no pair; its last `rolled-back` event names this batch, its `how`
  starts with `rescue:` and contains `wipe #<seq>`; the `wipes.jsonl` record
  with that `seq` has `ok` true, `how` `rollback`, `attempt_id` equal to the
  attempt ID (H17 makes `device-bench` pass it; `flash` already does), and
  `at` no earlier than the attempt's `started_at` in `flash.json` (the
  flash's own wipe has the same attempt ID but `how` `flash`). Matching by both
  the event's seq and the attempt ID means an earlier or unrelated rollback
  never passes as this rescue.
- **`flash` exit 1 with no attempt** (`flash_attempt` still unset, no new
  attempt in `flash.json`, no pair): `void`, nothing to adopt.
- Anything else (exit 3; exit 1 with an attempt or a pair; any mismatch
  above): HALT.

## 9. Outcomes

R9.1. **Classes.**

| Class | Signal | Result |
| --- | --- | --- |
| `pass` | `phone-qa` exit 0, `eligible: true`, `halted` null, bound | R10 if R10.1 holds |
| `halt` | any tool exit 3; `results.json` `halted` non-null at any exit code; R8.6 mismatch | HALT (R13.1); after `batch resume`, R11.2 decides |
| `fail` | a build failing twice (R7.6); a verified rescue (R8.6); `phone-setup` or its readback failing on the candidate; bound `phone-qa` exit 1 or 2, or exit 0 not eligible for a reason other than gate state | R9.3 to R9.5 |
| `void` | infra build codes (R7.6); a tool exit 1 before it touched the phone; an unbound QA result; a failed R7.2 check or a CS mismatch before `merge_started`; `tree-disturbed`; camera mismatch; build image changed | members `queued`, nothing recorded; a second void in a row pauses |
| `transport` | before `merge_started`: GitHub or the network failed or gave no answer, `mergeable` still `UNKNOWN` after 30 s, or an auth refusal (R11.6, R10.2) | abort, members `queued`, nothing recorded, `voids_in_a_row` unchanged; `strikes.transport_prs` (R11.6); exit 75, or pause for auth |

A void never records anything against a change. `voids_in_a_row` is reset by
any `pass` or `fail` and by `batch resume`. `eligible: false` because of gate
state (controls, calibration, thresholds) cannot happen while preflight 6
holds; if it does, the runner pauses ("gate not qualified").

R9.2. **Experiments.** Alone, staged with `--experiment`, the full sequence,
then always rollback; never merged. The entry becomes `waiting-zak` with the
numbers and the QA result. Low-RAM mode is an experiment by charter.

R9.3. **Two or more changes failed.** After rollback, every member goes back
to `queued` at its own position with `solo: true` and nothing recorded, and
runs alone later, in order (charter: "a batch of several changes is split").
Cost: n solo runs. The batch report keeps the evidence and names the solo
runs it caused.

R9.4. **One change failed.** There is no automatic rerun of a device run (the
build's rebuild, R7.6, is part of one run). The entry becomes `failed`, and
an evidence record is appended to `failure_history[name]`: `{batch, at,
record_sha256, class step (build, flash, bench, setup, qa), failing checks,
build fingerprints, run_ids, results.json and report paths, attempt_id,
base_verified, content_key, harness_version}`.

**Third-failure rule** (S-B4; PR #11: "after a change's third failure, only
Zak can put it back in the queue"). `failure_history` is keyed by change
name and append-only: record replacement (R5.6) and `withdraw` followed by a
new `enqueue` keep it. While it holds three or more records, **every
transition into `queued` from outside a batch** (`enqueue`, a replacing
`enqueue`, `requeue` without `--zak`) writes `waiting-zak` instead ("failed
<n> times: queueing it needs Zak"); only `requeue --zak` reaches `queued`.
Returns of members from `in-batch` (a split, a void, a transport or
desk-suspect abort, a settle with nothing landed) keep their earlier
admission and are not affected.

The **tester thread** (R16) reads the evidence and the QA report and records
one decision, with a written reason that names the evidence it relied on:
- `batch requeue NAME --reason TEXT`: the phone, the desk or the
  workstation caused it. The entry goes through eligibility (R5.4) and takes
  its outcome: `queued` at its position with `solo` and the same record, or
  `needs-reapproval` or `waiting-zak`. If `len(failure_history[name]) >= 3`,
  a requeue without `--zak` makes it `waiting-zak` (third-failure rule) with
  the reason kept in `decisions`. A requeued change can only merge through a
  later clean batch (R10.1).
- `batch tried NAME --reason TEXT`: the change caused it. Always available
  from `failed`, whatever the count. The entry becomes `tried` and the reason
  and evidence go in `decisions` ("how it failed"). It returns only with a
  fix and a new council record (R5.6), and after a third failure only
  through Zak.

Zak can make either decision too. The waiting-on-Zak list shows every
`failed` entry older than 24 h.

R9.5. **Desk suspect** (E14; charter: "several different changes fail the
same way in a row"). Only runs of purpose `batch` read or write `last_fail`;
experiments, qualification and calibration leave it alone. At `decided`, for
a `fail`: if `last_fail` is set, this run's content key differs from
`last_fail.content_key`, the two share a failing check (two build failures
share `build`), and this run's members are **not** a subset of
`last_fail.members` (a split of the same batch), the outcome becomes:
rollback, members back to `queued` with flags unchanged and nothing recorded,
and a pause "desk suspect: <check> (failed in <batch> and <batch> with
different changes)". Otherwise `last_fail` becomes this run's
`{members, checks, batch, content_key}`. A `pass` and `batch resume` clear
`last_fail`; voids and transport aborts leave it alone.

## 10. Merge, tag and promotion

R10.1. **Merge rule:** class `pass`; `batch.json` kind `batch`,
`experiment: false`, `camera_set_change: null`; `running.purpose` `batch`;
members non-empty; the harness and both runner versions still qualified and
equal to those recorded at claim.

R10.2. **Open PRs.** Commit phase `prs-open`, with a `cleanup` item for this
batch (repos, `batch/<id>`, composed SHAs), before the first push. Then:
1. Push `batch/<id>` to `zrm625` in every touched fork, never forced. A remote
   branch confirmed at another SHA: HALT.
2. Find the batch PR by head branch (`gh pr list --head batch/<id> --state
   all`); open it only if none exists (ready, base `river-22.2`, title
   `batch bNNNN: <names>`, body with the change PRs, council records, QA
   report and numbers). A found PR that is closed, merged, or at another
   head: void (minor), before `merge_started`.
3. Each batch PR: head = tested commit, base `river-22.2`, `mergeable`
   `MERGEABLE` (asked again for 30 s while `UNKNOWN`). `CONFLICTING`: void.
   **Still `UNKNOWN` after 30 s is inconclusive** (S-M7): a `transport`
   abort (R9.1), which keeps every approval and counts no void.
4. Write `prs`, `no_pr` (every other repo of the commit set, the manifest
   always; for b0001, 6 forks and the manifest), `tag` (`known-good-bNNNN`)
   and `tag_repos` (every repo, as `https://github.com/zrm625/<name>`) into
   `batch.json` with the harness's atomic writer, under the device lock.

R10.3. **Last checks, then `merge_started`.** CS (R10.4) with nothing merged;
the pair must still be this batch's, written by `attempt_id`; the versions
unchanged. A mismatch: void (`stale-base`), abort. Then
`flash --merge-started <id>` must exit 0; a refusal is void, except "PR
record changed", which is a HALT.

R10.4. **Merge** (phase `merging`), each batch PR in path order, on the first
try and on every resume. From fresh lookups (a failed lookup: 75, R11.6; a
false check: HALT before anything more is merged), and only on a
**reconciled** state (R10.4a):
1. **CS, the commit-set check** (E6), from `git ls-remote` of every repo of
   the commit set:
   - every frozen PR repo with no recorded merge: tip = `running.base[path]`;
   - every `no_pr` repo, the manifest included: tip = its base;
   - every PR repo with a recorded merge commit M: tip = M, M's parents are
     exactly `[base, tested]` (first parent the base), and M's tree (fetched)
     is the tested tree.
2. The repo and number are those frozen in `batch.json`
   `merge_started.expected_prs[path]`.
3. `gh pr view`: `isCrossRepository` false, `baseRefName` `river-22.2`,
   `headRefOid` the tested SHA. State `MERGED`: record its merge commit, run
   CS again, and go to the next PR. State `OPEN`: go on. Anything else: HALT.
4. `slot-state.json` still reserves this batch's pair.

Then the synchronous REST merge, `PUT repos/<repo>/pulls/<n>/merge` with
`sha=<tested sha>` and `merge_method=merge` (Sol B4): it merges at exactly
that head now or refuses, and never adds the PR to a merge queue or turns
on auto-merge (as `gh pr merge` can), so nothing lands after the runner
stopped looking. Commit the merge commit. A confirmed refusal (or an answer
without `merged: true`): HALT. An unknown outcome: R11.6. Never roll back.
In step 3, a PR found in any state but `OPEN` goes through R10.4a again
(recorded if it landed) before anything halts (Sol M2).

R10.4a. **Merge reconciliation** (S-B2), before CS on every resume and after
every unknown merge outcome. For each frozen PR with no recorded merge, one
**observation** is read together: the PR (`gh pr view`: `state`,
`mergeCommit`, `headRefOid`, `baseRefName`), the branch tip
(`git ls-remote`), and, if the tip is not the recorded base, the tip
commit's parents and tree (`gh api repos/<r>/git/commits/<tip>`). It is
classified:
- **pending:** tip = base, PR `OPEN` at the tested head on `river-22.2`;
- **landed:** tip's parents are exactly `[base, tested]` and its tree is the
  tested tree, and the PR is `MERGED` with `mergeCommit` = tip, or still
  `OPEN` (GitHub lag). Record the tip as the merge commit;
- **unknown:** any read failed; or the PR is `MERGED` but the tip is the base
  or another commit, or `mergeCommit` is null; or two reads of the same
  observation, 30 s apart, differ. Exit 75 (R11.6); nothing is recorded or
  merged;
- **mismatch:** two observations 30 s apart agree, and the tip is neither
  the base nor a landed merge (or the PR is `CLOSED`, retargeted or at
  another head). HALT before anything more is merged or tagged.

Only after every PR is pending or landed does CS (step 1) run on the
reconciled state, before another merge or any tag. An unknown that persists
is reported and notified after 24 h (R16.1); it never becomes a mismatch by
waiting.

R10.5. **Landed check** (phase `landed`): CS with every PR recorded, before
any tag. A mismatch: HALT ("untested content reached river-22.2").

R10.6. **Tag, promote, record** (E8). Promotion is the commit point.
1. Annotated `known-good-bNNNN` at the merge commit (PR repos) or the tested
   commit (`no_pr` repos), pushed alone; then `git ls-remote <url>
   refs/tags/known-good-bNNNN^{}` must give the intended commit everywhere.
   An existing tag there is accepted; elsewhere: HALT.
2. `flash --promote <id>`, skipped if `slot-state.json` already names this
   batch known-good. Exit 75 (lookup transport failure, H11): exit 75,
   promote again next tick. **Exit 1 with H11's lookup line** (`refused` or
   `malformed`, with its evidence; nothing touched the phone) (S-M7): an
   auth refusal (401, or 403 without rate-limit evidence) pauses at once
   ("GitHub auth", O-C3); otherwise `strikes.lookup` + 1 and retry next
   tick; the second consecutive one pauses with the evidence. A promotion
   resets the strike. Exit 3, or exit 1 without that line: HALT (Zak then
   resolves the pair and settles, R13.4).
3. Only after promotion: `known-good.json` = `regression.update_on_tag(kg,
   tag, id, commit_set=<tagged commits>, run_ids, this attempt's rows,
   staged cameras)`, atomically, skipped if `latest.batch` is this batch. It
   is pure host work from `batch.json`, `device.csv` and the stage, so it is
   always finishable. The derived obligation **KW** (O-C2) holds for batch X
   only if all of these hold: X is `running.id`; `batches/X/batch.json` has
   kind `batch` and `merge_started` set; the last slot-changing
   `slot-state.json` history event (`reserve`, `rolled-back`, `promote`,
   `close-baseline`; log-only events such as `fb-mode-recovered` are
   skipped) is `promote` for X, and `known_good.batch` = X; and
   `known-good.json` `latest.batch` ≠ X. R11.2 step 3 finishes KW first
   (S-M2); R13.4 finishes it for a settled batch. The first tag retires the
   RAM seed flag; the reference keeps the lower median and its provenance.

R10.7. **Change PRs.** Wait up to 60 s for GitHub to mark each merged. Still
open at its approved head: close it with a comment naming the batch PR and
tag, then read its head again (S-M9): if it moved between the read and the
close (a hand `git push`; `change pr` cannot, R6.5), reopen it with a
comment like the one below and record `followup`. Head moved past the
approved head: leave it open with a comment
("approved head <sha> landed in bNNNN; later commits need a new council
round") and record `followup`. Every comment carries
`<!-- river-batch bNNNN -->` and is posted only if none with that marker
exists.

R10.8. **Finish.** Delete the remote `batch/<id>` branches (R13.3 rules);
fast-forward local `river-22.2` where checked out and clean; the finish
transaction makes the members `merged` and drops the batch's `cleanup` item.
Then delete local `batch/<id>` branches and the `refs/river-batches/` refs of
finished and aborted batches.

## 11. Crash recovery: one rule

R11.1. **Journal.** Before each step that changes anything outside the state
file, the runner commits `running.intent = {step, before, after}`: the state
it expects before and the observable state that proves the step happened
(merge of a path: before "PR #n open at T, base tip B", after "PR #n merged,
merge commit parents [B, T]"). After the step it commits the next phase with
`intent` cleared. `prs-open` and its `cleanup` item are committed before the
first push.

R11.2. **The rule.** Run by the recovery entry point (R13.2), under both
locks, before anything else, if `running` is set:
1. **Read `batches/<id>/batch.json` `merge_started` first** (E9). It is
   local and durable; it alone decides the direction, never the runner's
   phase. No `batch.json` (a crash before stage): unset. An unreadable one:
   HALT.
2. **Unset: abort, local only.** No GitHub call; a pending intent is not
   looked up remotely; batch branches and PRs are left to the `cleanup`
   item written at `prs-open` (R13.3). In order, each part idempotent and
   committed:
   a. **Rescue evidence first** (G-C6). If no `outcome` is committed, the
      phase is `flashing` or `benching`, and the rescue evidence of R8.6
      holds for the attempt (`running.attempt_id`, else `batch.json`
      `flash_attempt` when it equals the last attempt in `flash.json`):
      commit outcome `fail` (`flash.boot` or `bench.boot`) through the
      phase-10 decision (R9, R9.5 included), with that evidence. A completed
      rescue is never lost as a void.
   b. If `slot-state.json` holds this batch's pair: `flash --rollback` (exit
      0: continue; 3: HALT; 1: a `recover` strike, the second pauses). No
      pair: nothing. Before `merge_started` the runner uses `--rollback`,
      which needs no GitHub, never `--recover`.
   c. If `lease` is set: R7.8.
   d. Apply a committed outcome: `fail` or experiment as R9; **desk suspect**
      (minor) as R9.5 (members `queued`, flags unchanged, nothing recorded,
      the pause); `transport`: members `queued`, no void counted, and
      `strikes.transport_prs` as R11.6. Otherwise, including a committed
      `pass` that never reached `merge_started`, members go back to `queued`
      at their positions, flags kept, nothing recorded, and it counts as a
      void.
   e. Clear `running`; prune local `batch/<id>` branches and refs.
   `qualify` and `stage` runs never reach `merge_started` and have no
   members; their `borrowed` entries are untouched.
3. **Set: forward.** In order:
   a. **KW first** (S-M2, O-C2): if KW (R10.6 step 3) holds, write
      `known-good.json` now, before any remote call or version check. It is
      local and proven by the promotion.
   b. Resolve a pending intent by looking up its `after` state (git refs,
      GitHub, `batch.json`, `slot-state.json`); a merge intent through
      R10.4a. A failed lookup exits 75.
   c. The current `runner_build`, `runner_tools` and harness version must
      equal those recorded at claim (the harness runs `flash --promote`);
      otherwise HALT ("tools changed mid-merge"). Skipped once promoted.
   d. Continue R10.4-R10.8 from the first step not done. **Before
      promotion**, R10.4a, CS and every R10.4 check run again. **After
      promotion** (`known_good.batch` = this batch) (minor), the pair check
      and CS are skipped: the promotion consumed the pair and its proof
      covered every merge and tag; only R10.6 step 3, R10.7 and R10.8 remain.
   The runner never runs `flash --rollback` or `--recover` for this batch,
   including after `batch resume`. A forward path that cannot finish ends at
   Zak's `batch settle` (R13.4).

R11.3. **Within a tick** the same code runs: a failure before
`merge_started` ends in step 2 with its class, after it in step 3. No batch
is ever resumed inside a phase; a batch killed before `merge_started` is
rolled back and aborted, and its members run in a later batch.

R11.4. **Lease without `running`:** R7.8.

R11.5. **Runner errors.** An exception exits 1 after committing
`last_error`; the next tick applies R11.2. A second error at the same step
is a HALT.

R11.6. **Transport errors and unknown outcomes** (E15). Every GitHub or
remote git call ends in one of four ways:
- **success**;
- **auth refusal** (O-C3): HTTP 401, or 403 without rate-limit evidence
  (`x-ratelimit-remaining: 0`, `retry-after`, or a "rate limit" message);
- **confirmed refusal**: GitHub answered with another HTTP 4xx other than
  429, or git reported a rejected ref from the remote;
- **unknown**: timeout, connection error, 5xx, 429, 403 with rate-limit
  evidence, a malformed answer to a read, or a `gh`/`git` failure with no
  server answer.

An unknown read exits 75 (through the end-of-tick pass, R8.1). An unknown
write is reconciled against its intent's `after` state by a fresh read (a
merge through R10.4a): done → record it and continue; not done → exit 75;
the read fails → exit 75. Before `merge_started` that exit is a `transport`
abort (R9.1), after it the next tick resumes forward. An unknown outcome
never consumes a void, a strike other than `transport_prs`, or an approval;
only a confirmed refusal can HALT (R10.2 step 1, R10.4, R10.4a, R10.6 step
1). An auth refusal never HALTs: it pauses ("GitHub auth: <repo>:
<status>"); before `merge_started` the batch is first aborted as
`transport`, after it the forward path resumes after `batch resume`.

**Bounded transport aborts** (O-C3): a `transport` abort of a batch that
reached phase `prs-open` adds one to `strikes.transport_prs`; the second in
a row pauses ("GitHub failed twice after batch PRs opened"), so a flaky
GitHub cannot cost a build and a flash every tick. A batch reaching
`merge_started`, and `batch resume`, reset it.

## 12. Every step that changes something

Columns: what the step changes; what is committed first; the state if it
fails; what the next tick does if the process dies during or right after it.
"Abort" is R11.2 step 2. "Entry point" is R13.2. Steps 3 onward run under
both locks.

| # | Step | Changes | Committed first | If it fails | If the process dies |
| --- | --- | --- | --- | --- | --- |
| 1 | `enqueue`, `withdraw`, `requeue`, `tried`, `migrate` | state file; approval bundle (R5.1) | the bundle, complete and renamed into place (GitHub read before) | nothing written to the state file; transport: 75 | old or new file, never half; an orphan bundle is harmless and reused by digest; `migrate` finishes its rename |
| 2 | Fast-forward manifest (non-XML) or forks (preflight) | tree, `river-22.2` only | — | `--ff-only` changes nothing | next preflight finishes it |
| 3 | `flash --recover`, no pair (R13.2 steps 1 and 5) | phone (fb_mode, fastboot), `known-good.json` (checkpoint) | — (derived from disk and the phone; the checkpoint before anything else) | 3: HALT; 1: strike, second pauses | entry point derives it again |
| 4 | `phone-qa journal --restore` (H10) | phone and host settings | — (derived: journal not empty, no pair, KB) | 3: HALT; 1: strike, second pauses | entry point derives it again |
| 5 | Compose | local refs | — | void | refs overwritten at the same ID |
| 6 | Claim | state file | — (one transaction, `rev` re-checked) | nothing written | abort (nothing else to undo) |
| 7 | Checkout, LFS, TC | tree | `lease`, phase `checking-out` | R7.8, pause | abort → R7.8 |
| 8 | Image check, intermediate clearing, build, rebuild | `obj/KERNEL_OBJ`, `obj/DTBO_OBJ` (and `DTB_OBJ`, `RECOVERY_KERNEL_OBJ`), `out-stripped`, `river-build.json` | phase `building` | R7.5, R7.6; abort | abort; every build clears again |
| 9 | Stage | `batches/<id>/` | phase `staging` | void; abort | abort; the ID is never reused, the directory stays as evidence |
| 10 | Restore | tree | phase `restoring` | R7.8 | R7.8 |
| 11 | Flash | phone, `slot-state.json`, `batch.json`, `flash.json`, `wipes.jsonl` | phase `flashing` (after asserting no `flash_attempt`) | R8.6: 0 adopt; 4 verify rescue, `fail`; 1 with no attempt: void; else HALT | abort: rescue evidence first (R11.2 2a: a completed rescue is a `fail`), then `flash --rollback` if the pair is this batch's |
| 12 | Bench | phone, `device.csv`, `batch.json`, maybe `slot-state.json`, `wipes.jsonl` (rescue, attempt ID from H17) | phase `benching` | 4: R8.6, `fail`; 1: void; 3: HALT | as 11 |
| 13 | Setup on the candidate, readback | phone, `setup_wipe_seq` (with phase 9) | phase `setting-up` | `fail` | as 11 |
| 14 | QA | phone, `qa/runs/`, QA journal | phase `testing` | R9.1 | as 11; then the entry point restores the journal |
| 15 | Rollback | phone, `slot-state.json`, `wipes.jsonl` | phase `rolling-back` | 3: HALT | R11.2 2b runs it again if the pair is still this batch's |
| 16 | Setup on known-good, readback | phone, `setup_wipe_seq` | — (derived: no pair, KB, wipe seq, wizard flags) | strike; second pauses | end-of-tick or paused pass derives it again |
| 17 | Push batch branches | GitHub `batch/*` | phase `prs-open` and its `cleanup` item | unknown: `transport` abort, `transport_prs` strike, 75; auth: abort, pause; other SHA confirmed: HALT | abort; `cleanup` removes them |
| 18 | Open batch PRs | GitHub PRs | intent `open-prs` | as 17; found closed or at another head, or `CONFLICTING`: void; `UNKNOWN` after 30 s: `transport` | `cleanup` finds them by head branch |
| 19 | `prs`, `no_pr`, `tag`, `tag_repos` | `batch.json` | intent `record-prs` | void; abort | abort |
| 20 | CS, `flash --merge-started` | `batch.json` | intent `merge-started` | void; abort | `batch.json` decides: set → forward, unset → abort |
| 21 | Merge each PR | GitHub `river-22.2` | intent `merge <path>` | mismatch (R10.4a), CS or check false, or confirmed refusal: HALT; unknown or observations that disagree: 75; auth: pause | forward: R10.4a reconciles PR, tip, parents and tree, then CS and all checks again, then record or merge |
| 22 | Landed check (CS) | — | phase `landed` | HALT, before any tag | redo (read-only) |
| 23 | Push and verify tags | GitHub tags | intent `tag` | tag elsewhere: HALT; unknown: 75 | redo; a tag at the intended commit is accepted |
| 24 | `flash --promote` | phone, `slot-state.json` | intent `promote` | 75: retry next tick; 1 with H11's lookup line: auth pause, else `lookup` strike, second pauses; 3 or other 1: HALT | skipped if already promoted |
| 25 | `known-good.json` | known-good record | intent `known-good` | retry next tick | derived (KW, R10.6 step 3); the first thing the forward path does, before any remote call (R11.2 3a) |
| 26 | Change PRs, remote branches, local fast-forward | GitHub, tree (`river-22.2` only) | intent `cleanup` | unknown: 75; head moved across a close: reopen, `followup` | redo; comments deduplicated by marker; branch rules of R13.3 |
| 27 | Finish | state file | — (one transaction) | — | redo the finish |
| 28 | Report, prune | `reports/`, local refs | `reports_pending` in the finish transaction | retried at every tick start and end | as "if it fails" |
| 29 | `cleanup` item | GitHub PRs (`pr_repos`), branches (`branch_repos`) | the item | unknown: retry; merged batch PR: pause; branch not at the composed SHA: left, pause; same confirmed refusal for 3 ticks: pause; auth: pause | item stays |
| 30 | `batch settle` (Zak) | state file, `cleanup`, `rebaseline`, maybe `known-good.json` | — (GitHub read before; `known-good.json` first, then one transaction) | nothing written; transport: 75 | `known-good.json` skipped if written; old or new state file |
| 31 | `batch cleanup drop` (Zak) | state file | — (one transaction) | nothing written | old or new file |

## 13. HALT, pause, recovery and settle

R13.1. **HALT** (exit 3): an unsafe or unknown state. Commit `halt`, write the
report, exit. Every later tick exits 3 at once until Zak runs
`batch resume --zak`, which only clears the flag; the next tick starts with
R11.2.

R13.2. **The recovery entry point**, shared by `tick` and `qualify`, under
both locks (`stage` runs only steps 2 and 3 for its own runs, R14.1). Every
obligation is **derived from disk and the phone each time**, never from a
flag the runner may have failed to write. Steps 1-7 are local and never wait
for GitHub (E11):
1. **Checkpoint first** (O-C2): no pair and `slots.checkpoint_pending()`:
   `flash --recover` (strike `recover`).
2. `running` set: R11.2 (forward begins with KW, R11.2 step 3a). Only
   `lease` set: R7.8.
3. **Known-good agreement** (O-C2): with `running` now null,
   `slot-state.json` `known_good.batch` ≠ `known-good.json` `latest.batch`:
   pause ("known-good records disagree: slot-state names <a>,
   known-good.json names <b>"). The runner never guesses which is right.
4. **Ownership** (O-C1): a pair in `slot-state.json` that is not
   `running`'s. Nothing is done to the phone, and steps 5-7 are skipped.
   - kind `batch`: pause ("slot pair of <batch> is not the runner's");
   - any other kind (`calibration`, `baseline`, `experiment`: a person's
     hand run): exit 0 "blocked: phone in use by <batch> (<kind>)", no pause;
     the tester notifies after 24 h (R16.1).
5. No pair and (`fbmode.pending()` or the phone in fastboot):
   `flash --recover`.
6. **Journal** (minor): `$BENCH_HOME/qa/journal.json` not empty, no pair, and
   KB holds: `phone-qa journal --restore` (H10). Gated like step 7, so it
   never runs on a candidate or a dev build. It is also harmless after a
   wipe: `journal.plan_recovery` drops phone entries older than the current
   wipe seq. With KB failing, the journal waits and preflight is blocked.
7. **Setup**, end-of-tick pass and paused pass only (a following batch wipes
   the phone anyway). Owed when there is no pair, KB holds (§1), and either
   `journal.current_wipe_seq()` (`wipes.jsonl`) is greater than
   `setup_wipe_seq` (null counts as 0; a rollback, a rescue or any other wipe
   since the last verified setup) or `user_setup_complete` or
   `device_provisioned` is not 1. Then `phone-setup --wifi …` (H16), then
   the **readback**, all with non-elevating reads: both wizard flags are 1;
   Wi-Fi is connected to the configured SSID; the phone's `wlan0` address is
   `qa/fixtures.json` `server.phone_wifi_addr` (the address `radios.wifi`
   tests); and every value of `features.SETUP_SETTINGS` (H18: the
   `final.settings` values phone-qa compares: navigation mode, location,
   extra dim, touch sounds, lock screen disabled) reads as expected. Only
   then `setup_wipe_seq` = the current wipe seq.
8. `cleanup` items (R13.3), last. A transport error there is logged and
   retried next tick; it never skips steps 1-7.

Every phone read is checked as in KB (§1): no answer on USB pauses ("phone
not answering on USB: hold the power button"); unauthorized, recovery or
fastboot (outside step 5) is blocked; only a `GuardError` is a HALT. Each of
steps 1 and 5 (`recover`), 6 (`journal`) and 7 (`setup`) keeps a strike
count in `strikes`: a success resets it, a failure (exit 1 or a failed
readback) adds one, the second consecutive failure pauses. Exit 3 is a HALT.

R13.3. **Pause** (exit 0): a problem the batch did not cause: two voids in a
row, `tree-disturbed`, a lease mismatch, a foreign `batch` pair, known-good
records that disagree, manifest pins changed, a camera mismatch, a base
camera set that differs from known-good, the RAM reference or a re-baseline
owed (R8.2 step 8), a gate failure, a build image change, desk suspect, a
second strike (`recover`, `journal`, `setup`, `transport_prs`, `lookup`),
GitHub auth, a `cleanup` problem. The runner finishes the abort first when
it can. While paused, a tick runs only the paused pass (R8.1 step 3).
`batch resume --zak` clears it; a condition that still holds pauses again.

**`cleanup` item** `{batch, branch, branch_repos: {path: composed sha},
pr_repos: {path: {repo, number}}}`, at most one per batch (minor: branches
and PRs are listed separately, so a partial settle can drop one without the
other). For each of `branch_repos`: `git ls-remote zrm625
refs/heads/batch/<id>` first; an absent branch is already deleted. A branch at the composed SHA is deleted with
`git push --force-with-lease=refs/heads/batch/<id>:<composed sha> zrm625
:refs/heads/batch/<id>`, so only the composed SHA is ever deleted; at another
SHA it is left and the runner pauses. Each open PR of `pr_repos` is closed
with one marked comment. A batch PR found merged pauses ("batch PR merged without the
runner"). The item is dropped when every branch is absent and every PR closed.
`batch cleanup drop <id> --zak` deletes an item Zak has resolved by hand.

R13.4. **`batch settle <id> --zak`** (E8), for a HALT after `merge_started`
that cannot go forward. Under both locks (busy: 75). It requires `halt` set,
`running.id` = `<id>`, `merge_started` set, and no pair for `<id>` in
`slot-state.json` (Zak has already resolved it, with `flash --rollback --zak`
or `flash --promote`). It reads GitHub first (transport: 75, nothing written):
each frozen PR's state and merge commit, `river-22.2` of every repo, the
`known-good-<id>` tags present, and eligibility (R5.4) for every member with
nothing landed. Then:
- **Promoted** (`slot-state.json` `known_good.batch` = `<id>`; promotion
  proved every PR merged and every tag in place): write `known-good.json`
  as R10.6 step 3 if `latest.batch` ≠ `<id>`; every member becomes `merged`.
- **Not promoted:** `known-good.json` is unchanged. A member whose approved heads are
  all ancestors of `zrm625/river-22.2` becomes `merged` with `followup`
  "settled in <id>: untagged"; a member with nothing landed takes its
  eligibility outcome (`queued` at its position, flags kept, or its other
  status); a partly landed member becomes `needs-reapproval` with what landed.
  Pushed tags are reported, never deleted by the runner.
- **Re-baseline owed** (S-M2): if not promoted and anything landed (fully or
  partly, whatever the reason the forward path stopped, a tools-version
  mismatch included), `river-22.2` now holds content no gate tested as a
  whole. The same transaction sets `rebaseline` = `{settle: <id>, at}`, and
  every tick pauses (R8.2 step 8) until the **re-baseline sequence** is done:
  `batch stage --kind baseline --id <new>` (R14.1) on `river-22.2`; flash,
  3-boot bench and `phone-qa --baseline` (README step 3), which keeps the
  historical RAM reference (H14) and writes a new `calibration_seed`; harness
  requalification from README step 4 to step 8; runner part 2 with zero
  members (`batch qualify --run-only`); then `batch resume --zak`. If nothing
  landed, `river-22.2` is still the known-good base and nothing is owed.
- One transaction: the above, the batch's `cleanup` item **replaced** by one
  whose `branch_repos` are only the branches still present and whose
  `pr_repos` are only the batch PRs still open, `running` and `halt`
  cleared, the report added to `reports_pending`. KW for a promoted batch is
  written before this transaction (R10.6 step 3).

The report says which changes landed fully, partly or not at all, which repos
have the tag, whether the phone was promoted, whether `known-good.json` was
written, and whether a re-baseline is owed.

## 14. Qualification and the first batch

R14.1. **Harness, and `batch stage`.** `phone-qa qualified` (H3) exits 0,
following README "Order of qualification". Its calibration batch, and a
baseline build (R13.4, §18 Z7, Z8), are built with
`batch stage --kind calibration|baseline --id ID [--change NAME...]`:
- **Refusals:** R2.4. Under both locks (busy: 75).
- **No phone work at all** (minor), recovery included: of the recovery entry
  point it runs only R13.2 steps 2 and 3 for an interrupted run R2.4 allows
  it to abort (host only: R11.2 step 2 with no pair, R7.8). A pair, an
  fb_mode or checkpoint obligation, a journal or a setup owed is reported
  and left to `tick` or `qualify`; none of them blocks staging.
- **Its own image pin** (G-C3): at its start it runs `ensure_image`, then
  `podman image inspect --format '{{.Id}}'`, and pins that ID in
  `running.versions`; R7.5's before and after checks compare with this pin,
  never with `qualified.json` (which does not exist during bootstrap).
- **Base:** forks and manifest as R8.2 steps 2 and 3 and TC (step 4).
  `calibration` also needs the baseline-XML check and, with zero members, a
  verified base (§1). `baseline` skips the baseline-XML check (it makes the
  new baseline), refuses `--change`, and builds `river-22.2` as it is.
- **Members:** the named `queued` entries are checked as in R7.2 (read-only;
  a failure stops with its reason) and composed (R7.3) as `running.borrowed`.
  After bootstrap, `calibration` with no `--change` stages the current
  verified base with zero members (harness requalification on an empty
  queue).
- Then `running.purpose` `calibration` or `baseline`, the lease, R7.4-R7.8
  (intermediate clearing included) and `phone-qa stage <id> --kind <kind>`.
  It claims nothing and changes no entry. A crash is aborted and restored by
  R11.2 like any batch.

R14.2. **Runner part 1: A→B→A builds**, on the host, with the runner's own
compose, TC, image check, intermediate clearing, build and restore. Each B is
local, never-pushed commits on `batch/abtest-<n>`, deleted afterwards. After
each build the runner hashes every file under
`out-stripped/target/product/river/system/` and `.../vendor/`, masking the
`build.prop` lines in `qa/lib/riverbatch/abtest_masks.json` (build date and
UTC date, fingerprint, incremental, display ID, description,
`ro.river.build_id`, `ro.vendor.river.build_id`, Lineage version strings) and
skipping the files in `abtest_allowlist.json`, each with the reason it differs
between identical builds. It also records the SHA-256 of the product
`dtbo.img` (the one `phone-qa stage` copies) and of every
`DTBO_OBJ/arch/arm64/boot/dts/qcom/sdm632-river-*-overlay.dtbo`, and
`stage.boot_info(boot.img)` (`header_version`, `cmdline`, `dtb_sha256`,
`ikconfig_sha256`). Both lists are `runner_build` inputs.
- **A** = the base. **B1:** a same-size edit to a file installed verbatim
  into `/vendor` and a new `PRODUCT_COPY_FILES` file in
  `device/motorola/river`, plus a same-length change to a string literal in a
  built-in driver in `kernel/motorola/sdm632`; then **A1**. **B2:** one
  property value in `arch/arm64/boot/dts/qcom/sdm632-river-common-overlay.dtsi`,
  which every river overlay in `dtbo-$(CONFIG_RIVER_DTB)` includes; then
  **A2**. **B3:** one defconfig option that IKCONFIG records; then **A3**.
  **B4:** B1's edits again, interrupted (S-M4): the runner polls every
  second for the object file of B1's edited driver under `KERNEL_OBJ` (its
  path is in the part-1 config), and SIGTERMs the holder only once that file
  exists, is newer than the build's start and contains B1's string, so the
  interruption provably follows a B-affected intermediate. The build ending
  first fails part 1 (inconclusive). After the SIGTERM the runner waits
  until the holder has exited, its child record is gone and no
  `lineageos-build` container is running (R6.2), then restores; then
  **A4**. Every `batch/abtest-<n>` branch is deleted after part 1, pass or
  fail, and any left over is deleted before it starts (minor).
- **Kernel strings** (S-M4) are read from the kernel **extracted from each
  staged `boot.img`** (the image `phone-qa stage` copies: the header's kernel
  section, gunzipped, with the appended DTBs cut off), never from
  `KERNEL_OBJ` or `boot_info` alone.
- **Passes** only if each of A1-A4 equals A: file for file after masks and
  allowlist, the same `dtbo.img` and overlay hashes and the same `boot_info`;
  the A→B1 diff contains exactly the edited and the new file; B1's (and
  B4's, if it finished) extracted kernel has the new string and not the
  old, and **A1's and A4's extracted kernels have the old string and not the
  new**; **B2's river
  overlay hashes and `dtbo.img` hash differ from A's, and A2's equal A's**
  (the edit demonstrably feeds `dtbo.img`, and its revert comes back out);
  `ikconfig_sha256` of B3 differs from A's. Nothing is asserted about the DTB
  for B3. A failure means the runner is not qualifiable; it is a design bug,
  not a setting.

R14.3. **Runner part 2: one full run** (E2). `batch qualify` (or
`--run-only`) refuses as R2.4, runs the full recovery entry point (it may do
phone recovery), and requires the manifest XML check (R8.2 step 2), a verified base
and KB, and composes the `--change` entries (none by default) as
`running.borrowed`: `qNNNN` with `running.purpose` `qualify`, staged as
`--kind batch`, not an experiment. Named entries must be `queued` and pass the
R7.2 checks read-only; qualification never changes, claims, pushes or merges
an entry. It runs R8.3 phases 1-9 and 11 and passes only if every tool exits
0, `phone-qa` exits 0 with `eligible: true`, `halted` null and a bound result,
the rollback exits 0, the end-of-tick setup and its readback pass, TC passes
after restore, and the state file ends with no `running` or `lease` and
nothing derived pending (R13.2). A failure says which criterion failed
and exits 1 (`"qualified": false`); a HALT anywhere in it (the rollback,
the setup) is committed and exits 3, never reported as a failed criterion
(Gemini GM2-1). Like the tick (R8.2 step 2), `stage` and `qualify`
re-exec once after fast-forwarding `.repo/manifests`, so they never run
or certify old code (Gemini GM2-2).
- **The first qualification** uses `--change gnss-standalone --change
  perch-launcher`: the known-good base is the baseline, which has no Perch, so
  zero members cannot pass `phone-qa`.
- After the first tag, and after a re-baseline (whose build already contains
  everything merged), part 2 runs with zero members.

R14.4. **Pin and record.** `batch qualify` resolves the build image ID once
at its start (`ensure_image`, then `podman image inspect`) and pins it in
`running.versions` of every part; every build checks it before (R7.5) and
after (`river-build.json` `image_id`). Part 1's result is kept in
`runner/abtest.json` with its `runner_build` and image ID, and reused while
both match. `runner/qualified.json` = `{runner_build, runner_tools,
harness_version, image_id, abtest, run}` is written only after every required
part has passed under one pin. `tick` needs all of them current. A new
`runner_build` or image ID needs both parts; a new `runner_tools` or harness
version alone needs part 2 (`--run-only`).

R14.5. **First batch** `b0001` = `gnss-standalone` (sdm632-common #3) +
`perch-launcher` (river #6, vendor/lineage #5): 3 batch PRs; `no_pr` for 6
forks and the manifest. Its base was verified on 2026-11-23: every fork's
`river-22.2` tree equals the tree of `latest.commit_set` (river moved only by
the merge `0691ea0`, tree-equal to `9851f11`), and `default.xml` is
byte-identical between `1faf721` and `origin/river-22.2` (`74d2e72`). The RAM
reference is still the seed, so 3 boots are measured. The calibration batch
and the first part 2 run have b0001's content, so b0001 is a weak test of the
gate. Prerequisites: section 17 merged (by Zak until PR #11), harness and
runner qualified, forks on `river-22.2`, `batch migrate` done.

## 15. Reports and retention

R15.1. After every batch, HALT, pause and settle:
`$BENCH_HOME/runner/reports/bNNNN.html` and `reports/index.html`,
self-contained, no secrets. The transaction that ends the event adds the
report to `reports_pending`; it is written after the transaction and removed
from the list once written, at the start and end of every tick (halted or
paused too), and `batch report` can always regenerate it. In order:
**waiting on Zak** first, even if empty (HALT advice, pause reason,
`waiting-zak` entries with reasons, `failed` entries older than 24 h, "not
qualified", blockers with their paths, foreign pair or "phone in use by",
"base unverified" when it is, a re-baseline owed); the outcome in one line; changes with PR links and status; for a failure,
the failing checks with a link to `phone-qa`'s report, the previous failing
batch's checks, and the tester's decision once made; boot, RAM and size
against known-good; the phone's known-good slot, tag and any pair; the queue
(next up, solo, `failed`, `tried`, `needs-reapproval`).

R15.2. **Retention**, at finish: delete `batches/<id>/images/` older than 7
days, except batches named by `known-good.json` `latest`, `slot-state.json`,
`running` or a `failed` entry's newest evidence. Keep `batch.json`,
`results.json` and all reports; build logs 30 days.

## 16. The tester thread

R16.1. A scheduled T3 task every 2 hours runs `batch tick` in the background,
waits, and reads the JSON line. A batch ran or the waiting list changed: show
the newest report and notify Zak once. 75, idle, blocked or not qualified:
silent unless the same state lasts over 24 h, then notify once. Exit 3:
notify with the HALT advice. "Blocked: phone in use by <id> (<kind>)"
(R13.2 step 4) is one of the silent states: Zak hears of it after 24 h.

R16.2. **Failed entries.** For each `failed` entry the tester reads the
evidence, the QA report, the build or flash logs, and the failing checks of
the previous failing batch (shown in the report), and runs `batch requeue` or
`batch tried` with a reason of at least one sentence that names the evidence
(check IDs, log lines, earlier runs). The same check failing for an unrelated
change just before points to the desk, not the change. The runner applies
the same rule to solo runs (minor): if solo A fails and then solo B (a
different change) fails a shared check, B's failure is not recorded and the
runner pauses as desk suspect (R9.5); A stays `failed` with its evidence, so
after Zak checks the desk and resumes, the tester weighs A's decision with
that in mind (a desk fault is a reason to `requeue` A). It does not decide
while a desk-suspect pause is set. It may do this in the same task run as the
tick.

R16.3. It never edits code; never runs `adb`, `fastboot`, `flash`,
`phone-qa`, `qualify` or any command with `--zak`; never merges, comments on
or closes PRs.

## 17. Changes in other tools

All land in **one PR before qualification**, through council code review;
until PR #11 is merged, Zak merges it (R5.8). H2, H3, H6, H10, H11, H12, H13,
H14, H15, H17 and H18 change harness-version inputs, so the harness is
qualified after this PR, in README order.

| ID | File(s) | Change | Harness version |
| --- | --- | --- | --- |
| H1 | `scripts/common.sh`, `scripts/bench/lib.sh` (`ensure_image`, `run_build`), `scripts/build` | Move `acquire_build_lock` to `common.sh` (so `change`, `merge-upstream` and `setup-forks` use it). It opens `.lock` with `exec 9<>`, takes `flock -n`, then runs the H15 check (S-M5), and only then writes the token. With `BENCH_BUILD_LOCK_TOKEN` set, it opens no fd and reuses the lock only if the file holds that token and the lock is held. Right after taking the lock, `scripts/build` deletes `river-build.json` and `.bench-result`. `ensure_image` resolves the tag to an image ID once (`podman image inspect --format '{{.Id}}'`); `run_build` runs that ID; `river-build.json` gains `image_id`. | no (`runner_build`) |
| H2 | `qa/lib/riverqa/lock.py` (`build_running`, `_refuse_during_build`), `scripts/common.sh` (`device_lock`) | A build lock whose file token equals `BENCH_BUILD_LOCK_TOKEN` and which is held counts as "no build". `device_lock` (used by `phone-setup`) gets the same build-lock probe and the H15 check. | yes |
| H3 | `qa/lib/riverqa/qa.py`, `scripts/phone-qa` | `phone-qa qualified`, host-only: `thresholds` `calibrated`, `calibration_runs(version) >= 5`, `controls_status(version)`, `known-good.json` with `latest`, `ram_reference`, `calibration_seed`, `slot-state.json` `baseline_closed`, `slots.checkpoint_pending()` None. One JSON line `{"qualified", "harness_version", "missing"}`; exit 0 only if qualified. | yes |
| H4 | `scripts/change`, `scripts/merge-upstream`, `scripts/setup-forks` | `change start\|pr\|merge\|tag`, `merge-upstream` and `setup-forks` take the build lock once at the start (H1) for the whole run, around every `repo forall` (busy: exit 1, "a build or batch is running"); so `change pr` never pushes while a batch runs (S-M9); refuse while the state file has `running` or `lease`, or exists and does not parse. `change merge NAME --record FILE` (E7): the record is a valid unanimous `tooling` record (R5.2); every PR it names is in `zrm625/android_manifest` with base `river-22.2` and `isCrossRepository` false; the PRs are found on GitHub by head branch, whether or not a local branch exists, and must be exactly the record's; the path allowlist (`qa/`, `scripts/`, `workspace/`, `*.md` other than `CHARTER.md`) is checked on `git diff --no-renames --name-only -z <merge-base> <reviewed head>` at the reviewed head, so a rename lists both its old and its new path and both must pass (S-B3: moving `CHARTER.md` or `default.xml` into `qa/` is refused); then the synchronous REST merge at the reviewed head (`PUT repos/zrm625/android_manifest/pulls/<n>/merge` with `sha`; never a merge queue or auto-merge). Any fork PR, manifest XML or other path is refused with the offending paths. Without `--record`, `change merge` refuses in every repo (G-C2), so it merges nothing but a council-reviewed tooling-only manifest PR. `merge-upstream` loses `--push`, refuses on `river-22.2`, and merges into the topic branch from `change start upstream-YYYYMM`, which goes through `change pr`, the council and the queue. | no (`runner_tools`) |
| H6 | `qa/lib/riverqa/qa.py` (`finish`) | Last stdout line: `{"run_id", "results", "exit_code", "eligible", "halted"}`. | yes |
| H7 | `.repo/manifests/AGENTS.md` (`CLAUDE.md` links to it), `workspace/AGENTS.md` | Image changes end at `batch enqueue`; `change merge` always needs `--record` and is for council-reviewed tooling PRs only, and only after PR #11; `change pr` waits (exit 1) while a build or batch holds the build lock; never push `batch/*`; while `batch status` shows a batch or a lease, never `repo sync`, check out in a fork, or merge into `river-22.2` (CLI or web); return forks to `river-22.2`; **when dev work on the phone ends, return it to known-good with `flash --rollback`** (E3); agents never pass `--zak`; the tester thread decides `failed` entries with `requeue` or `tried` and a reason; upstream merges via `change start upstream-YYYYMM` and the queue; never `podman image prune` the `lineageos-bench` images. | no |
| H9 | `qa/README.md` "Interfaces for the batch runner" | The runner in `qa/lib/riverbatch/`; the H6 line; `qualified`; `journal --restore`; `flash --recover` only with no pair; `--promote` exit 75 (lookup transport) and exit 1 with the lookup line (refused or malformed, H11); `device-bench` rescue wipes carry the flash attempt ID (H17); `features.SETUP_SETTINGS` (H18); the refusals of H12 and H15; failures are decided by the tester thread, not the harness. | no |
| H10 | `qa/lib/riverqa/qa.py`, `restore.py` | `phone-qa journal --restore`: under the device lock, only the preflight's journal recovery (`recover_journal`, fb_mode included), with `dev.guard()` before any `dev.root()`. Exit 0 if the journal is then empty; 3 on a guard failure, a halting failure, or an fb_mode obligation still pending; 1 otherwise. | yes |
| H11 | `qa/lib/riverqa/recovery.py` (`lookup`), `flashing.py` (`promote_batch`, `recover`) | **Typed lookup** (S-M7): `lookup` returns `(outcome, data, evidence)` instead of `dict \| None`. `transport`: timeout, `OSError`, no server answer, 5xx, 429, or 403 with rate-limit evidence. `refused`: `gh` or `git` reports HTTP 401, 403 without rate-limit evidence, 404 or 422 (`evidence` keeps the status and the first error line). `malformed`: unparsable JSON, a missing field, or a non-SHA tree (the existing `SHA_RE` check). `ok`: the dict of today. `promote_batch`, before touching the phone: `transport` exits 75; `refused` or `malformed` prints a last stdout line `{"lookup": "refused"\|"malformed", "repo", "evidence"}` and exits 1; exit 3 stays for a proof that is not PROMOTE (mismatch) and for phone failures. `recover` treats any non-`ok` outcome as today's failed lookup. Unit tests pin each class. | yes |
| H12 | `qa/lib/riverqa/flashing.py` (`main`) | `flash --rollback` and `flash --recover` refuse (exit 1) while `slot-state.json` holds a pair whose batch is `running.id` in `$BENCH_HOME/runner/state.json` (read under its lock shared; unparsable: refuse), unless `RIVER_BATCH_ID` equals that batch and the `DeviceLock` was reused through a matching `BENCH_LOCK_TOKEN` (`not lock.owned`: the runner's holder holds it), or `--zak` is given. | yes |
| H13 | `qa/lib/riverqa/stage.py` | Split `camera_set` into pure `parse_camera_set(text: str \| None) -> list[int]` (None: the bootstrap set) and the file read; same rule. | yes |
| H14 | `qa/lib/riverqa/regression.py` (`baseline_record`), `qa.py` (`baseline_close`) | `baseline_close` passes the existing `known-good.json`, if any; `baseline_record` then keeps its `ram_reference` (value, tag, batch, run IDs) when its median is lower than the new baseline's, with `seed` cleared, so a re-baseline never raises the historical minimum. The new `calibration_seed` is written as before. Unit tests pin both cases. | yes |
| H15 | `qa/lib/riverqa/children.py` (new, standalone stdlib, also runnable as `python3 -I …/children.py`), `lock.py` (`DeviceLock.acquire`), `scripts/common.sh` (`acquire_build_lock`, `device_lock`) | `live_children()` reads the files in `$BENCH_HOME/runner/children/` whose names match `^[0-9]+\.json$` (never the holder's `.<pid>.tmp` files) with the R6.2 rule (current host boot, matching start time or a live pgid); unreadable files count as live. Every lock acquisition, and so `scripts/build`, `flash`, `device-bench`, `phone-qa`, `phone-setup`, `change`, `merge-upstream` and `setup-forks`, refuses (exit 1, naming the record) while a live record's pgid differs from the caller's session ID. **The check runs after `flock` succeeds and before the token is written** (S-M5; R6.3), in `DeviceLock.acquire`, `acquire_build_lock` and `device_lock` alike; on a live orphan the lock is released and nothing is written. The runner's R6.2 check uses the same function, again after each lock. | yes |
| H16 | `scripts/phone-setup` | **Requested SSID** (G-C5): with `--wifi`, Wi-Fi counts as set up only if `cmd wifi status` says `Wifi is connected to "<SSID>"` for the SSID in the file; connected to any other network, it connects to the requested one. The final report prints the connected SSID. | no (`runner_tools`) |
| H17 | `qa/lib/riverqa/bench.py` (`bench.run`) | **Rescue attempt ID** (G-C6): the bench-boot failure path calls `flashing.rescue(dev, st, reason, attempt["attempt_id"])`, so the rescue's `wipes.jsonl` record carries the flash attempt ID, as `flash`'s rescue already does. A unit test pins it. | yes |
| H18 | `qa/lib/riverqa/features.py` | **Setup settings** (minor): the `want_s` dict of `final.settings` becomes the module constant `SETUP_SETTINGS` (same keys, values and read commands), used by `final.settings` and by the runner's setup readback (R13.2 step 7), so the two cannot drift. | yes |

H5 and H8 stay dropped (D1, S2). KB needs no harness change: its
non-elevating reads (§1) are runner code in `qa/lib/riverbatch/kb.py`, built
on `Device.state`, `in_fastboot`, `guard` and `sh_rc`.

## 18. Needs Zak

Z1-Z6 are decided as in revision 2 (in-tree branches; locks per batch;
camera changes by hand; tooling changes under council code review; the
"worker checkouts" wording; no "require up to date" protection).

1. **Merge charter PR #11.** It covers Z4 (tooling hand merge, which R5.8
   and H4 keep off until it merges), Z5 (one batch PR per repo), D1 (the
   tester decides failed singletons), Z8 (camera changes) and Z7:
   - **Z7, manifest pins move by hand until runner v2.** In PR #11's words:
     moving the pinned projects in `default.xml` is done by hand, with a
     council review and a new baseline. The sequence: council review of the
     `default.xml` PR; Zak merges it; `repo sync`; **the baseline build**,
     `batch stage --kind baseline --id <id>` (R14.1: intermediate clearing,
     TC, its own image pin, then `phone-qa stage <id> --kind baseline`);
     flash, 3-boot bench and `phone-qa --baseline` (README "Order of
     qualification" step 3, which writes a new `calibration_seed`); harness
     requalification **from README step 4 to step 8** (the baseline itself is
     step 3); runner part 2 (`batch qualify --run-only`, zero members); then
     `batch resume --zak`. From the merge until the new baseline, every tick
     pauses on R8.2 step 2. Upstream merges into the 9 forks go through the
     queue (H4).
2. **Z8, camera-set changes and the re-baseline they force.** The runner
   never lands a camera-set change (`waiting-zak`); Zak merges it in the web
   UI. Then every tick pauses ("river-22.2 camera set differs from
   known-good") until a new baseline updates `latest.cameras`. Costs:
   - With H14 the RAM reference keeps the historical minimum with its
     provenance. If the re-baselined build cannot meet it (R8.2 step 8), the
     runner pauses: raising the RAM reference is Zak's decision (a hand edit
     of `ram_reference`, or reverting what raised RAM).
   - The re-baseline is the Z7 sequence from the baseline build on
     (`batch stage --kind baseline`, README step 3). It writes a new
     `calibration_seed`, a harness-version input, so the harness is
     requalified from README step 4 to step 8, then runner part 2: several
     hours of phone time, with no batches meanwhile.
3. **`batch settle`** (R13.4) is Zak's only exit from a HALT after
   `merge_started` that cannot go forward. If the phone was not promoted,
   any pushed `known-good-bNNNN` tags stay; deleting them is Zak's call.
4. **Each desk-suspect pause needs `batch resume --zak`** after Zak checks
   the desk. So do the second-void, strike and cleanup pauses.
5. **After a change's third failure, only Zak puts it back in the queue**
   (R9.4), as PR #11 says. The count is kept by change name, across record
   replacements and withdraw plus re-enqueue, and every later return to
   `queued` from outside a batch needs `requeue --zak`; `tried` stays with
   the tester.
6. **Never prune the pinned build image.** `podman image prune` or a
   Containerfile change gives a new image ID; every tick then pauses ("build
   image changed") until both qualification parts run again (R14.4).
7. **A settle that leaves anything landed but unpromoted costs a re-baseline**
   (R13.4, S-M2): the Z7 sequence from the baseline build on, with the RAM
   reference kept (H14). Every tick pauses until it is done.
8. **Hand-run phone work blocks the runner, silently.** While a person's
   calibration, baseline or experiment pair is open, every tick exits
   "blocked: phone in use" (R13.2 step 4); Zak is notified after 24 h. A
   foreign `batch` pair still pauses.
9. **GitHub auth** (O-C3): a missing push permission or a 401/403 pauses
   until Zak fixes the token and resumes.

Hard stops are unchanged: they reach the runner only as `needs_zak` entries,
which it never runs.

## 19. How each finding is closed

Round 5 (`batch-runner-r5-findings.md`):

| Item | Closed by |
| --- | --- |
| O-C1 foreign non-batch pair blocks, batch pair pauses | R13.2 step 4; R16.1; R13.3; §18 item 8 |
| O-C2 KW conditions; checkpoint first; known-good agreement | R10.6 step 3 (KW); R11.2 step 3a; R13.2 steps 1 and 3; R13.3 |
| O-C3 push permission, auth pause, rate limit unknown, 2 transport aborts | R8.2 step 9; R11.6 (auth class, `transport_prs`); R4.1; R9.1; §12 rows 17, 18, 21, 29; §18 item 9 |
| G-C1 `qualify`/`stage` refusals | R2.4; R14.1; R14.3 |
| G-C2 `change merge` always needs `--record` | H4; R5.8; H7 |
| G-C3 = S-B1 `stage` image pin; zero-member calibration | R14.1 (own pin, zero members on a verified base) |
| G-C4 = S-M3 KB non-elevating reads | §1 KB (`kb.py`: state, fastboot, guard, getprop; only identity HALTs); R13.2; §17 note under H18 |
| G-C5 ≈ S-M1 transport → end-of-tick pass; paused pass; SSID | R8.1 steps 3, 5 and 6; R13.3; H16 |
| G-C6 ≈ S-M6 rescue evidence by attempt ID; reconcile before abort | R8.6 (rescue evidence); R11.2 step 2a; H17; §12 rows 11, 12 |
| S-B2 merge reconciliation | R10.4a; R10.4; R11.2 step 3b; R11.6; §12 row 21 |
| S-B3 renames in the H4 path check | H4 (`--no-renames`, both paths); R5.8 |
| S-B4 failure history by name; every return after 3 needs Zak | R4.1 `failure_history`; R4.2; R4.4; R5.5; R5.6; R9.4; §18 item 5 |
| S-M2 partial settle re-baseline; KW first | R13.4 (re-baseline owed, sequence); R8.2 step 8 (`rebaseline`); R11.2 step 3a; §18 item 7 |
| S-M4 kernel from staged `boot.img`; B4 after a B-affected object | R14.2 (B4 waits for the driver object; extracted kernels; A1/A4 checks) |
| S-M5 orphan check after each lock | R6.3; H1; H15 |
| S-M7 typed lookups; bounded refusals; `mergeable: UNKNOWN` inconclusive | H11; R10.6 step 2 (`lookup` strike); R10.2 step 3; R9.1; §12 rows 18, 24 |
| S-M8 immutable bundles by digest; replies cover the round's heads | R5.1; R5.2 item 5; R5.7; R4.2; §12 row 1 |
| S-M9 `change pr` vs finalization | R6.5; H4 (`change pr` takes the build lock); R10.7 (re-read after close, reopen); H7 |
| Minor: desk rule for solo A then solo B | R16.2 |
| Minor: rescue wipe by attempt ID and event seq | R8.6 |
| Minor: holder temp files | R6.2 step 2; H15 |
| Minor: batch PR closed or at another head → void | R10.2 step 2; §12 row 18 |
| Minor: readback covers phone-qa's settings | R13.2 step 7; H18 |
| Minor: journal restore gated or harmless | R13.2 step 6 (gated like setup, and harmless after a wipe) |
| Minor: cleanup separates branch and PR repos | R13.3 (`branch_repos`, `pr_repos`); R13.4 |
| Minor: enqueue/requeue read GitHub, no fetches | R5.4 (camera-set paragraph) |
| Minor: R11.2 applies a committed desk-suspect outcome | R11.2 step 2d |
| Minor: R11.2 step 3 skips pair/CS after promote | R11.2 step 3d |
| Minor: origin XML compared first | R8.2 step 2 |
| Minor: phase 8 sets `setup_wipe_seq` | R8.3 phase 8; §12 row 13 |
| Minor: non-empty `prs` | R5.2 item 0 |
| Minor: abtest waits for the container; abtest branches cleaned | R14.2 B4 |
| Minor: §18 item 5 | §18 item 5 |
| Minor: Z7/Z8 requalification from README step 4 | §18 items 1 and 2 |
| Minor: Z7 baseline build explicit | §18 item 1; R14.1 (`stage --kind baseline`) |
| Minor: may `stage` do phone recovery | R14.1 (no phone work, recovery included) |

Round 4 (`batch-runner-r4-findings.md`):

| Item | Closed by |
| --- | --- |
| E1 always clear kernel and DT intermediates; DTS test | R7.5 (list from `kernel.mk`/`BoardConfigKernel.mk`, every build incl. rebuild, qualify, calibration); R7.6; R14.2 B2; §12 row 8; provenance rule removed |
| E2 qualification bootstrap | R14.3 (`--change`, first run gnss + perch, then zero members); R14.4 (image pin, before/after checks, `qualified.json` only after all parts); R14.1 (calibration own entry point) |
| E3 preflight phone check | §1 KB (non-elevating reads only, never `running_identity`: I1, I2); R8.2 step 7; H7 (`flash --rollback` after dev work) |
| E4 manifest XML = baseline | §1 baseline XML; R8.2 step 2; R14.3; §18 Z7 (PR #11 sequence) |
| E5 runnable entries are fork-only | R5.4 step 3 (refused / `waiting-zak`, never ok); R5.5; R7.3 (manifest never composed); R10.2 step 4 |
| E6 full commit-set check before every merge | R10.4 step 1 (CS); R10.3; R10.5; R11.2 step 3 |
| E7 tooling merge pins reviewed head | H4 (`change merge --record`, allowlist at the head, `--match-head-commit`); R5.2 `kind: tooling`; R5.8 |
| E8 cleanup items; commit point | R13.3 (one item per batch, `ls-remote` first, absent = deleted); R13.4 (replace item); R2.3, §12 row 31 (`cleanup drop`); R10.6 (tags → promote → `known-good.json`, derived obligation); R11.2 step 3a |
| E9 `merge_started` read first; local abort | R11.2 steps 1-2 |
| E10 adoption by exit code | R8.6; R8.3 phases 6, 7, 11; R9.1; §12 rows 11-12 |
| E11 setup obligation from wipes; readback; GitHub down | R13.2 step 7 (`current_wipe_seq` vs `setup_wipe_seq`, readback); R13.2 steps 1-7 local, step 8 last; R8.1 steps 4-6; R4.1 |
| E12 base camera set first; RAM reference | R5.4 step 1; R7.2; R8.2 step 8; §18 item 2 |
| E13 orphan children block all tools; re-read halt/pause | H15; R6.5; R6.2; R8.1 steps 2, 3 and 5 |
| E14 third failure; desk rule | R9.4 (third failure limits returns to `queued`, not `tried`; as changed by S-B4); R9.5 (content keys differ, shared check, not a subset; batch runs only); R4.4 |
| E15 unknown outcomes; migration | R11.6; R9.1 `transport`; R10.4; §12 rows 17, 21; R5.7 (missing full-round reply → new full round) |
| Minor: D3 camera touch by merge-base diff | R5.4 (camera set paragraph) |
| Minor: tester compares with the previous batch | R16.2; R15.1 |
| Minor: settle returns, reports | R13.4 (nothing landed → eligibility; partly → `needs-reapproval`; tag, promotion, `known-good.json` reported) |
| Minor: R13.2 step 3 fastboot | R13.2 step 5 |
| Minor: image ID before the build | R7.5 image check; R7.6 |
| Minor: image prune | §18 item 6; H7 |
| Minor: R14.1 wording | R14.1 |
| Minor: desk-suspect resumes | §18 item 4 |
| Minor: stub EOF | R6.2 step 1 |
| Minor: dead vs running container | R6.2 |
| Minor: `qa/.ruff_cache/` | R7.4 item 3 (directory prefix) |
| Minor: top-level XML only | §1; R8.2 step 2 |
| Minor: stub file type hashed | R2.2 (`*.xml`); R7.4 item 1 |
| Minor: harness version before promote | R11.2 step 3 |
| Minor: H12 `not lock.owned` | H12 |
| Minor: `isCrossRepository` | R5.3; R10.4 step 3; H4 |
| Minor: checked reads | §1 KB; R13.2 |
| Minor: settle locks and report | R13.4; R6.1 |
| Minor: `len(failures) >= 3` | R9.4 (`len(failure_history[name]) >= 3`) |
| Minor: requeue on non-ok eligibility | R9.4 |
| Minor: `acquire_build_lock` in `common.sh` | H1 |
| Minor: H4 finds PRs on GitHub | H4 |
| Minor: H10 guard before root | H10 |
| Minor: rebuild clears again | R7.6 |
| Minor: sysrq wording | R8.4 |
| Minor: reports while halted or paused | R8.1 step 1; R15.1 |

Round 3 (`batch-runner-r3-findings.md`):

| Item | Closed by |
| --- | --- |
| D1 no automatic conviction | R9.4, R16.2 (`failed`, evidence, tester `requeue`/`tried` with reason); R4.4 |
| D2 desk suspect | R9.5 (as changed by E14) |
| D3 eligibility at every transition | R5.4; R7.2; R7.3; H13 |
| D4 flash attempt ID | R8.3 phase 6; R8.6; R8.5 |
| D5 merge preconditions | R10.4 (CS and checks, every merge and resume); R10.5; H12 |
| D6 `batch settle` | R13.4; §12 row 30 |
| D7 qualification candidate | R14.3 (as changed by E2) |
| D8 recovery from disk | R13.2; H10; R8.1 |
| D9 camera re-baseline | H14; R8.2 step 8; §18 item 2 |
| M1 pre-merge restore | R7.8; R11.2 step 2 |
| M2 holder handshake | R6.2 |
| M3 incremental provenance | superseded by E1 (R7.5); H1 (`.bench-result`); R14.2 B4 |
| M4 TC | R7.4 items 1, 3, 4 |
| M5 runner version, image | R2.2; R7.5, R14.4, H1 |
| M6 transport, revisions, migration, promote | R5.4, R7.2, R11.6; R7.3 (`rev`); R5.7; H11, R10.6 step 2 |
| M7 tooling path gated | R5.8; §17 preamble; §18 item 1 |
| Minors | report retry R15.1; boot count R8.4; A1 R14.2; device-bench exit 4 R8.6; setup ordering R13.2 step 7; `voids_in_a_row` R9.1; prune refs R11.2 2e, R10.8; cleanup only composed SHA R13.3; forward versions R11.2 step 3; calibration staging R14.1; replies R5.1, R5.7; `runner_tools` R2.2, R14.4; "base unverified" §1, R15.1; TC parallel R7.4; H4 allowlist; lock around `repo forall` R6.5, H4 |

Round 2 (`batch-runner-r2-findings.md`):

| Item | Closed by |
| --- | --- |
| S1 manifest XML | R5.4 step 3; R8.2 step 2; H4; §18 Z7 |
| S2 camera set | R5.4, R7.2, R7.3; R7.7; R10.1; §18 item 2 |
| S3 no bisection | R9.3; R7.1 |
| S4 conviction | superseded by D1 (R9.4) |
| S5 foreign pair | R13.2 step 4; R11.2 2b; H12 |
| S6 one state file | R4.1 |
| S7 rehearsal | R14.3 |
| M1 verified base | §1; CS R10.4; R14.3, R14.5 |
| M2 `change merge` | R5.8, H4, H7 |
| M3 approval records | R5.2-R5.6 |
| M4 provenance | R7.4; R6.5, H4; R6.4 |
| M5 lock fds | R6.2 |
| M6 A→B→A | R14.2 |
| M7 crash recovery | R11; R13.2; §12 |
| M8 qualification | R14.2-R14.4; R2.2 |
| Minors | exit 75 R3.1; seed flag R10.6 step 3; comments R10.7; local `batch/*` R10.8; boot_info names R14.2; `origin`/URLs §1, R10.2 step 4; H1 order; H3 checkpoint; H4 fail closed R4.1; counts §1, R14.5; unimplicated members R9.3 |

Round 1 (`batch-runner-r1-findings.md`), where still relevant:

| Item | Closed by |
| --- | --- |
| A1 base races | R6.5, H4; CS R10.4; R10.6; Z6 residual |
| A2 moved heads | R5.2, R5.3; R5.7 |
| B1 provenance | R7.4; R7.5 (E1); R6.4; R14.2; S1 |
| B2 per-change compose | R7.3 |
| B3 git-lfs | R7.4 item 7, `PATH`; R7.5 |
| B4 small build items | R7.4; R7.2; R7.3 |
| B5 disturbed tree | R7.8 |
| C1-C6 cache, evidence, build classes, search | no cache; superseded by D1 and S3; build classes R7.6 |
| D1 package | R2.1, R2.2 |
| D3 lock protocol | R6.2, R6.3, H1, H2, H4, H15 |
| D4 halted, binding | R9.1, R8.5, H6 |
| D5 setup failure | R9.1; R13.2 step 7 |
| D7 qualified | H3 |
| E1 idle, reconcile | R8.1; R13.2 |
| E2 crash windows | R11; R7.3; R10.2; R13.2, R13.3; R10.3 |
| F1 charter scope | S1, §18 Z7; H4; R5.8 |
| G1-G8 | RAM R8.4; queue outcome R10.7, R10.8; retention R15.2; GitHub delay R10.7; exit 4 R8.6; report blockers R15.1; calibration R14.5; breakfast R7.6, R9.1 |

## 20. Open questions and residuals

Q3. Does GitHub mark the change PRs merged when a batch PR's merge commit
brings their heads into `river-22.2`? R10.7 closes any it doesn't, so
correctness does not depend on it.

Residuals, accepted: the Z6 web-UI merge window; a merge observation that
stays unknown (R10.4a) keeps the runner at exit 75 until GitHub agrees with
itself, with a notification after 24 h rather than a HALT; a hand
`git push` to a change branch in the instant of R10.7's close is undone by
the reopen, not prevented; KB reads the build IDs from the properties init
loaded at boot, which come from the same `build.prop` files the harness's
root read greps; a SIGKILLed holder whose
child keeps running (every tool refuses until it ends, H15); a tool that
creates its own session inside a runner child would be refused by H15 (none
does today); `--zak` flags and `RIVER_BATCH_ID` are conventions against
rule-following agents, not authentication.

---

# Implementation requirements (round 6), binding

## Batch-runner design, round 6 result and implementation requirements

Round 6 (rev 6, `batch-runner-design.r6.md`): **Opus APPROVE 8** (conditions O6-C1..C3), **Gemini APPROVE 8** (conditions G6-C1..C5), Sol OBJECT 6 (4 blockers, 8 majors). Decision: the design phase ends here. Implementation starts from rev 6 with every item below as a **binding requirement**. The implementation PR goes through council code review and needs all three approvals before it merges; the reviewers will check these items against code and tests.

## Requirements (overlaps merged)
I1 (Sol B1, Gemini C1 blocker). KB must not read `ro.vendor.river.build_id` with non-root `getprop` (SELinux: vendor_default_prop is unreadable by the `shell` coredomain). Use a read that works with non-root adbd: e.g. `/vendor/bin/sh` (vendor_shell) reading `/vendor/build.prop`, or `ro.river.build_id` plus `ro.vendor.build.date.utc` matched against the known-good batch's staged vendor date (with root-level verification still done by `fbmode.verify_boot` before flashing). Verify live on the test phone after `adb unroot` (non-root adbd), then restore root.
I2 (Opus m1, Gemini C2, Sol B1 part). KB does not call `dev.guard()`. It reads ro.product.device, ro.serialno, ro.boot.slot_suffix, sys.boot_completed and the stamps in one checked `sh_rc` call; HALT only on rc 0 with a non-empty mismatched product or serial; rc≠0 or empty values → re-check state/fastboot → pause or blocked.
I3 (Opus C2, Gemini C3, Sol M1). Bootloader-fallback exit 4: H17 also routes `flashing.py`'s fallback path through `rescue()` (or R8.6 accepts the `bootloader fell back to slot` prefix); both exit-4 paths get the same structured disposition with wipe-seq and attempt-ID checks; unit tests for both.
I4 (Sol B2, Gemini C4). (tip = landed merge, PR still OPEN) is `unknown` (75), not `landed`; tags and `flash --promote` wait until GitHub reports MERGED with mergeCommit == tip, headRefOid == tested, baseRefName river-22.2, isCrossRepository false. `flash --promote` itself returns 75 (not 3) when its own read sees the same lag (H11). Unexpected MERGED in R10.4 step 3 goes through R10.4a.
I5 (Opus C1). Two agreeing observations that fit neither pending nor landed are a mismatch (HALT → settle). "Unknown" is only failed or disagreeing reads. Additionally `batch settle --zak` may act on a running batch with merge_started whose reconciliation has been unknown for more than 24 h.
I6 (Opus C3). `batch stage` ends with a finish transaction that clears `running` and `lease` after staging, so a hand-flashed calibration/baseline pair is always foreign (blocked under O-C1). Only `qualify` can leave an interrupted run holding a pair.
I7 (Gemini C5). KW's commit_set = `running.merge` merge commits for PR repos + batch.json commit_set for no_pr repos; if any expected PR's merge commit is missing locally, resolve it from GitHub (75 on transport) before writing known-good.json; never substitute batch-branch SHAs.
I8 (Sol B3). Part-1 A→B→A comparison: `system/product/etc/build-manifest.xml` (repo manifest -r) changes with project HEADs; verify its recorded revisions against each trial's commit set and exclude those expected revision lines from the content comparison.
I9 (Sol B4). Partial first merge (e.g. river Perch landed, vendor/lineage not): define the authorized repair before qualifying, i.e. Zak either completes the missing merges of the same batch (exact tested heads, `--match-head-commit`) or reverts the landed ones through reviewed PRs, then re-baselines. Also require a re-baseline whenever current fork trees have drifted from known-good, even if none of the batch's approved heads landed.
I10 (Sol M2). Camera re-baseline: R7.7's camera backstop applies to production candidates only; `batch stage --kind baseline` allows a changed set. The requalification sequence includes a verified `flash --rollback` after README step 8 (the hand calibration pair), before runner part 2.
I11 (Sol M3). `batch settle` checks the local promotion proof and writes known-good.json first, before any GitHub call.
I12 (Sol M4). Cleanup discovers PRs by exact head branch in every touched repo (not only saved pr_repos), with repo, branch, base and tested-head checks before closing.
I13 (Sol M5, Opus m7). failure_history is keyed by canonical change identity (the set of repo#PR keys), not only by name; enqueue refuses an alias for PRs already associated with another change.
I14 (Sol M6). Approval evidence binds each provider's actual verdict and the exact scope (full, or delta from→to) of the round, not just SHA presence; replies cite full 40-hex heads; briefs list full heads. (New GNSS/Perch council rounds at migration are accepted.)
I15 (Sol M7, Opus m5, Gemini M2). The end-of-tick pass after an unknown outcome is local-only: no second remote reconciliation in the same tick; it keeps the original outcome and respects pair ownership.
I16 (Sol M8). Direct `phone-qa stage` takes (or reuses) the build lock across staging and refuses the runner's reserved IDs (`bNNNN`, `qNNNN`).
I17 (Sol M9). Retention (R15.2) runs before the free-space floor check, under the same locks and exclusions.
I18 (Opus m2). R13.2 "phone in fastboot" with no pair acts only if `fbmode.pending()`; otherwise blocked "phone in fastboot" (someone's dev session).
I19 (Gemini M3). Paused pass: a foreign pair only gates setup, never overwrites the existing pause reason; a blocked tick still flushes reports.
I20 (Gemini M7). `qualify`/`stage` exit 1 on a live blocking condition (disturbed tree, foreign pair, known-good disagreement, strike limit) rather than proceeding under an existing pause.
I21. Minors from all three: R2.4/R2.3 table split; R14.1 "step 2 only"; R14.3 names qualify's own record transaction; R9.5 note on the solo-B pause; m8 settle report advises reverting a broken base before re-baselining; m9 reports_pending writes are idempotent; R5.1/R5.2 basenames only; H4 fetches origin/river-22.2 and refs/pull/<n>/head first; R5.4 checks previous_filename on renames; H11 classifies gh GraphQL errors; §19 E3 reference; H17 names `bench.run`; R5.2 full-SHA wording.

# phone-qa: the river QA harness

`phone-qa` decides whether a batch of changes may merge without Zak. It
proves that each item on the must-work list in [CHARTER.md](../CHARTER.md)
works on the exact build under test. The design, approved by the council in
round 11, is [DESIGN.md](DESIGN.md). This file covers how to use it.

Stage 1 (this code) has `phone-qa`, the helper APK, the fixtures, and the
`flash` and `device-bench` support. Stage 2 is the batch runner
(`change batch`), the queue and the tester thread. See
[Interfaces for the batch runner](#interfaces-for-the-batch-runner).

## Day to day: `phone-qa --quick`

Run it after every flash. It replaces `phone-check`.

```sh
scripts/phone-qa --quick          # add --wait straight after a flash
```

It runs the health group on the live phone: boot completed, SELinux
enforcing, no crashes since boot, no tombstones since boot, no services
crashing or restarting in a loop, no telephony. Any FAIL exits 1. INFO lines
for the build in `out-stripped`, Wi-Fi, Bluetooth, GPS, cameras, sensors,
fingerprint and audio are for reading only. `--quick` loads no fixtures and is
never merge-eligible.

## The batch sequence

The runner holds the device lock across the whole sequence. Without a runner,
wrap the commands in `scripts/device-lock`:

```sh
S=.repo/manifests/scripts
$S/build --batch <id>                   # build ID "<id>-<commit set hash>"; refuses a dirty tree
$S/phone-qa stage <id> [--kind batch|baseline|calibration|experiment] [--experiment]
$S/flash --batch <id> --wipe            # hash checks, slot pair, first-boot evidence
$S/bench/device-bench current --boots 1 --batch <id>
# boots 2-3 when the charter calls for them (see DESIGN.md, step 4)
$S/bench/device-bench current --boots 2 --batch <id>
$S/phone-setup --wifi SSID_FILE PSK_FILE
$S/phone-qa --batch <id>
```

For `--kind batch`, `stage` refuses a build that isn't tied to the batch: the
build ID must be `<id>-<commit set hash>` in both `build.prop` files, never
`unstamped`, and the commit set must cover every zrm625 fork and the manifest
at clean commits. A new `flash --batch` starts a new flash attempt:
`device-bench` and `phone-qa` then use only that attempt's bench rows.

`phone-qa --batch` exits 0 only if every required check passed. It writes
`$BENCH_HOME/qa/runs/<run-id>/results.json` and `report.html`. The tester
merges only when the exit code is 0, `eligible` is `true`, the mode is
`batch`, and the batch ID matches. Exit 1 means a check FAILed. Exit 2 means
at least one check is UNVERIFIED.

`eligible` is `true` only if all of these hold: batch mode with the full
check set; `batch.json` kind `batch` with `experiment: false`;
`ro.config.low_ram` read successfully and not enabled (Android's boolean
values: `1`, `y`, `yes`, `on`, `true`); every required check PASS, and every
helper check reports a terminal helper result; `thresholds.json` says
`calibrated: true`; 5 clean `--calibrate` runs and every control qualified,
both for the current harness version (`phone-qa version`). Any failed or
incomplete diagnostic read is UNVERIFIED, never PASS.

After a FAIL or UNVERIFIED result, roll the phone back to the known-good slot
with `flash --rollback`. Rollback wipes data, so run `phone-setup` after it.

## Modes

| Command | What it does | Eligible? |
| --- | --- | --- |
| `phone-qa --quick [--wait]` | Health group on the live phone | never |
| `phone-qa --batch ID` | Every check | if all pass and the controls qualified |
| `phone-qa --batch ID --only X,Y` / `--skip X,Y` | A subset (check IDs or groups). Preflight always runs and can't be skipped. | never |
| `phone-qa --calibrate --batch ID` | Every check; regression only records | never |
| `phone-qa --baseline --batch ID` | Identity and health only; on a clean run writes `known-good.json` and closes the baseline pair | never |
| `phone-qa --control NAME --batch ID` | One deliberately broken run from the control set | never |
| `phone-qa boot-timing` | Calibration step 1: the bootloader allowance A per reboot path | n/a |
| `phone-qa controls` | Which controls are qualified for this harness version | n/a |
| `phone-qa controls --boot-timing` | The step-1 proofs again, under the current harness version | n/a |
| `phone-qa stage ID` | Copy a build's images into `$BENCH_HOME/batches/ID` | n/a |
| `phone-qa helper` | Build or verify the helper APK | n/a |
| `phone-qa journal` | Show anything the state journal still has to restore | n/a |
| `phone-qa version` | The harness version (a content hash) | n/a |

Groups: `preflight`, `identity`, `health`, `radios`, `hardware`, `system`,
`apps`, `cleanup`, `final`. The canonical list of required check IDs is
`GROUPS` in `lib/riverqa/results.py`.

## Calibration and bootstrap

The steps run in this order (DESIGN.md, "Calibration and bootstrap"):

1. `flash --wipe` the known-good build, then run `phone-qa boot-timing`
   straight away, before any other adb reboot. It flashes nothing itself and
   writes `$BENCH_HOME/qa/boot-timing.json`. Until both proofs pass for a
   reboot path, the uptime signal on that path is UNVERIFIED. On the adb path
   the clock starts at the old kernel's reset ([amendment
   A1](DESIGN.md#amendment-a1-the-adb-path-uptime-clock)); only reboots that
   got a reset anchor set A, and their largest shutdown tail becomes the bound
   for every later anchor. The first adb reboot after a wipe (every batch's
   first bench boot) has the longest tail, so the bound is set only if that
   reboot, step 1's first adb reboot, got an anchor; otherwise the adb path
   has no bound and its uptime signal stays UNVERIFIED. A later boot with a
   slower tail than the bound is UNVERIFIED, never PASS. Changing this file
   changes the harness version.
2. The baseline: build `river-22.2` HEAD with `scripts/build`, then:

   ```sh
   S=.repo/manifests/scripts
   $S/phone-qa stage baseline-N --kind baseline
   $S/device-lock sh -c "$S/flash --batch baseline-N --wipe &&
     $S/bench/device-bench current --boots 3 --batch baseline-N &&
     $S/phone-qa --baseline --batch baseline-N"
   $S/phone-setup --wifi SSID_FILE PSK_FILE
   ```

   The first `--baseline` flash also performs the first-run transition: the
   slot running now becomes the provisional known-good slot. A clean baseline
   closes its own pair and writes `$BENCH_HOME/qa/known-good.json` with the
   seeded RAM reference. A failed baseline leaves the pair open. Roll it back
   with `flash --rollback`, then run the baseline again.
3. Perch calibration: `phone-qa --calibrate` five times on one flash of the
   Perch candidate. Not done in stage 1.
4. Thresholds: write the calibrated values into `thresholds.json` and set
   `"calibrated": true`. They never go below the hard floors in
   `features.FLOORS`. This changes the harness version, so run
   `phone-qa --calibrate` five more times: the gate needs `calibrated: true`
   and 5 clean (exit 0, full set) `--calibrate` runs recorded for the current
   harness version in `$BENCH_HOME/qa/calibration.json`.
5. Controls: run every control in `phone-qa controls`. Until all of them
   qualify for the current harness version, every run reports
   `eligible: false` with "controls not qualified". A control qualifies only
   if its fault was really injected (the run's injection marker names it) and
   a target check reached the expected state; `controls.json` stores both,
   with each target's summary. Most controls are one command,
   `phone-qa --control NAME --batch ID`, on a calibration batch. The rest:

   - `extra-reboot-first-boot`: `RIVERQA_CONTROL=extra-reboot-first-boot
     flash --batch ID --wipe`, then `device-bench current --boots 1 --batch
     ID`, then `phone-qa --control extra-reboot-first-boot --batch ID`.
   - `extra-reboot-bench`: a normal `flash --batch ID --wipe`, then
     `RIVERQA_CONTROL=extra-reboot-bench device-bench current --boots 1
     --batch ID`, then `phone-qa --control extra-reboot-bench --batch ID`.
     Both reboot the phone with adb before boot completes and record that in
     `batch.json` (`control_injections`). `flash` and `device-bench` refuse
     `RIVERQA_CONTROL` on a kind `batch`.
   - `killed-mid-check`: start `phone-qa --calibrate --batch ID`, kill it
     during a feature check, then run `phone-qa --control killed-mid-check
     --batch ID`. It qualifies only if journal recovery restored the killed
     run's leftovers and the second run is clean.
   - `tag-pushed-host-killed`: `phone-qa --control tag-pushed-host-killed
     --batch ID` runs `flash --recover` against a scratch `$BENCH_HOME` with
     stubbed `gh` and `git ls-remote`; no phone.
   - The four `boot-timing-*` controls come from step 1, or from
     `phone-qa controls --boot-timing`, which runs only the step-1 proofs
     again with the recorded A and tail bound.

### Order of qualification

The controls and the calibration runs count only for the harness version
they ran under. The version hashes the harness code, `thresholds.json`,
`fixtures.json`, the `paths` and `chain` blocks of `boot-timing.json` (the
proofs included) and the baseline's `calibration_seed`. So qualify in this
order, and start again from the step that changed if anything in that list
changes:

1. Freeze the harness: no more commits to `qa/` or `scripts/phone-qa`.
2. `flash --wipe` the known-good build, then `phone-qa boot-timing` (step 1).
3. The baseline (step 2). It writes `calibration_seed`.
4. Perch calibration and thresholds (steps 3 and 4). Writing
   `thresholds.json` is the last change to a version input.
5. `phone-qa controls --boot-timing`. This renews the four `boot-timing-*`
   controls under the final version. It rewrites the proofs in
   `boot-timing.json`, so it must come after every other version input has
   changed and before every other control.
6. Note `phone-qa version`. This is the final version.
7. Every other control (step 5 above) until `phone-qa controls` shows all of
   them qualified.
8. Flash the Perch calibration batch again and run `phone-qa --calibrate`
   five times, each with exit 0 and the full check set.
9. Check that `phone-qa version` still prints the version from step 6 and
   that `phone-qa controls` exits 0.

## The uptime clock on the adb reboot path (amendment A1)

The first baseline (`baseline-1`, 2026-11-23) showed that the USB gadget
drops several seconds before the kernel resets, so a clock that starts at the
USB disconnect includes a variable shutdown tail, and no allowance A could
separate a normal boot from one with an extra bootloader pass. Amendment A1
(DESIGN.md) starts the adb-path clock at the old kernel's reset instead:

- the leaving marker's host write time is paired with its own printk
  timestamp, read back from `dmesg`;
- the marker's own timestamp in pstore must match that readback, and the gap
  to the reset is measured inside pstore from it;
- the reset is the earliest stamp of the last run of three consecutive
  non-decreasing pstore stamps within 1 s after the marker, so a bit flip
  can't move it later;
- it must fall between the USB disconnect and the disconnect plus the largest
  calibrated shutdown tail;
- it is used only when that boot's chain is PASS. Otherwise the adb-path
  uptime signal is UNVERIFIED; it never falls back to the disconnect.

The fastboot path keeps the disconnect clock (10.01-10.08 s, stable). The
measurements behind the change are in `$BENCH_HOME/qa/anchor-experiment.json`.
`boot-timing` has to be re-run on the phone before the new clock is usable.

## Known issues

- **The shutdown-tail bound comes from step 1 only.** The bound is the
  largest tail among step 1's normal adb reboots, which must include the
  first adb reboot after a wipe (F1). A later build with a longer shutdown
  would make its anchors out of bounds, and its bench boots UNVERIFIED
  (never PASS). If that happens, `flash --wipe` and re-run `boot-timing`,
  with more normal reboots if needed (`--normal N`).
- **Camera-set approvals and the gh account.** The approval must be a
  comment or review by `zrm625` on one of the batch's own PRs, naming the new
  set on a line `approve camera set: 0,1,2`. `gh` on forge is logged in as
  `zrm625`, so the check proves the account, not that Zak typed it.
- **The test page changed.** The page script now writes a string only it can
  produce (the token reversed) for the Brave check, so `qa/fixtures.json`
  pins a new `page.html`. Copy it into `$BENCH_HOME/qa/fixtures/` (run
  `qa/fixtures/make-fixtures`) before the first run of this version, or
  preflight is UNVERIFIED.

## One-time host setup

- **Firewall for the Wi-Fi check.** The Wi-Fi check fetches a page from a
  listener on forge's LAN address, port 47801. forge runs ufw, so add this
  rule once (it needs sudo):

  ```sh
  sudo ufw allow from 192.168.4.60 to 192.168.4.44 port 47801 proto tcp comment 'river phone-qa Wi-Fi check'
  ```

  Until the rule is in place, the Wi-Fi check is UNVERIFIED with "LAN
  listener unreachable".
- **Fixtures.** The three APKs come from their projects' release pages. The
  test clip, its reference frames and the test page are generated with
  `qa/fixtures/make-fixtures`. Every file is pinned by SHA-256 in
  `qa/fixtures.json`. A missing file or a hash mismatch makes preflight
  UNVERIFIED, and nothing else runs.
- **Helper APK.** It is built offline on first use with build-tools 37 and
  `platforms/android-36/android.jar` from the SDK at
  `~/.local/share/mise/installs/android-sdk/23.0`, and signed with
  `~/.config/.android/debug.keystore`. It is cached in
  `$BENCH_HOME/qa/helper/` by a key that covers the sources, the toolchain and
  forge's LAN address.

## Files

| Path | What |
| --- | --- |
| `scripts/phone-qa`, `scripts/flash`, `scripts/bench/device-bench`, `scripts/device-lock` | Entry points (Python 3, standard library, `python3 -I`) |
| `qa/lib/riverqa/` | The code. Pure logic is in `results.py`, `regression.py`, `slots.py`, `journal.py`, `bootsig.py`, `evidence.py` and `analysis.py` |
| `qa/helper/` | The `river-qa` helper APK source (plain Java, no Gradle) |
| `qa/fixtures.json`, `qa/fixtures/` | Fixture pins, the test page and the fixture generator |
| `qa/thresholds.json` | Thresholds and calibrated parameters |
| `qa/tests/` | Host-side unit tests (`unittest`) |
| `$BENCH_HOME/batches/<id>/` | `batch.json`, staged images, `flash.json`, per-boot evidence under `boots/` |
| `$BENCH_HOME/slot-state.json` | The slot pair and `known_good_slot` |
| `$BENCH_HOME/qa/` | `known-good.json`, `boot-timing.json`, `controls.json`, `calibration.json`, `journal.json`, `wipes.jsonl`, `history.jsonl`, `runs/` |

## Interfaces for the batch runner

- **The lock.** Take `$BENCH_HOME/.device-lock` with `flock`, write a fresh
  token into the file, and export `BENCH_LOCK_TOKEN`. `scripts/device-lock`
  does exactly this. Child tools reuse the lock only if the token matches.
- **`batch.json`.** `phone-qa stage` writes it. The schema is in the docstring
  of `lib/riverqa/stage.py`. The runner adds `prs`, `tag` and `tag_repos`
  when it opens PRs, so recovery can look them up. `tag_repos` must list
  every repo in the batch's `commit_set`, not only the ones with PRs. Before
  any merge or tag action the runner runs `flash --merge-started <id>` and
  waits for it to succeed; that records `merge_started` in `batch.json`. It also adds
  `camera_set_change` (`url` of Zak's comment or review on one of the
  batch's PRs, and the new `set`) when Zak approves a camera change. A
  batch with `camera_set_change` is never eligible ("camera set change: Zak
  merges by hand", amendment A7). `hardware.cameras` still checks the
  approval and the new set, and the report shows the result as evidence,
  but Zak merges that batch by hand. `flash`
  adds `pair`, `first_boot_seconds` and `flash_attempt`, and moves the old
  `bench_runs` to `prior_bench_runs` at every new attempt. `device-bench`
  appends to `bench_runs`. Control runs add `control_injections`.
- **`slot-state.json`.** `flash` owns it (schema in `lib/riverqa/slots.py`).
  The runner uses `flash --recover` at the start of every batch, then
  `flash --rollback` after a FAIL, UNVERIFIED, experiment or calibration run,
  and `flash --promote <id>` after it has durably merged and tagged a batch.
  `--promote` itself looks the batch up the way `--recover` does and releases
  the fallback only with proof for every repo of the tested commit set: for a
  repo with a PR, the PR's head is the tested commit, the PR is merged and
  the tag is at its merge commit; for a repo without a PR, the tag is at the
  tested commit. A stale or incomplete `prs`/`tag_repos` record halts. With
  nothing merged or tagged, recovery rolls back only if `merge_started` is
  absent; with it, recovery halts. A baseline pair is never promoted (`phone-qa --baseline`
  closes it). A closed baseline's seed is saved in `slot-state.json` in the
  same write as the slot change; if `known-good.json` didn't follow, no batch
  may flash until `flash --recover` finishes it. `flash` exits 3 when the
  pipeline must halt for Zak (including a guard failure during rescue or
  rollback), and 4 when a boot failed and the phone was rescued back to the
  known-good slot.
- **`known-good.json`.** `phone-qa --baseline` seeds it, including the
  immutable `calibration_seed` that the harness version hashes. After a
  clean tag, the tester updates it with `regression.update_on_tag()`.
- **The target phone.** The serial `ZY225DPT7F` is fixed in the code;
  `BENCH_DEVICE` may only repeat it. Every reboot command checks the phone
  (adb) or bootloader (fastboot) reports `river` first.
- **Rerun once.** A spurious extra-boot FAIL (this phone has reset on its
  own) is settled by rerunning the batch once. That rule belongs to
  `change batch`.

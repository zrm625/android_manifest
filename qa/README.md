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
$S/bench/device-bench current --boots 1 --batch <id>   # measures boots 1 (adb) and 2 (sysrq)
# boot 3 when the charter calls for the median (see DESIGN.md, step 4)
$S/bench/device-bench current --boots 1 --batch <id>
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
| `phone-qa boot-timing` | Calibration step 1: the bootloader allowance A for the fastboot and sysrq paths, and the adb chain control | n/a |
| `phone-qa controls` | Which controls are qualified for this harness version | n/a |
| `phone-qa controls --boot-timing` | The step-1 proofs again, under the current harness version. Refuses a `boot-timing.json` measured under older calibration rules, or whose samples don't give the stored A | n/a |
| `phone-qa stage ID` | Copy a build's images into `$BENCH_HOME/batches/ID` | n/a |
| `phone-qa helper` | Build or verify the helper APK | n/a |
| `phone-qa recover` | Clear a leftover `fastboot oem fb_mode` (a journaled obligation, or the phone sitting in fastboot) and boot Android | n/a |
| `phone-qa journal` | Show anything the state journal still has to restore | n/a |
| `phone-qa version` | The harness version (a content hash) | n/a |

Groups: `preflight`, `identity`, `health`, `radios`, `hardware`, `system`,
`apps`, `cleanup`, `final`. The canonical list of required check IDs is
`GROUPS` in `lib/riverqa/results.py`.

## Calibration and bootstrap

The steps run in this order (DESIGN.md, "Calibration and bootstrap"):

1. `phone-qa boot-timing`. It flashes nothing itself and writes
   `$BENCH_HOME/qa/boot-timing.json`. It sets A for the two gated reboot
   paths (amendment A2): the fastboot path and the immediate `sysrq`
   reboot. Until all three proofs pass for a path, the uptime signal on
   that path is UNVERIFIED. The `adb reboot` path (bench boot 1) has no
   uptime gate; step 1 runs its chain control instead. The `sysrq` path's
   injected extra pass sets `fastboot oem fb_mode_set` for one boot and
   clears it again. Changing `boot-timing.json` changes the harness
   version. `--reuse-normals` reuses the stored normal reboots only if
   they come from this build and every one carries the current
   calibration-rules stamp (`rules_version` 3, full-precision brackets);
   otherwise it says why and measures fresh ones.
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
     `RIVERQA_CONTROL=extra-reboot-bench device-bench current --boots 2
     --batch ID`, then `phone-qa --control extra-reboot-bench --batch ID`.
     The fault goes into the first `sysrq` bench boot (boot 2), and the
     control qualifies only if that boot's uptime gate is FAIL.
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
   - The five `boot-timing-*` controls come from step 1, or from
     `phone-qa controls --boot-timing`, which runs only the step-1 proofs
     again with the recorded A: `boot-timing-{fastboot,sysrq}-extra-pass`
     and their `-unmarked` twins, and `boot-timing-adb-extra-reboot` (an
     extra full reboot after ramoops init on an `adb reboot` like bench boot
     1 must FAIL through the chain).

### Order of qualification

The controls and the calibration runs count only for the harness version
they ran under. The version hashes the harness code, `thresholds.json`,
`fixtures.json`, the `paths` and `chain` blocks of `boot-timing.json` (the
proofs included) and the baseline's `calibration_seed`. So qualify in this
order, and start again from the step that changed if anything in that list
changes:

1. Freeze the harness: no more commits to `qa/` or `scripts/phone-qa`.
2. `phone-qa boot-timing` (step 1). It needs no wipe: since amendment A2
   nothing in it depends on the first boot after a wipe. Its summary must
   show A and all three proofs for both `fastboot` and `sysrq`, and the adb
   chain control qualified; it exits 0 only then.
3. The baseline (step 2). It writes `calibration_seed`.
4. Perch calibration and thresholds (steps 3 and 4). Writing
   `thresholds.json` is the last change to a version input.
5. `phone-qa controls --boot-timing`. This renews the five `boot-timing-*`
   controls under the final version. It rewrites the proofs in
   `boot-timing.json`, so it must come after every other version input has
   changed and before every other control.
6. Note `phone-qa version`. This is the final version.
7. Every other control (step 5 above) until `phone-qa controls` shows all of
   them qualified.
8. The five clean calibrations. A flash archives the old attempt's bench
   rows (`identity.bench_rows` then has nothing for the new attempt), so this
   step does the whole flash sequence again, in this order:

   ```sh
   S=.repo/manifests/scripts
   $S/flash --recover        # 1. resolve the previous pair (a calibration pair
                             #    always rolls back: verified wipe, back to known-good)
   $S/device-lock sh -c "$S/flash --batch CAL --wipe &&                  # 2. flash with a wipe
     $S/bench/device-bench current --boots 3 --batch CAL"                # 3. bench (the 3-boot median)
   $S/phone-setup --wifi SSID_FILE PSK_FILE                              # 4. set up the phone
   for i in 1 2 3 4 5; do $S/phone-qa --calibrate --batch CAL || break; done   # 5. five clean runs
   ```

   `CAL` is the Perch calibration batch (kind `calibration`). `flash
   --batch` refuses while any pair is open, including this batch's own
   (`slots.preflight`), and needs the phone on `known_good_slot`, which the
   rollback in 1 guarantees. `phone-setup` runs after `device-bench`, never
   before, because the benchmark measures the phone with the wizard
   untouched. Each of the five must exit 0 with the full check set (no
   `--only` or `--skip`); they are recorded for the current version in
   `$BENCH_HOME/qa/calibration.json`.
9. Check that `phone-qa version` still prints the version from step 6 and
   that `phone-qa controls` exits 0.

Step 2 is needed again after any change to the reboot paths or the
boot-signal code (`reboots.py`, `bootsig.py`, `calibrate.py`).

## Reboot paths and the uptime clock (amendment A2)

| Path | Used by | Clock | Uptime | Reset detection |
| --- | --- | --- | --- | --- |
| `fastboot` | the first boot after a flash, step 1 | the disconnect after the final `fastboot reboot` | gated by A | uptime, chain, boot count |
| `adb` (`adb reboot`) | bench boot 1 only (the charter's boot time) | none | INFO only | chain, boot count |
| `sysrq` (`sync; echo b > /proc/sysrq-trigger`, as root) | every later bench boot, at least one per batch, step 1 | the disconnect (USB drops at the reset) | gated by A | uptime, chain, boot count |

The first baseline (`baseline-1`, 2026-11-23) showed that the USB gadget
drops several seconds before the kernel resets on `adb reboot`, so a clock
that starts at the disconnect includes a variable shutdown tail. Amendments
A1 and A1′ tried to read the reset from pstore instead; ramoops damages too
many lines on river for that to work, so A2 dropped it. The `sysrq` reboot
resets at once (USB gone 1.17-1.28 s after the command, then 9.89-9.93 s to
kernel start; the fastboot path's pass is 10.05-10.09 s).

The chain passes when the previous kernel's log holds a copy of this
attempt's leaving marker, within 3 wrong characters, with no new kernel
session after it (amendment A2.1). Each marker carries a fresh 64-bit random
nonce, so a copy of any other write can't match (about 1 in 10¹³); printk
stamps play no part. No copy at all in a readable log is a FAIL (another
kernel's log replaced it). No marker, or a missing, empty or unreadable
pstore, is UNVERIFIED. `boot-timing` keeps the raw pstore of every reboot
whose chain did not pass in `$BENCH_HOME/qa/pstore-nopass/`.

## Known issues

- **A reset early in bench boot 1 is not caught on that boot.** If the
  interrupted kernel resets before it initialises ramoops (its first ~1 s),
  the marker kernel's log survives and the chain passes. Only a kernel or
  bootloader change can cause that, and the same batch's fastboot first boot
  and `sysrq` bench boot are still uptime-gated. See DESIGN.md, amendment A2.
- **A badly damaged marker fails the chain.** ramoops has no ECC. If every
  copy of the marker has more than 3 wrong characters, the log looks like
  another kernel's and the chain is FAIL. No boot on the A1′ or A2 data had
  that; the raw pstore is kept in `$BENCH_HOME/qa/pstore-nopass/`.
- **A stuck fb_mode.** The `sysrq` extra-pass control sets `fastboot oem
  fb_mode_set` for one boot and clears it on success and on failure. It
  journals the obligation (`$BENCH_HOME/qa/journal.json`, a host entry of
  kind `fb_mode`) before it sets it. If the harness was killed in between,
  the phone stops in fastboot on every boot until the obligation is
  cleared. The next `phone-qa`, `device-bench`, `flash --batch` or
  `phone-qa boot-timing` run clears it under the device lock and boots the
  active slot; `phone-qa recover` does only that. A full `phone-qa` run that
  had to do this stops with `preflight.journal` FAIL, because the phone
  rebooted outside the plan. The obligation is resolved only once Android
  booted, so a run killed in between leaves it for the next one.
  `flash --rollback`, `flash --recover` and rescue never boot the active
  slot (it may be the failed candidate): in fastboot they clear fb_mode, set
  the fallback active, then reboot, and resolve the obligation once the
  fallback is verified. A dev flash (also `device-bench --flash`) clears it
  and stays in fastboot. If the clear fails with an obligation pending,
  rollback still sets the fallback active, then halts (exit 3). Any other
  failure of `flash --recover`, `--rollback` or rescue on the phone (a
  fastboot error, a timeout, a guard failure) also halts with exit 3 and an
  `error:` line, and leaves the obligation pending.
- **The phone found in fastboot.** `flash --batch` boots `known_good_slot`
  from fastboot, and `flash --promote` the candidate slot; if fastboot
  reports another slot active they halt (exit 3) instead of booting it.
- **No device job during a build.** `flash`, `device-bench`, `phone-qa` and
  calibration refuse to start while a build holds `$BENCH_HOME/.lock`: a
  build eats the timing brackets' headroom. `flash --merge-started` and
  `flash --promote` time nothing and may run during a build.
- **Bench rows from before A2** have no `reboot_path`, so
  `health.boot_signals` is UNVERIFIED for them; bench the batch again.
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
| `$BENCH_HOME/qa/` | `known-good.json`, `boot-timing.json`, `pstore-nopass/`, `controls.json`, `calibration.json`, `journal.json`, `wipes.jsonl`, `history.jsonl`, `runs/` |

## Interfaces for the batch runner

- **The lock.** Take `$BENCH_HOME/.device-lock` with `flock`, write a fresh
  token into the file, and export `BENCH_LOCK_TOKEN`. `scripts/device-lock`
  does exactly this. Child tools reuse the lock only if the token matches.
- **`batch.json`.** `phone-qa stage` writes it. The schema is in the docstring
  of `lib/riverqa/stage.py`. The runner adds `prs`, `no_pr`, `tag` and
  `tag_repos` when it opens PRs, so recovery can look them up. Every repo in
  the batch's `commit_set` must be in exactly one of `prs` (with its PR
  number) or `no_pr` (its repo path), and `tag_repos` must list every repo
  in `commit_set`, not only the ones with PRs. Before any merge or tag
  action the runner runs `flash --merge-started <id>` and waits for it to
  succeed; that records `merge_started` in `batch.json` and freezes the
  expected PR set. It refuses an incomplete set, and a changed one later. It also adds
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
  The runner uses `flash --recover` at the start of every batch (with no
  pair it only clears a leftover fb_mode and returns to `known_good_slot`
  without a wipe, and only if that slot is active), then
  `flash --rollback` after a FAIL, UNVERIFIED, experiment or calibration run,
  and `flash --promote <id>` after it has durably merged and tagged a batch.
  `--promote` itself looks the batch up the way `--recover` does and releases
  the fallback only with proof, against the PR set `--merge-started` froze,
  for every repo of the tested commit set: for a repo with a PR, the PR's
  head is the tested commit, the PR is merged into `river-22.2` of its own
  zrm625 repo and the tag is at its merge commit; for a repo without a PR,
  the tag is at the tested commit; in every repo the tagged tree is the
  tested commit's tree. A stale or incomplete `prs`/`tag_repos` record halts. With
  nothing merged or tagged, recovery rolls back only if `merge_started` is
  absent; with it, recovery halts. A baseline pair is never promoted (`phone-qa --baseline`
  closes it). A closed baseline's seed is saved in `slot-state.json` in the
  same write as the slot change; if `known-good.json` didn't follow, no batch
  may flash until `flash --recover` finishes it. `flash` exits 3 when the
  pipeline must halt for Zak (including a guard failure during rescue or
  rollback, and a failed fb_mode recovery), and 4 when a boot failed and the phone was rescued back to the
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

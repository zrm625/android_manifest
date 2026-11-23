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
$S/phone-qa stage <id> [--kind batch|baseline|calibration|experiment] [--experiment]
$S/flash --batch <id> --wipe            # hash checks, slot pair, first-boot evidence
$S/bench/device-bench current --boots 1 --batch <id>
# boots 2-3 when the charter calls for them (see DESIGN.md, step 4)
$S/bench/device-bench current --boots 2 --batch <id>
$S/phone-setup --wifi SSID_FILE PSK_FILE
$S/phone-qa --batch <id>
```

`phone-qa --batch` exits 0 only if every required check passed. It writes
`$BENCH_HOME/qa/runs/<run-id>/results.json` and `report.html`. The tester
merges only when the exit code is 0, `eligible` is `true`, the mode is
`batch`, and the batch ID matches. Exit 1 means a check FAILed. Exit 2 means
at least one check is UNVERIFIED.

After a FAIL or UNVERIFIED result, roll the phone back to the known-good slot
with `flash --rollback`. Rollback wipes data, so run `phone-setup` after it.

## Modes

| Command | What it does | Eligible? |
| --- | --- | --- |
| `phone-qa --quick [--wait]` | Health group on the live phone | never |
| `phone-qa --batch ID` | Every check | if all pass and the controls qualified |
| `phone-qa --batch ID --only X,Y` / `--skip X,Y` | A subset (check IDs or groups) | never |
| `phone-qa --calibrate --batch ID` | Every check; regression only records | never |
| `phone-qa --baseline --batch ID` | Identity and health only; on a clean run writes `known-good.json` and closes the baseline pair | never |
| `phone-qa --control NAME --batch ID` | One deliberately broken run from the control set | never |
| `phone-qa boot-timing` | Calibration step 1: the bootloader allowance A per reboot path | n/a |
| `phone-qa controls` | Which controls are qualified for this harness version | n/a |
| `phone-qa stage ID` | Copy a build's images into `$BENCH_HOME/batches/ID` | n/a |
| `phone-qa helper` | Build or verify the helper APK | n/a |
| `phone-qa journal` | Show anything the state journal still has to restore | n/a |
| `phone-qa version` | The harness version (a content hash) | n/a |

Groups: `preflight`, `identity`, `health`, `radios`, `hardware`, `system`,
`apps`, `cleanup`, `final`. The canonical list of required check IDs is
`GROUPS` in `lib/riverqa/results.py`.

## Calibration and bootstrap

The steps run in this order (DESIGN.md, "Calibration and bootstrap"):

1. `phone-qa boot-timing` on the running build, with no flash. It writes
   `$BENCH_HOME/qa/boot-timing.json`. Until both proofs pass for a reboot
   path, the uptime signal on that path is UNVERIFIED.
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
4. Thresholds: write the calibrated values into `thresholds.json`. They never
   go below the hard floors in `features.FLOORS`.
5. Controls: run every control in `phone-qa controls`. Until all of them
   qualify for the current harness version, every run reports
   `eligible: false` with "controls not qualified".

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
| `$BENCH_HOME/qa/` | `known-good.json`, `boot-timing.json`, `controls.json`, `journal.json`, `wipes.jsonl`, `history.jsonl`, `runs/` |

## Interfaces for the batch runner

- **The lock.** Take `$BENCH_HOME/.device-lock` with `flock`, write a fresh
  token into the file, and export `BENCH_LOCK_TOKEN`. `scripts/device-lock`
  does exactly this. Child tools reuse the lock only if the token matches.
- **`batch.json`.** `phone-qa stage` writes it. The schema is in the docstring
  of `lib/riverqa/stage.py`. The runner adds `prs`, `tag` and `tag_repos`
  when it opens PRs, so recovery can look them up. It also adds
  `camera_set_change` when Zak approves a camera change. `flash` adds `pair`
  and `first_boot_seconds`. `device-bench` appends to `bench_runs`.
- **`slot-state.json`.** `flash` owns it (schema in `lib/riverqa/slots.py`).
  The runner uses `flash --recover` at the start of every batch, then
  `flash --rollback` after a FAIL, UNVERIFIED, experiment or calibration run,
  and `flash --promote <id>` after it has durably merged and tagged a batch.
  `flash` exits 3 when the pipeline must halt for Zak, and 4 when a boot
  failed and the phone was rescued back to the known-good slot.
- **`known-good.json`.** `phone-qa --baseline` seeds it. After a clean tag,
  the tester updates it with `regression.update_on_tag()`.
- **Rerun once.** A spurious extra-boot FAIL (this phone has reset on its
  own) is settled by rerunning the batch once. That rule belongs to
  `change batch`.

# Design: `phone-qa`, the QA harness for river batches (revision 11)

Status: **approved by the council in round 11** (Opus 9, Sol 9, Gemini 9). This is revision 11, plus [amendment A2](#amendment-a2-drop-the-pstore-reset-anchor-and-add-an-immediate-reboot-bench-path) and its [chain rule A2.1](#amendment-a21-the-nonce-proves-the-chain-not-the-stamp). A2 replaces amendments [A1](#amendment-a1-the-adb-path-uptime-clock) and [A1′](#amendment-a1-reconcile-the-strict-anchor-with-the-phone-data). A1 and A1′ are kept below as history only. The implementation notes from each round are at the end and must be applied during the build.

## Council review

Rounds: 11

| Delegate | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 | R10 | R11 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Opus 5.5 | 5 | 7 | 8 ✓ | 8 ✓ | 9 ✓ | 9 ✓ | 9 ✓ | 9 ✓ | 9 ✓ | 9 ✓ | 9 ✓ |
| GPT-6.2 Sol | 4 | 6 | 7 | 7 | 7 | 8 | 8 | 8 | 8 | 8 | 9 ✓ |
| Gemini 4 Neon | 5 | 7 | 8 | 8 | 9 ✓ | 9 ✓ | 8 | 8 | 8 | 9 ✓ | 9 ✓ |

✓ = approved. Round 11 was a narrow confirmation round: all three reviewers checked only the diff from revision 10, which Opus and Gemini had already approved.

Round 11 implementation notes (Sol, Opus, Gemini):

- Put "controls qualified" in the main merge-eligibility checklist.
- Judge each control by its table row's expected result. For the two interruption controls, the expected result is a successful recovery.
- Only control mode can mark a fastboot enumeration as planned. A `--batch` run that finds any control override must refuse to run, or write FAIL.
- Add a second extra-pass control without the planned marking. It must FAIL through the unplanned-enumeration rule.
- Each control records which check failed, not only that it failed.
- "Harness version" is a content hash of `phone-qa`, the helper sources, the thresholds, the allowance A and the known-good seed.
- The step-1 boot-timing proofs are part of the control set.

## Why we need it

The batch pipeline in `CHARTER.md` (merged as manifest PR #9) merges a batch
automatically when it is clean. Clean includes passing QA on every item of the
must-work list. `scripts/phone-check` mostly checks that things are present. A
check like that would let a broken camera, speaker or sensor auto-merge.

`phone-qa` must:

- show that each must-work feature works;
- prove that the evidence comes from the exact batch under test;
- run with no one at the desk;
- never let an incomplete or uncertain run authorize a merge.

## Context

- **The phone.**
  - Moto G7 `river`, model XT1962-1 (US), serial `ZY225DPT7F`.
  - Connected over USB to the workstation `forge` (bus 8, root port; USB ID
    `18d1:4ee7` in adb mode). Always on the charger.
  - LineageOS 22.2 (Android 15) userdebug, kernel 4.9.337.
  - Timezone `Pacific/Midway`.
  - No NFC chip, no SIM, no telephony, no screen lock.
- **The build fingerprint can't tell builds apart.** `ro.build.fingerprint` is
  the stock Motorola fingerprint for every build. The `*.build.date.utc`
  properties can.
- **Decisions in the charter (binding).**
  - The must-work list. The apps on it are Brave, Tailscale, T3 Code and Perch.
  - Deep sleep and the headphone jack are not checked.
  - Rear main camera: a photo with the torch that isn't black. Front and depth
    cameras: liveness only.
  - Boot: `host_boot_seconds` of the first `device-bench` boot after the flash
    must be 55 s or less. The first boot after the wipe is only recorded.
  - RAM:
    - The reference is the lowest known-good 3-boot median.
    - If a single boot is more than 10 MB over the reference, measure 3 boots.
    - Fail if that median is more than 5 MB over the reference.
    - At or under 1,050 MB, always measure 3 boots.
  - Size: `/system` and `/vendor` may not grow compared with the latest
    known-good build, unless the batch's RAM median is below the reference.
  - Low-RAM mode is an experiment and never merges.
- **Facts checked on the phone during the round-2 fixes.**
  - Turning Extra dim on changes SurfaceFlinger's `colorTransformMatrix`. The
    diagonal goes from about 1.0–1.09 to about 0.54–0.57.
  - Brave 1.100.58 (arm64), Tailscale 1.104.7 and T3 Code 2.3.0 (815) each
    installed and opened on the phone, then were removed.
  - With the screen off, the phone dozes. `config_dozeAfterScreenOffByDefault`
    is true, so `mWakefulness` stays `Dozing` and never becomes `Asleep`.
  - River has no AVB vbmeta (`ro.boot.vbmeta.digest` is empty), so nothing on
    the phone identifies the flashed images by itself.
  - The phone (192.168.4.60) and forge (192.168.4.44) are on the same Wi-Fi
    LAN.
  - The phone hard-reset twice during review. Boot reason: `hardware_reset`.
    pstore had no panic.

## The batch sequence

`phone-qa` is one step. The batch runner (`change batch`, a separate change)
owns the whole sequence and holds one lock across all of it.

1. **Stage.** Copy the build's images into `$BENCH_HOME/batches/<id>/images/`
   and record a SHA-256 for each image. Also record:
   - the commit set (each repo's SHA);
   - `ro.build.date.utc`, `ro.vendor.build.date.utc` and
     `ro.bootimage.build.date.utc`;
   - the expected camera set, from the switch values in the device repo at the
     batch commit;
   - `experiment: true|false`.
   - a unique build ID, `<batch id>-<short hash of the commit set>`. The batch
     build passes it in as `RIVER_BUILD_ID`. The device repo writes it to two
     separate properties: `ro.river.build_id` in `/system/build.prop` and
     `ro.vendor.river.build_id` in `/vendor/build.prop`. Two names are needed
     because Android loads every partition's properties into one map, so a
     single shared name would let one partition's value hide the other's. The
     stamping is a small device-repo change that ships together with the
     harness, before calibration.
   - copies of `installed-files.json` and `installed-files-vendor.json`, with
     their hashes. The NFC build check reads these copies, never the mutable
     `out-stripped`.

   All of this goes into `batch.json`.
2. **Flash** from the staged directory. `flash --images <dir> --wipe` checks
   every hash before it writes, and records the slot it wrote. The time of the
   first boot after the wipe is recorded in `batch.json` for information. The
   flash record gets its own attempt ID and notes that the wipe succeeded.
   Every adb and fastboot wait has a hard deadline.

   **The slot pair is fixed before anything is written.** Before the first
   write, `flash` saves the slot pair to `$BENCH_HOME/slot-state.json` and
   `batch.json`:
   - `target`: the slot this batch writes;
   - `fallback`: the slot that keeps the last good build;
   - the SHA-256 of the fallback slot's whole boot partition, taken before the
     flash.

   The pair is never worked out again from fastboot's `current-slot`,
   because that may already point at the candidate after `--set-active`.
   **Retries follow one rule.** A retry that writes images first completes a
   verified rollback to `known_good_slot`. It then writes to the same
   `target` saved in its own pair. The "no unresolved pair" check below
   applies to pairs left by other batches. A batch's own pair is reused
   only after that verified rollback.

   **`slot-state.json` also keeps `known_good_slot` permanently.**

   **First-run transition.** The file doesn't exist before the first
   baseline. The first `--baseline` run therefore starts by recording the slot
   running now as a *provisional* `known_good_slot`, together with the SHA-256
   of that slot's whole boot partition, checked live against the phone. Then
   it flashes the baseline to the other slot under the normal pair rules.
   - If the baseline passes, its own pair is closed at once. There are no PRs
     to check. `known_good_slot` becomes the baseline's slot, with its build
     ID and boot hash confirmed and saved.
   - If it fails, rollback returns the phone to the provisional slot.

   Perch calibration runs only after this checkpoint. After that,
   `known_good_slot` is always the slot holding the latest tagged build. A batch's pair
   is resolved in one of two ways only:
   - **Rolled back.** This happens after a FAIL, an UNVERIFIED result, an
     experiment, a calibration run of a candidate, a slot rescue, or a
     bootloader fallback the harness detects. An interrupted run goes through
     the recovery rule below, which may roll it back or promote it. There is
     only one rollback procedure, and rescue uses it too:
     1. Set the batch's `fallback` slot (always `known_good_slot`) active with
        `fastboot --set-active`.
     2. Run `fastboot erase userdata`. It must succeed, and the wipe is
        recorded for the journal. This is the erase `scripts/flash` uses:
        river's bootloader rejects `fastboot -w`, because it also tries to
        erase a `metadata` partition the phone doesn't have. The old build
        then never starts on data the candidate wrote.
     3. Reboot, then check the slot and the build: the build ID, or the saved
        boot hash.

     Only after that check is the pair released.
   - **Promoted.** The tester has merged and tagged the batch, and recorded
     that durably. Before saving, the runner confirms the candidate slot's
     build ID and records its boot hash. Then `known_good_slot` becomes the
     candidate's slot.

   **Recovery checks GitHub first.** At the start of any batch, if a pair is
   still unresolved, the runner looks up that exact batch's merge and tag
   state with `gh` and `git ls-remote` for the tag in every repo:
   - Before any merge or tag action the runner records `merge_started` in
     `batch.json` (`flash --merge-started`). That write also freezes the
     complete expected PR set (code review round 3): every repo of the
     tested commit set, each with its PR number (`prs`) or an explicit "no
     PR" (`no_pr`). `--merge-started` refuses, and the runner must not merge,
     if any repo has neither. The set can't change afterwards: a later
     `prs`/`no_pr` that gives a different set (a repo dropping from "PR" to
     "no PR", say) is refused and halts recovery. If nothing is merged or
     tagged and there is no `merge_started`, the run stopped before merging
     and the batch is rolled back. With `merge_started` it halts.
   - The batch is promoted only with proof, against the frozen set, for every
     repo of its tested commit set (`batch.json` `commit_set`): for a repo
     with a PR, the PR's head is the tested commit, the PR is merged into
     `river-22.2` of that repo's own zrm625 fork, and the tag points at its
     merge commit; for a repo without a PR, the tag points at exactly the
     tested commit. In every repo the tagged commit's tree must be the
     tested commit's tree, which catches a merge into a branch that had
     moved on (content that was never tested). Without a frozen set,
     nothing is promoted. A stale or incomplete `prs`/`tag_repos` record
     halts. `batch.json` and `slot-state.json` writes are atomic and
     durable: the file is fsynced, renamed into place, and the directory is
     fsynced after the rename.
   - If only some are, the runner halts and notifies Zak.
   - Pairs that never have PRs (baseline before it passes, calibration,
     experiments) always roll back.
   - If the lookup itself fails, for example because the network or `gh` is
     down, the runner halts. A failed lookup never counts as "none merged".

   The pair stays reserved until that decision is recorded durably. This
   applies to every unresolved pair, not only after a host crash. If a
   rollback fails, the runner halts and notifies Zak.

   Until one of these happens, the pair stays in `slot-state.json`. Before
   any flash, the runner checks three things:
   - there is no unresolved pair from another batch;
   - the running slot is `known_good_slot`;
   - the inactive slot, which is the write target, is not `known_good_slot`.



   A failed boot during the flash or during any `device-bench` boot starts
   slot rescue:
   1. Get to fastboot. If fastboot already answers, use it. If adb answers
      while `sys.boot_completed` is still unset, run `adb -s <serial> reboot
      bootloader`.
   2. Run the rollback procedure above: set the fallback active, run
      `fastboot erase userdata` and record the wipe, then reboot.
   3. Check that the fallback is up. Check the slot suffix. Then
      check its build ID if it has one; otherwise compare the SHA-256 of its
      whole boot partition with the hash saved before the flash. The build
      running now has no stamp, so the hash is the check until a stamped
      build is the fallback.
   4. Mark the batch failed.

   A completed slot rescue counts as **Rolled back** under the rules above.
   Nothing else releases the pair. If the bootloader falls back to the old
   slot by itself, the slot and build-ID checks catch it, and the batch is
   reported failed. If neither adb nor fastboot can be reached, the pipeline
   stops and Zak is notified.

   When `flash` sees `sys.boot_completed=1`, it records that boot's `boot_id`
   in the flash record at once. Before `device-bench` reboots, the runner saves
   the same health evidence for that first boot as for the measured boots,
   running `adb root` first. That evidence is `boot_id`, the boot reason,
   `dmesg` service deaths and restart loops, `logcat -b crash`, the SELinux
   mode, and `sys.boot_completed`. The record carries the flash attempt ID and
   the `boot_id` read at collection time. Collection is complete only if that
   `boot_id` equals the one `flash` recorded. If it differs, a reset happened
   in between, and the evidence is from the wrong boot. In the same way,
   `device-bench` records each measured boot's `boot_id` when it sees boot
   completed, and that boot's evidence has to match it. QA gates on all of
   this the same way. The 55 s limit still applies only to bench boot 1.
   `boot_id` and `sys.boot_completed` are always read in the same shell
   command.

   **Catching a reset before boot completes.** A matching `boot_id` can't
   show that no other boot came before the one that completed. Three
   separate signals cover that gap:
   - **Kernel-log chain.** Before every planned reboot, the harness writes a
     short marker token, made from the attempt number and a few characters of
     `boot_id`, to `/dev/kmsg` as root. It writes the token several times,
     because ramoops on this phone has no ECC and flips bits. Matching
     tolerates a few flipped characters, and calibration records the
     corruption rate. That
     includes the reboot into the bootloader for flashing and every bench
     reboot. At each boot completion it reads
     `/sys/fs/pstore/console-ramoops*`, which holds the previous kernel's log.
     That log must end with the latest leaving marker. If a different kernel
     session shows up in between, for example a log that ends a few seconds
     into a boot, that is a FAIL. If pstore is missing or too corrupt to read
     the marker, the check is UNVERIFIED. Calibration tests whether the chain
     survives each kind of reboot, including the trip through fastboot. A
     reboot kind where it doesn't survive is written down, and those boots
     rely on the other two signals.
   - **Uptime.** The clock starts at the first USB disconnect after the
     harness's reboot command, which leaves Android's variable shutdown time
     out of it. On the `adb reboot` path river's USB gadget drops seconds
     before the reset, so [amendment
     A2](#amendment-a2-drop-the-pstore-reset-anchor-and-add-an-immediate-reboot-bench-path)
     records uptime there as INFO only and adds the immediate `sysrq`
     reboot path, which has the gate. Any fastboot enumeration the harness
     didn't plan counts as an extra boot. At boot completion, `/proc/uptime` must be at least the time
     since that disconnect, minus a bootloader allowance A set by calibration:
     - A must be at least the longest normal bootloader pass calibration
       measured;
     - A must be less than the shortest normal pass plus one more bootloader
       pass.

     Any reset, even one too early for ramoops to record, adds at least one
     more bootloader pass, so the uptime comes up short. If no value of A
     meets both conditions, that reboot path is UNVERIFIED.
   - **Boot count.** After a wipe, dropbox starts empty. The number of
     `SYSTEM_BOOT` entries must equal the boots expected since the wipe. The
     entry is written after `BOOT_COMPLETED`, so the count is read only once
     it has settled. Dropbox entries are compared by name and count, never by
     timestamp: on this phone every entry carries the same time.

   Any positive sign of an extra boot is a FAIL. This phone has hard-reset
   on its own before, so now and then this may fail a good batch. A rerun
   settles that, and it is better than a false pass. The `sys.boot.reason`
   string is recorded but not trusted. It differs from boot to boot on this
   phone and isn't needed by any rule.
3. **Bench boot 1.** `device-bench current --boots 1 --batch <id>`. Under
   amendment A2 this run also measures bench boot 2, the `sysrq` boot that
   every batch needs. The setup wizard is still untouched. Before any later reboot, `device-bench` records
   health evidence for the boot it just measured. Each boot row gets a unique
   key (`run_id` plus `boot`). The evidence is:
   - `boot_id` and `sys.boot.reason`;
   - `logcat -b crash`;
   - service deaths and restart loops from `dmesg`;
   - SELinux mode and `sys.boot_completed`.

   `device-bench` runs `adb root` first. The evidence goes into
   `$BENCH_HOME/batches/<id>/boots/<row key>/`. QA fails the batch on any of
   these in any measured boot:
   - a crash;
   - a restart loop;
   - SELinux not enforcing;
   - `boot_completed` not equal to 1.

   If a boot's evidence is missing, the check is UNVERIFIED.
4. **Bench boots 2–3, when the charter calls for them.** The runner decides
   before `phone-setup`, so every boot is measured in the same untouched state.
   Boot 2 already exists (amendment A2), so it runs `device-bench current
   --boots 1 --batch <id>` for boot 3 if any of these is true:
   - boot 1 is more than the reference + 10 MB;
   - boot 1 is at or under 1,050 MB;
   - `/system` or `/vendor` grew compared with the latest known-good build (so
     the RAM exception can be claimed);
   - there is no reference yet, or `ram_reference` is still the calibration
     seed.

   The median is boot 1 plus these 2 boots. All three run IDs go into
   `batch.json`. MB here means `used_ram_kb / 1024`.
5. **`phone-setup --wifi ...`.** It turns location off, Extra dim on and
   gesture navigation on.
6. **`phone-qa --batch <id>`.**

`device-bench` gets new columns: `build_date_utc`, `vendor_date_utc`, `slot`
and `batch`. The harness accepts a row only if all four match `batch.json` and
the flash record.

**The shared lock.** The runner takes `$BENCH_HOME/.device-lock` and exports
`BENCH_LOCK_TOKEN`. `flash`, `device-bench`, `phone-setup` and `phone-qa`
reuse the lock when the token matches, and refuse to run otherwise.

## Result states and the merge gate

| State | Meaning | Merge-eligible? |
| --- | --- | --- |
| `PASS` | Seen working on this batch | yes |
| `FAIL` | Seen broken, or the evidence contradicts the batch | no |
| `UNVERIFIED` | No verdict was possible: missing fixture, unreadable diagnostics, no helper result, or something only Zak can decide | no |
| `INFO` | Reported only. Used for non-must-work data, such as photos from the front and depth cameras | n/a |

- The harness holds a canonical list of required check IDs. A run passes only
  if every required ID is `PASS`, so a harness bug that drops a check can't
  pass.
- **Exit codes:**
  - 1 if any check is `FAIL`. This wins over `UNVERIFIED`.
  - Otherwise 2 if any check is `UNVERIFIED`.
  - Otherwise 0.
- **Merge eligibility.** A run is merge-eligible (`eligible: true`) only if all
  of these hold:
  - it ran in `--batch` mode with the full check set (no `--only`, `--skip`,
    `--calibrate` or `--baseline`);
  - `batch.json` has `experiment: false`, and the phone's `ro.config.low_ram`
    is not `true`. If `ro.config.low_ram` is `true` while `experiment` is
    `false`, that is a FAIL;
  - every helper test returned a terminal result;
  - the build identity matched;
  - `boot_id` didn't change;
  - the final checks, which run after cleanup, passed.
- **The tester's rule.** The tester merges only when all of these are true:
  exit 0, `eligible: true`, batch mode, and the batch ID matches.
- **`--quick`.** A quick run (health group only, for the edit-flash loop)
  exits 0 when the health checks pass. It always reports `eligible: false` and
  skips the regression checks.

## Pieces

1. **`scripts/phone-qa`.** Python 3, standard library only, run as
   `python3 -I`. It:
   - runs `adb root` and `wait-for-device` once, before any `adb reverse`;
   - checks the serial and the `river` guard before every group;
   - writes `results.json`, `report.html` and the artifacts to
     `$BENCH_HOME/qa/<run-id>/`;
   - puts every adb call under a deadline.
2. **The helper APK, `river-qa`.** Plain Java, with no Gradle and no
   dependencies. The source lives in `.repo/manifests/qa/helper/`.
   - **Build:** offline, against `platforms/android-36/android.jar`, using
     aapt2, javac, d8, zipalign and apksigner from build-tools 37. Signed with
     the debug key. The cache key covers the sources and the toolchain
     versions.
   - **Structure:** an `android.app.Instrumentation` subclass, plus activities
     that the checks bring to the foreground.
   - **Foreground rule:** every device-side check runs while a helper activity
     is resumed in front with the screen on, and the harness confirms that
     each time with `dumpsys activity top`.
   - **One UiAutomation at a time:** only one UiAutomation connection can exist
     at once. Host `uiautomator` commands never overlap an instrumentation run.
   - **Normal permissions,** declared in the manifest:
     - `INTERNET`
     - `ACCESS_NETWORK_STATE`
     - `CHANGE_NETWORK_STATE`
     - `VIBRATE`
     - `USE_BIOMETRIC`
     - `USE_FINGERPRINT`
     - `com.android.alarm.permission.SET_ALARM`
   - **Runtime permissions,** granted with `pm grant` after install:
     - `CAMERA`
     - `RECORD_AUDIO`
     - `ACCESS_FINE_LOCATION`
     - `BLUETOOTH_SCAN` (declared `neverForLocation`)
     - `BLUETOOTH_CONNECT`
   - **Foreground scope.** The foreground rule covers only helper operations.
     Turning the screen off on purpose and showing another app on purpose
     (DeskClock, Brave, Tailscale, T3 Code) are allowed.
   - **Uninstall order.** The helper is uninstalled last, after every cleanup
     step that needs it, such as unbonding and restoring the scan mode.
   - **Privileged Bluetooth calls:** the instrumentation adopts shell's
     `BLUETOOTH_PRIVILEGED` only around those
     calls (`setPairingConfirmation`, `setScanMode`, `removeBond`), using
     `UiAutomation.adoptShellPermissionIdentity`.
   - **Hidden API:** `BluetoothDevice.removeBond()`, `BluetoothAdapter.setScanMode()` and `setDiscoverableTimeout()` are hidden or system APIs. The helper calls them by reflection, with `hidden_api_policy` relaxed through the journal while they run.
   - **Cleartext:** a network-security-config allows cleartext to exactly two
     hosts: `127.0.0.1` and forge's LAN address. The LAN address is filled in
     when the helper is built and is part of the cache key.
3. **`phone-check` folds into `phone-qa`** and is deleted in the same change.
   Every caller and doc moves to `phone-qa --quick`.
4. **Fixtures.** They live in `$BENCH_HOME/qa/fixtures/`, each pinned by
   SHA-256 in `qa/fixtures.json`:
   - `Bravearm64Universal.apk` 1.100.58
   - `tailscale-android-universal-1.104.7.apk`
   - `t3code-android-internal-2.3.0-815.apk`
   - a 5 s H.264 `testsrc2` clip, with reference frames generated by ffmpeg
   - the test page

   The host serves the page over `adb reverse`. Each run, and each consumer
   (WebView and Brave), gets its own token. The host logs every request with
   its path, token, User-Agent and timestamp.

   A missing fixture or a hash mismatch makes preflight `UNVERIFIED`, and
   nothing else runs. The charter says an app whose APK can't be found any
   more is dropped from the list in a charter change.
5. **The state journal,** `$BENCH_HOME/qa/journal.json`. Before any change is
   made, the journal records the original value. Each entry is tagged with
   where it lives:
   - **Phone entries,** tagged with the flash attempt ID. A phone entry is
     dropped only when the flash record shows a successful wipe after that
     attempt. Otherwise it is restored, even after a reboot or a failed flash.
   - **Host entries:** Bluetooth adapter state, gvfs mounts and the `adb
     reverse` ports. These are always restored.
   - **Resources the run created:** the alarm label, the marker file, the
     on-phone watchdog PID and the Bluetooth bond. These are cleaned up by
     identity.

   Journal recovery runs only after the lock is held and the guards have
   confirmed the phone. A leftover alarm that was dismissed before it fired is
   still in DeskClock's database. Recovery clears it with
   `pm clear com.android.deskclock`; the test phone has no alarms of its own.
6. **History,** `$BENCH_HOME/qa/history.jsonl`. It is written only in batch
   mode, and is used for reports only.

## Run structure

1. **Preflight.**
   - Check the lock token, the guards and the fixture hashes.
   - Restore anything left in the journal.
   - Build or verify the helper.
   - Record `boot_id`, the uptime, and the PIDs and start times of
     `system_server`, `cameraserver`, `audioserver`, the vendor camera
     provider, the audio HAL and the fingerprint HAL. Start times come from
     `/proc/<pid>/stat`.
   - Start streaming `logcat -b crash,kernel,main` to an artifact file.
2. **Identity.** Every one of these is a FAIL on mismatch:
   - `ro.river.build_id` read directly from `/system/build.prop`, and
     `ro.vendor.river.build_id` read directly from `/vendor/build.prop`, both
     as root, against `batch.json`'s build ID;
   - the SHA-256 of exactly the first `size(boot.img)` bytes of
     `/dev/block/by-name/boot<slot>`, read as root, against the staged
     `boot.img`. A short read is a FAIL. The partition is 33,554,432 bytes and
     today's image is 29,327,360, so hashing the whole partition would never
     match;
   - the phone's `ro.build.date.utc`, `ro.vendor.build.date.utc`,
     `ro.bootimage.build.date.utc` and `ro.boot.slot_suffix` against
     `batch.json` and the flash record;
   - the benchmark rows (matched by run ID, batch, dates and slot);
   - the image hashes recorded by `flash`.
3. **Health baseline.**
   - `sys.boot_completed` is 1.
   - SELinux is enforcing.
   - The saved per-boot evidence, for the first boot after the wipe and for
     every measured boot, shows no crashes, no restart loops, SELinux
     enforcing and boot completed. Each record's `boot_id` must equal the one
     recorded when that boot completed: by `flash` for the first boot after
     the wipe, and by `device-bench` for the measured boots. Missing evidence,
     or a `boot_id` mismatch, is UNVERIFIED.
   - `dumpsys dropbox` has no entry (counting all of them, with no wall-clock
     filter, because `/data` was wiped) tagged
     `system_server_crash`, `system_app_crash`, `data_app_crash`,
     `SYSTEM_TOMBSTONE`, `system_server_native_crash`, `*_anr` or
     `SYSTEM_LAST_KMSG` with a panic. Dropbox survives the bench reboots.
   - `/data/tombstones` is empty.
   - `/sys/fs/pstore` has no panic or oops.
   - No service is crashing or restarting in a loop in `dmesg`.
   - No telephony features.
   - Unreadable diagnostics are `UNVERIFIED`.
4. **Regression.** See the table below.
5. **Feature checks.**
6. **Cleanup.**
   - Remove the Bluetooth bond on both ends, and restore the scan mode and
     Bluetooth on/off to their snapshots.
   - Remove the alarm. `ACTION_DISMISS_ALARM` only disables it, so also run
     `pm clear com.android.deskclock`.
   - Remove the marker file and kill the watchdog.
   - Uninstall the apps.
   - Restore everything from the journal.
   - Uninstall the helper, last.
   - Go home and turn the screen off.
   - Every cleanup step is verified. A failed cleanup is a FAIL.
7. **Final checks**, after cleanup:
   - Run the health checks again. Dropbox, tombstones, pstore, `dmesg` and the
     streamed logcat must show nothing new since preflight.
   - Each recorded process must have the same PID and start time.
   - `HAL deaths since last reboot: 0`.
   - `boot_id` must be unchanged.
   - The phone settings must equal `phone-setup`'s choices.

   Only then is `eligible` written.

## Retries

Only **environmental** outcomes are retried, and each check names its own:

- **GPS:** the engine started, but no satellite has Cn0 > 0 yet.
- **Bluetooth:** forge was not discovered within the scan window.

Each gets one retry, and the first attempt is reported. Functional errors are
never retried. These are exceptions, HAL or service errors, crashes, wrong
values, and timeouts inside an exercise.

## Checks

| Group | Check | Method | Pass rule |
| --- | --- | --- | --- |
| health | regression | Read the batch rows and `known-good.json`. That file has two separate records. `latest` is replaced at every clean tag: the tag, the commit set, the run IDs, `host_boot_seconds`, `system_used_kb`, `vendor_used_kb` and the camera set. `ram_reference` is a running minimum: the tag, the run IDs, the 3-boot median and `seed`. It changes only when a clean tag has a 3-boot median strictly lower than the stored one. The calibration seed is retired at the first real tag. The reference then becomes the lower of the seed's median and the tag's median, and keeps that value's provenance, so retiring the seed can never raise it. The `seed` flag is cleared either way. A tag with only one boot, or with a higher median, never changes it. | The charter rules. Boot 1 `host_boot_seconds` ≤ 55. **RAM:** if a 3-boot median is required (see the sequence) and missing, UNVERIFIED. If it is present, it must be ≤ reference + 5 MB. If boot 1 is used alone, it must be ≤ reference + 10 MB. No reference at all: UNVERIFIED. **Size:** `system_used_kb` and `vendor_used_kb` no bigger than `latest`, unless the 3-boot median is below `ram_reference`. In `--calibrate` mode, nothing is compared: the run records the 3 boots, the sizes and the camera set, and is never eligible. |
| radios | wifi | The helper calls `requestNetwork(TRANSPORT_WIFI)` and binds its sockets to that network. It fetches two things: a page from forge's LAN address (the host test server also listens on the LAN on a fixed port, with a fresh token per fetch and `Cache-Control: no-store`. forge's ufw allows that port only from the phone's address; the rule is set up once, with sudo through `request_secret`), and `https://connectivitycheck.gstatic.com/generate_204`. Then the harness turns the screen off and waits until `dumpsys power` shows `mWakefulness` of `Dozing` or `Asleep` and `dumpsys display` shows `Display State=OFF`. It waits 30 s, wakes the phone, and the helper fetches both again with new tokens. | Each time: the page body has that fetch's token, the host log shows the request came from the phone's Wi-Fi address, and `generate_204` returns HTTP 204. |
| radios | bluetooth | **Phone:** `cmd bluetooth_manager enable`, then `wait-for-state:STATE_ON`, then read `settings get secure bluetooth_address`. **Host:** run a dedicated `bluetoothctl` subprocess with its own `agent NoInputNoOutput`, and **don't** make it the default agent, so the desktop agent is left alone. Snapshot the adapter's `Discoverable` setting. Remove any existing host bond for the phone's address; that address is only ever the test phone, so the bond is QA's. **Discovery:** turn `discoverable on` on the host. The helper, in front, scans and must find forge's address. Then restore `Discoverable`. **Making the phone visible:** the helper snapshots the scan mode in the journal, then sets `SCAN_MODE_CONNECTABLE_DISCOVERABLE` for 120 s with `setScanMode` (reflection plus shell `BLUETOOTH_PRIVILEGED`). The host session runs `scan bredr` until the phone's address appears, with a deadline, then `scan off`. **Pairing:** started from the host (`pair <phone>`) and accepted by the session's own agent. On the phone, a high-priority receiver for `ACTION_PAIRING_REQUEST` accepts the request only for forge's address and variant 3 (CONSENT, which also covers Just Works; written as a literal because the constant is hidden). It calls `setPairingConfirmation(true)` and then `abortBroadcast()`, so Settings doesn't show its own dialog. Afterwards the scan mode and Bluetooth on/off go back to their journal snapshots. `setScanMode(int)` takes one argument; the 120 s comes from `setDiscoverableTimeout`. Both `setScanMode` and `removeBond` are hidden APIs, so `hidden_api_policy` is relaxed through the journal while they run. **Cleanup:** `removeBond()` on the phone and `remove <phone>` on the host, both verified. | Forge found in a new scan. The phone reports `BOND_BONDED` for forge, and the host shows `Paired: yes`. Cleanup is verified on both ends. The host's `Discoverable` matches its snapshot. The phone's scan mode and Bluetooth on/off match theirs. |
| radios | nfc | **On the phone:** the `com.android.nfcservices` APEX is active under `/apex`. Tag (`com.android.apps.tag`) is installed. `/vendor/bin/hw/android.hardware.nfc@1.2-service` and its init `.rc` exist, as do `/odm/etc/permissions/sku_nfc/*.xml`, `/odm/etc/vintf/manifest_nfc.xml`, `/vendor/etc/libnfc-nxp.conf`, `/vendor/etc/libnfc-nci.conf` and `/vendor/lib64/nfc_nci_nxp.so`. **In the staged build:** the paths in the staged, hashed copies of `installed-files*.json`: `/system/apex/com.android.nfcservices.capex`, `/system/priv-app/Tag/Tag.apk`, `/vendor/odm/etc/permissions/sku_nfc/*.xml`, `/vendor/odm/etc/vintf/manifest_nfc.xml` and `/vendor/lib64/nfc_nci_nxp.so`. | All present. This is a build check, because the phone has no NFC chip. |
| radios | gps | Turn location on with `cmd location set-location-enabled true`, recorded in the journal. The helper, in front, calls `requestLocationUpdates(GPS_PROVIDER)` and registers `GnssStatus.Callback`. Location is turned off again afterwards. | `onStarted()` fires and status callbacks arrive. At least one satellite has Cn0 > 0 within 120 s; zero satellites gets one retry. Calibration must see satellites in 5 out of 5 runs, or this item goes to Zak before gating starts. |
| hardware | cameras | **Expected set:** taken from `batch.json`. It must equal `known-good.json`'s set and the phone's `ro.vendor.river.cameras` once that exists. The bootstrap set is {0, 1, 2}. Any difference between these sources is UNVERIFIED: "camera set changed, needs Zak". The one exception is a camera-set change Zak has approved. `batch.json` then carries `camera_set_change`, which links Zak's approval (the PR or comment URL) and gives the new set. `gh` must confirm the approval was written by `zrm625`. The new set must still match the device repo's switch values and `ro.vendor.river.cameras`. That set becomes the expected set, and the tester writes it to `latest` when the batch is tagged. A missing or malformed source is UNVERIFIED, never an empty set. `getCameraIdList()` must equal the expected set exactly. **Liveness (every camera):** with `MANUAL_SENSOR`, use the camera's maximum sensitivity and maximum exposure, with a matching frame duration. On this phone the longest is 487 ms, so 10 frames take under 5 s. The whole capture has a 20 s deadline. Without it, auto exposure. Capture 10 frames in the YUV format the camera offers. **Camera 0 photo** (only when camera 0 is in the expected set): take two JPEG stills with the sensitivity, exposure and white balance locked manually. The settings are chosen by calibration so a lit room doesn't saturate the torch-off still; they are separate from the maximum exposure used for liveness. The first still is taken with the torch off. The second is taken with `FLASH_MODE_TORCH` in the still request itself. Luma is measured on the decoded full-range 0–255 image. **Cameras 1 and 2 photos:** best effort. | **Liveness:** every camera delivers 10 frames with increasing timestamps, and the frame-to-frame difference is above a calibrated floor (hard minimum: more than 0). **Camera 0:** the torch-on still's capture result reports `FLASH_STATE_FIRED`. Its mean luma is higher than the torch-off still's by at least a calibrated amount, with a hard floor of 5 levels. The torch-on still itself has mean luma ≥ 20 and spatial standard deviation ≥ 4. The torch evidence and the photo are the same image. **Cameras 1 and 2:** photo numbers are INFO. A failed JPEG capture on those two is INFO, not FAIL. Only their liveness gates. |
| hardware | audio | The helper is in front. It sets `STREAM_MUSIC` to a fixed level, recorded in the journal. The record source is `UNPROCESSED` if supported, otherwise `VOICE_RECOGNITION`. It records 1 s of silence, then plays 1 kHz, a 300 ms gap, 2.5 kHz, a 300 ms gap, and 1 kHz. Once playback and capture have started, it checks `getRoutedDevice()`. | Routed devices are `TYPE_BUILTIN_SPEAKER` and `TYPE_BUILTIN_MIC`. The recording isn't all zeros. In every burst, the burst frequency is ≥ 15 dB over both the silence and the bins ±150 Hz away. The gaps drop ≥ 10 dB. The level is above a calibrated floor. |
| hardware | fingerprint | The helper calls `isHardwareDetected()`. The harness checks that the HAL process is alive and unchanged (PID and start time) and that `HAL deaths since last reboot: 0`. | All three |
| hardware | sensors | The helper is in front and the screen is on. It reads for 3 s. | The accelerometer gives ≥ 20 samples, with a median magnitude of 9.81 ± 1.5. Light gives ≥ 1 finite value ≥ 0. Proximity gives ≥ 1 value in [0, maxRange]. |
| hardware | vibration | The helper is in front and plays a `createWaveform` of 3 pulses. | `dumpsys vibrator_manager` has a `river-qa` entry newer than the request, with status finished. |
| hardware | screen | Wake the screen and bring the rotatable helper activity to the front. **Brightness:** mode 0, values 40 and 200, 1.5 s apart. **Extra dim:** set `reduce_bright_colors_activated` to 0, then 1. **Rotation:** `cmd window user-rotation lock 1`, then `lock 0`. Everything is recorded in the journal. | **Brightness:** the `lcd-backlight` sysfs value follows the setting's direction. **Extra dim:** `dumpsys color_display` reads `Reduce bright colors:` / `Activated:` as false, then true. SurfaceFlinger's `colorTransformMatrix` diagonal drops by at least 30% between off and on. This was seen on the phone: about 1.08 to 0.56. If the matrix can't be found, UNVERIFIED. **Rotation:** `mCurrentRotation=ROTATION_90`, then `ROTATION_0`, and the helper receives the matching configuration changes. |
| hardware | battery | `dumpsys battery` and `/sys/class/power_supply/battery/` | `present`. The level is 1–100 and within 1 of `capacity`. Status is 2, 5, or 4 when a power source is present. At least one source is powered. Voltage is 3,400–4,500 mV. |
| hardware | usb | Find the phone by `serial` under `/sys/bus/usb/devices/`. Push a marker with a token to `/sdcard/Download/`, then index it with `content call --uri content://media/external/file --method scan_file`. Save the current functions with `svc usb getFunctions`. It prints to stderr, and prints an empty line when only charging is on; empty means restore with no argument. **Watchdog:** `setsid nohup sh -c 'sleep 180; svc usb setFunctions <saved>' </dev/null >/dev/null 2>&1 & echo $!`, recorded in the journal. The harness checks the PID is alive. Then run `svc usb setFunctions mtp`, which keeps adb. Wait, under a deadline, for the device with this serial to reappear. **Listing:** if gvfs is running, wait up to 10 s for its mount and list through `gio` at the `mtp://` URI that contains this serial. Without gvfs, use `mtp-files` only if this phone is the only USB device with an MTP interface under `/sys/bus/usb/devices` (checked by serial). Otherwise the check is UNVERIFIED. Then restore the saved functions, confirm adb, kill the watchdog by PID, delete the marker and set up `adb reverse` again. | The marker is listed over MTP from this phone, and adb is back within 30 s. If adb is still missing after 240 s (past the watchdog), the run halts with "phone unreachable", the pipeline stops, and Zak is told. |
| system | gestures | `dumpsys activity service SystemUI` shows the overview proxy enabled and bound to `app.zak.perch`. HOME resolves to Perch. Press HOME: the top is Perch's `MainActivity`. Start DeskClock: the top is DeskClock. Swipe up from the bottom edge, using calibrated coordinates. **Back:** helper activity A starts child B, and B is on top. Then a left-edge back swipe. | After HOME and after the swipe up, the top is exactly Perch. After Back, the top is exactly A. |
| system | alarms | The helper reads the phone's local time. It sends `ACTION_SET_ALARM` with `EXTRA_SKIP_UI`, `EXTRA_RINGTONE=VALUE_RINGTONE_SILENT`, `EXTRA_VIBRATE=false`, a unique `EXTRA_MESSAGE` label recorded in the journal, and the next minute boundary at least 45 s away. The harness turns the screen off. Afterwards: `ACTION_DISMISS_ALARM` with `EXTRA_ALARM_SEARCH_MODE=ALARM_SEARCH_MODE_LABEL` and the label. Check the alarm is gone, then force-stop DeskClock. | Only evidence that the alarm is **firing** counts: a DeskClock notification on the firing channel, with `fullScreenIntent` and this label, read with `dumpsys notification --noredact`. `AlarmActivity` being resumed is supporting evidence; its intent carries the instance URI, not the label. The "upcoming alarm" notification doesn't count, because it already shows the label. That evidence must be **absent** at target − 10 s and present by target + 10 s. The alarm is deleted afterwards. |
| system | keyboard | Before the `EditText` takes focus, the helper sets `FLAG_RETRIEVE_INTERACTIVE_WINDOWS` through `UiAutomation.getServiceInfo()` and `setServiceInfo()`. The `EditText` uses an `InputConnection` wrapper that logs `commitText` and `setComposingText`. Once the LatinIME window is shown, the harness taps a fixed word at calibrated key coordinates (`input tap`). `input text` is never used. | `dumpsys input_method` shows LatinIME with `mInputShown=true`. The field holds the word, and the wrapper logged IME commits for it. |
| system | sideload | `pm install` the helper and each app. Launch each. Uninstall them at cleanup. | Every step succeeds and is verified. |
| system | webview | The helper's WebView has JavaScript on and loads the page with the WebView token. The page script sets `document.title` to the token and sends a beacon. | The title is the token within 15 s, and the host log has the beacon with the token. |
| system | video | The decoder is the first AVC decoder with `isHardwareAccelerated()`, not an alias, and not requiring `FEATURE_SecurePlayback`. Decode into an `ImageReader` surface, and compare sampled frames on the host. | The frame count matches. Sampled frames have PSNR ≥ 30 dB against the ffmpeg reference. The decoder name is reported. |
| system | time | `auto_time=1`. Wi-Fi is the active default network. Record `elapsedRealtime` (`E0`), then run `cmd network_time_update_service force_refresh`. | The stdout is `true`. Polling `dumpsys time_detector` for up to 5 s finds a network suggestion with an elapsed timestamp ≥ `E0`. The phone and host clocks agree within 2 s. |
| apps | brave | Install the pinned APK. Send a VIEW intent for the Brave-token page to `com.brave.browser`. Get through first-run using the steps calibration recorded for the pinned version. | Brave's activity is in front. The host log has the page GET and the page-script beacon for the Brave token. A uiautomator dump shows the token text on screen. |
| apps | tailscale | Install the pinned APK and launch `com.tailscale.ipn/.MainActivity`. | The activity is resumed, then 10 s go by with no crash or ANR for the package. Tailscale's VPN consent dialog counts as opened. It is dismissed and never accepted. |
| apps | t3code | Install the pinned APK and launch `com.t3tools.t3code/.MainActivity`. | The activity is resumed, then 10 s go by with no crash or ANR for the package. |

Not checked, by charter: the headphone jack and deep sleep. Auto-rotate (the
sensor path) isn't tested, only user rotation, and the report says so.

## Calibration and bootstrap

The known-good build on `river-22.2` still runs Trebuchet, and the gestures
check requires Perch. The steps run in this order, and each one finishes
before the next starts:

1. **Boot-timing calibration, with no flash.** On the build running now (a
   stamped build from this tree, dev-flashed first with `flash stripped
   --wipe` when the phone ran anything else: see "Code review round 9",
   R9-3, and "Code review round 10", R10-2), do
   planned reboots on each path that matters: the immediate `sysrq` reboot
   and a reboot through fastboot with no writes ([amendment
   A2](#amendment-a2-drop-the-pstore-reset-anchor-and-add-an-immediate-reboot-bench-path);
   the `adb reboot` path has no uptime gate, only a chain control). The clock for each starts at the
   first USB disconnect after the harness's command. For flashing, the clock
   starts at the disconnect after the final `fastboot reboot`, so the time
   spent writing images is never counted. From these reboots, set the
   bootloader allowance A for each path, using the rule in the uptime signal.
   For each path, show two things. A normal boot must pass. An injected extra
   bootloader pass must be rejected **by the uptime check itself**: for the
   control, the clock starts at the disconnect that begins the injected pass
   and runs through the final reboot. Expected fastboot enumerations from the
   control are marked as planned, so the "unplanned enumeration" rule can't
   fail the run first and hide a broken uptime check. A path with no workable
   A, or without that proof, is UNVERIFIED from then on.
2. **Baseline.** Seed `known-good.json` from the build that is already
   known-good, not from the Perch candidate. Otherwise Perch would become its
   own baseline.
   - Use the `--baseline` mode, starting with the first-run transition. It is
     never eligible. It uses the same slot pair and rescue as any flash. It
     doesn't run the functional checks, so it works on the Trebuchet build.
     It writes `known-good.json` only if every boot's health evidence is
     clean and bound to the right `boot_id`.
   - Build `river-22.2` HEAD, stage it, and flash it with a wipe. Check its
     identity: its build IDs if it has them, otherwise its dates, slot and
     boot hash. Record 3 `device-bench` boots with the setup wizard
     untouched, plus per-boot health evidence for the first boot after the
     wipe and for each measured boot.
   - Existing `device.csv` rows can be reused only if they have the same
     identity and health evidence. Today's rows don't, so the baseline is
     measured fresh.
   - `latest` gets that build's sizes, `host_boot_seconds` and camera set.
   - `ram_reference` gets its 3-boot median, marked `seed: calibration`.
   - **Closing the pair.** A passing baseline closes its own pair, and its
     slot becomes `known_good_slot`. An interrupted or failed baseline has no
     PRs or tags to look up, so it always rolls back to the provisional slot,
     and the baseline is run again.
3. **Perch calibration.** This step starts only after the baseline's pair is
   closed.
   - Build the Perch candidate (the `perch-launcher` branches, with Perch
     160d58f) and flash it through the staged-batch path, with
     `experiment: false` and no merge.
   - Run `phone-qa --calibrate` on it 5 times, all on one flash. The phone
     rolls back once, at the end. Calibrate mode runs every check, but it is
     never eligible.
   - Its regression check only records: the 3 boots, the sizes and the
     camera set. The camera set must match both `getCameraIdList()` and the
     device repo's switch values.
   - Every other check must pass in all 5 runs.
4. **Thresholds.** Set the thresholds from the Perch calibration data. Each
   threshold has a hard floor in the code:
   - audio ≥ 15 dB
   - PSNR ≥ 30 dB
   - luma standard deviation ≥ 4
   - camera frame difference > 0
   - Extra dim matrix drop ≥ 30%

   Calibration can never lower a threshold below its floor. The Perch runs
   set only the functional thresholds.
5. **Controls before any merge.** The deliberately broken runs below must
   all give their expected results before any run can be merge-eligible.
   Until they do, the harness writes `eligible: false` with the reason
   "controls not qualified". The control results are stored with the
   harness version, and changing the harness or the thresholds clears them.
   Only then do Perch's council review and the first real batch happen. That
   batch is compared with the baseline seed. Every
   batch measures 3 boots while the seed is in place. When the batch is
   clean, the tester tags it, and it becomes the first real known-good.
6. Show that each check can fail. Each of these deliberately broken runs must
   end in FAIL or UNVERIFIED. This list is the step-5 control set:

   | What is broken | Expected result |
   | --- | --- |
   | Audio volume 0 | FAIL |
   | Location off | FAIL (GPS) |
   | Software decoder forced | FAIL (video) |
   | `input text` instead of taps | FAIL (keyboard) |
   | Torch LED disabled below the HAL for the whole capture, even though the request still asks for the torch. A `led:switch_*` write alone doesn't hold: the torch request's next positive brightness write re-enables the switch (`leds-qpnp-flash-v2.c:1753`). So `max_brightness` is set to 0 on every `led:torch_*`/`led:flash_*` node (the LED core clamps the camera's trigger writes to it) and read back right after the capture; the control qualifies only if it still reads 0 then. | FAIL, because the two stills show no difference |
   | Bad NTP server, set **after** a successful sync, then reset with `reset_server_config_for_tests` | FAIL (time) |
   | Wi-Fi off | FAIL |
   | Host Bluetooth adapter rfkilled; the harness never powers the adapter itself | UNVERIFIED, because it's a fixture problem and not the batch's fault |
   | Wrong `batch.json` | FAIL |
   | Stale `device.csv` row | FAIL |
   | Wrong `ro.river.build_id`, or a boot hash mismatch | FAIL |
   | `ro.config.low_ram=true` with `experiment: false` | FAIL |
   | A crash planted in the evidence for an earlier measured boot | FAIL |
   | Wi-Fi page fetched over USB instead of Wi-Fi (socket not bound) | FAIL |
   | Missing 3-boot median when one is required | UNVERIFIED |
   | Beacon blocked | FAIL (webview) |
   | Extra dim matrix check run against a stub | FAIL |
   | Run killed mid-check, then run again | The journal restores the phone, and the next run is clean |
   | `boot_id` changed | FAIL |
   | Alarm checked 1 minute early | FAIL |
   | `navigation_mode` 0, or HOME pointed elsewhere | FAIL (gestures) |
   | Camera set changed in `batch.json` | UNVERIFIED (and a batch with an approved `camera_set_change` is never eligible) |
   | An extra reboot planted before boot completes (first boot, and the `sysrq` bench boot, where its uptime gate must reject it) | FAIL |
   | An extra full reboot after ramoops init on an `adb reboot` like bench boot 1 (amendment A2) | FAIL, through the kernel-log chain |
   | An extra bootloader pass with no kernel session (`fastboot reboot bootloader`, then `fastboot reboot` straight away), inside a planned reboot. The clock starts at the disconnect that begins the injected pass, and those enumerations are marked planned. | FAIL, through the uptime check itself |
   | Tag pushed, then the host killed before `slot-state.json` was updated | Recovery promotes the batch and does not roll back |

   The calibration report lists each result.

## Runtime

About 13 minutes: alarms up to 2, GPS up to 4, USB about 1, apps about 2, and
the rest about 4. The extra RAM boots are part of `device-bench`.

## Safety

- The harness only ever uses `-s $BENCH_DEVICE`, checks the `river` guard
  before every group, and runs under the shared lock token.
- On the host, it uses Bluetooth only with the phone's address. It never
  replaces the default agent. Only the `Discoverable` setting is changed, and
  it is restored from a snapshot.
- Before the USB switch, a detached on-phone watchdog with a known PID is
  armed. adb is never dropped on purpose. If adb is lost, the run halts and
  Zak is told.
- Alarms are silent with no vibration, and the audio bursts are short and
  moderate, because Zak works at that desk.
- Network traffic: the LAN test listener on forge (one fixed port, open only to the phone's address through ufw), the `generate_204` fetches, NTP, and whatever the three apps
  fetch on first launch. Brave's first-run screens and the Tailscale and T3
  Code start screens make requests to their own services. The helper builds
  offline.

## Round-2 findings and how revision 3 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| The boot gate measures the wrong boot (Sol), and the "first boot" wording (Opus) | The charter now says the first `device-bench` boot after the flash. The first post-wipe boot is recorded as information. Every adb wait has a deadline. |
| Exact batch identity: image hashes and the CSV batch field (Sol). Vendor and boot dates (Opus, Gemini) | Staged and hashed images, which `flash` checks. The `batch`, date and slot columns must all match. The vendor and bootimage dates are matched too. |
| USB watchdog detach, saved functions, MTP scoping (Sol, Gemini) | `setsid nohup` with closed streams and a verified PID. `getFunctions` is saved and restored. `gio` uses the serial URI, or `mtp-detect` confirms the serial. MediaStore scan, a gvfs settle time, the marker deleted, deadlines. |
| Final checks ran before cleanup; journal generations; a shared lock (Sol) | Cleanup comes first, then the final checks, then `eligible`. Journal entries are tagged by batch, boot and host. One lock token across the batch. |
| A low-RAM experiment could be eligible (Sol) | `experiment: true` means never eligible, and the tester enforces it. |
| Dead torch in a bright room; front and depth JPEG failures; who defines the camera set (Sol, Opus, Gemini) | `FLASH_STATE_FIRED` is required, and the torch-disabled control must FAIL. Front and depth JPEGs are INFO. The camera set comes from `batch.json`, `known-good.json` and the phone property together, and any mismatch is UNVERIFIED for Zak. |
| Bluetooth: consent can't complete unattended; `removeBond` is hidden; address read too early; no getter for the default agent (all three) | Pairing starts from the host with the session's own non-default agent. The phone confirms with shell `BLUETOOTH_PRIVILEGED`, only for forge. `removeBond` by reflection with the policy recorded in the journal. The address is read after `STATE_ON`. Only `Discoverable` is snapshotted. |
| Extra dim can pass without the transform (Sol, Opus) | SurfaceFlinger `colorTransformMatrix` must drop ≥ 30%, as seen on the phone. UNVERIFIED if it can't be read. |
| Time evidence from before the refresh; exit code always 0 (Sol, Opus, Gemini) | Stdout must be `true`, the suggestion must be newer than `E0`, and Wi-Fi must be the default network. The bad-server control runs after a sync. |
| Wrong API for `FLAG_RETRIEVE_INTERACTIVE_WINDOWS` (Sol) | Set through `getServiceInfo()` and `setServiceInfo()`. |
| Perch HOME not checked; Brave proof weak (Sol) | Pressing HOME must bring Perch to the front. Brave needs its own token, a page-script beacon and the token on screen. A User-Agent alone isn't enough. |
| `--quick` exit contract (Sol) | Exits 0 when its checks pass, and is always `eligible: false`. |
| RAM 3-boot timing and reference bootstrap; size baseline (Opus, Gemini) | Step 4 runs before `phone-setup`, with all four triggers. No reference means UNVERIFIED. The reference is seeded at calibration. Size is compared against the latest known-good. MB is defined. |
| Crashes from earlier boots since the wipe aren't seen (Opus) | `dumpsys dropbox` since the wipe, pstore at baseline, and a streamed logcat. |
| Calibration can't pass on a Trebuchet known-good (Opus) | Calibrate on the Perch candidate, then seed `known-good.json`, and the first clean batch becomes the first tag. |
| Restart counters that don't exist (Gemini) | PIDs and start times of the key processes. |
| Helper permissions (Gemini, Opus) | Normal and runtime permissions are listed separately. |
| NFC `installed-files` paths (Gemini) | The exact paths from `out-stripped`. |
| Camera exposure could hang; `MANUAL_SENSOR` fallback (Opus, Gemini) | Exposure capped at 150 ms, with an auto exposure fallback and the YUV format the camera offers. |
| Alarm dismiss needs a search mode; vibration; redacted notification text (Gemini) | `ALARM_SEARCH_MODE_LABEL`, `EXTRA_VIBRATE=false`, and `--noredact` or uiautomator. |
| Audio source and route timing (Opus, Gemini) | `UNPROCESSED`, falling back to `VOICE_RECOGNITION`. Routes are checked after the streams start. |
| Exit precedence, a canonical required-check list, one UiAutomation at a time, network list (Opus, Gemini) | All adopted |
| Brave Origin replaced (Zak) | The charter now lists Brave, Tailscale and T3 Code. All three APKs are pinned and were tested on the phone. |

## Round-3 notes to apply during implementation

From Opus round 3:

- Disable the torch for its control below the HAL, using the flash LED sysfs as root. Add a check that luma differs with and without the torch.
- Use the camera's real maximum exposure, under a deadline. The maximums are 270–487 ms. Drop the 150 ms cap.
- Never power the host Bluetooth adapter on or off.
- In token mode, child scripts skip `flock` and refuse to run unless the token matches.
- Store `ram_reference` as a running minimum. Store all three boot rows.
- Count every dropbox entry, with no wall-clock filter.
- Put the boot reason and pstore in the report.
- Accept battery status 4 when a power source is present.

From Sol and Gemini round 3, also to apply during implementation:

- Migrate the `device.csv` header to the new columns, including
  `bootimage_date_utc`.
- The watchdog's ready signal includes `boot_id`, the PID and the process
  start time.
- `--quick` keeps `phone-check`'s `--wait` behaviour and loads no fixtures.

## Round-3 findings and how revision 4 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| Calibration can't produce its first baseline (Sol) | A `--calibrate` mode that records the 3 boots, the sizes and the camera set, and is never eligible |
| `known-good.json` mixes up the latest tag with the lowest RAM median (Gemini, Sol, Opus) | Separate `latest` and `ram_reference` records. `ram_reference` is a running minimum. |
| BlueZ can't pair with a phone it hasn't discovered (Sol, Gemini) | The phone is made discoverable for 120 s with `setScanMode` under shell permission. The host runs `scan bredr` until it sees the phone, then pairs. The scan mode is restored. A high-priority pairing receiver calls `abortBroadcast`. |
| The screen-off wait expects `Asleep`, but river dozes (Gemini) | Wait for `Dozing` or `Asleep`, and `Display State=OFF` |
| Wi-Fi never loads an actual page (Sol) | A page from forge's LAN address with a fresh token, over the bound Wi-Fi network, before and after the screen cycle |
| Torch evidence could be faked; the 150 ms cap; approved camera-set changes; camera 0 being optional (Sol, Opus, Gemini) | An optical torch-off/on difference at locked exposure, with the torch control disabled below the HAL. Real maximum exposure under a deadline. Zak-approved `camera_set_change`. The torch photo runs only when camera 0 is enabled. |
| The alarm could pass on the "upcoming" notification (Opus, Sol) | Only firing evidence counts: absent at target − 10 s, present by target + 10 s. Applied after Opus's round-3 review. |
| Dates can repeat between builds; low-RAM could slip through (Sol) | `ro.river.build_id` stamped into the build, the boot partition hash read on the phone, and a `ro.config.low_ram` veto |
| Recovery handoff, MTP scope, journal invalidation (Sol) | Slot rescue when fastboot answers, otherwise halt and notify. MTP limited to the gio serial URI or a single matching device, otherwise UNVERIFIED. Journal entries dropped only after a recorded successful wipe, and recovered only once the lock is held and the phone is confirmed. |
| Health evidence from each measured boot (Sol) | `device-bench` saves health evidence for each boot before it reboots again. QA fails on any of it. Dropbox has no time filter. |
| Minors: staged `installed-files`, `getFunctions` on stderr, `time_detector` delay, a hidden constant, the CSV migration, alarm recovery, gesture and alarm controls, battery status 4 (Gemini, Opus, Sol) | All adopted |

## Round-4 notes from Opus (now in the design text above)

- **Boot hash.** Hash only the first `size(boot.img)` bytes of the partition. The partition is 33,554,432 bytes and the image is 29,327,360.
- **Wi-Fi LAN fetch.**
  - Add forge's LAN address to the helper's cleartext allowlist.
  - Use a fixed listener port.
  - forge has ufw enabled, so the port needs a rule allowing only the phone's address. Adding the rule needs sudo. It is a one-time setup step through `request_secret`.
  - List the listener in the Safety section.
- **Slot rescue.** When adb answers but boot never completes, run `adb reboot bootloader` and then rescue. If the bootloader falls back to the old slot by itself, report the batch as failed.
- **RAM reference.** While `seed: calibration` is in place, every batch measures 3 boots.
- **Camera set change.** Use `gh` to confirm that the `camera_set_change` approval was written by `zrm625`.
- **Bluetooth state.** Restore the scan mode and Bluetooth on/off to the journal snapshot.
- **Torch.** Run the torch check at its own locked exposure, separate from the liveness check's maximum exposure. If the HAL rewrites `led:flash_*`, the calibration control can use `led:switch_*` instead.
- **First-boot evidence.** The flash step saves `dmesg` and `logcat -b crash` from the first boot after the wipe, before the reboot for boot 1.

## Round-4 findings and how revision 5 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| The boot hash covered the whole partition (all three) | Hash only the image's size, and FAIL on a short read |
| One shared property can't prove both partitions, and the stamps came too late (Sol, Gemini) | `ro.river.build_id` in `/system/build.prop` and `ro.vendor.river.build_id` in `/vendor/build.prop`, each read directly. The stamping ships with the harness, before calibration. |
| Perch would become its own baseline (Sol) | The seed comes from a 3-boot `--calibrate` measurement of `river-22.2` HEAD. Perch calibration sets only the functional thresholds. |
| Slot rescue missed the case where adb answers but boot never completes (Sol, Opus, Gemini) | `adb reboot bootloader`, then rescue. The previous slot is recorded and kept across retries. A bootloader fallback is reported as a failed batch. |
| Torch evidence and the photo were separate images (Sol) | Matched torch-off and torch-on stills at locked exposure. `FLASH_STATE_FIRED` comes from the torch-on still's own result. The LED stays disabled for the whole control capture. |
| The Wi-Fi LAN fetch was blocked by the cleartext policy (Gemini, Opus) | The cleartext allowlist covers exactly `127.0.0.1` and forge's LAN address. A one-time ufw rule is set up through `request_secret`. |
| Minors: the seed trigger, the `setScanMode` signature and hidden API, variant 3 only, alarm evidence through the notification, `pm clear` in cleanup, `adb root` in `device-bench`, cleanup order, battery status 4, `camera_set_change` checks, Bluetooth snapshot restore, first-boot evidence (all three) | All written into the design text |

## Round-5 findings and how revision 6 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| A retry could overwrite the fallback slot (Sol) | The target and fallback slots, plus the fallback's boot hash, are saved before any write. Retries enforce them and never work the target out from `current-slot`. Rescue runs on a failed boot during the flash or the bench. An unstamped fallback is checked by slot and the boot hash saved before the flash. |
| The baseline couldn't be measured on the Trebuchet build (Sol) | A `--baseline` mode: identity check, wipe, 3 bench boots with the wizard untouched, and health evidence for every boot. No functional checks. Reused rows need the same evidence. |
| Retiring the calibration seed could raise the RAM reference (Sol) | Retiring the seed keeps the lower of the seed's median and the tag's median, with its provenance |
| Evidence from the first boot after the wipe wasn't gated (Sol) | It is the same evidence as for the measured boots, and gated the same way. Missing evidence is UNVERIFIED. |
| Firewall rule and port in the body, hidden API list, status line, flicker-safe exposure, a stamped fallback, returning to the known-good slot after a FAIL (Opus) | The first three are written into the text. The others are implementation notes: exposure in multiples of 10 ms, and after a FAIL the batch tools switch back to the known-good slot. |

## Round-6 findings and how revision 7 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| The slot record could be cleared before the candidate became known-good (Sol). Raised as notes by Opus and Gemini. | `known_good_slot` is kept for good. A pair is resolved only by a verified rollback to `known_good_slot` (after a FAIL, an UNVERIFIED, an experiment, calibration, baseline or an interruption) or by a durable merge and tag. Before every flash the runner checks that no pair is unresolved, that the running slot is `known_good_slot`, and that the write target isn't. If a pair is abandoned, the runner rolls back, or halts and notifies Zak. |
| A replacement boot could supply clean first-boot evidence (Sol) | `flash` records `boot_id` when it sees boot completed. The evidence must carry the same `boot_id` and flash attempt, and the same applies to each bench boot. A mismatch or missing evidence is UNVERIFIED. |
| Minors: rescue wording, clearing the seed flag, `--baseline` never eligible with the same slot rules, first-boot crash allowlist only in writing and from calibration (Opus, Gemini) | Written into the text. Any first-boot allowlist that calibration needs is written down in the harness, never applied quietly. |

## Round-7 findings and how revision 8 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| A leftover sentence still released the pair when QA finished (Sol, Gemini, Opus) | Removed. Only a verified rollback, including a completed rescue, or a durable promotion releases the pair. |
| The retry rule was ambiguous (Gemini, Sol) | One rule: a retry that writes images first completes a verified rollback, then reuses its own `target`. The "no unresolved pair" check covers other batches' pairs. |
| A crash after tagging could roll back over the new tagged build (Sol) | Recovery checks the exact batch's merge and tag state with `gh` and `git ls-remote` before deciding. Fully merged and tagged means promote; nothing merged means roll back; partly done means halt. The pair stays reserved until the decision is recorded. This applies to every unresolved pair. |
| A reset before boot completes could pass as a clean boot (Sol, Opus) | Three signals: the kernel-log marker chain through pstore, uptime against host elapsed time, and the dropbox `SYSTEM_BOOT` count since the wipe. Any sign of an extra boot is a FAIL. Unreadable pstore is UNVERIFIED. The boot reason isn't trusted, because ordinary flashes report `hardware_reset`. Calibration plants extra reboots to prove this. |
| Minors: setting `known_good_slot` the first time, rolling back onto the candidate's `/data`, a single command for `boot_id` and `boot_completed`, any unresolved pair (Opus) | All written into the text: it is set when `--baseline` passes, together with the boot hash; rollback runs `fastboot -w` and records the wipe; one shell command reads both; every unresolved pair is reconciled. |

## Round-8 findings and how revision 9 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| The first baseline couldn't meet the flash precondition (Sol, Opus) | First-run transition: the running slot becomes a provisional `known_good_slot`, checked live by its boot hash. The baseline is flashed to the other slot, and a passing baseline closes its own pair at once. Perch calibration runs only after that. |
| Rollback used `fastboot -w`, which river rejects, and rescue skipped the wipe (Sol, Gemini, Opus) | One rollback procedure for rescue, failures and detected fallbacks: `--set-active`, then `fastboot erase userdata` (must succeed and is recorded), then reboot and check the slot and build. Only then is the pair released. |
| A reset too early for ramoops or dropbox to record (Sol) | The uptime clock starts at USB disconnect. The allowance must be smaller than the shortest bootloader pass calibration measured, so every reset makes the uptime come up short. If calibration can't separate the two, that path is UNVERIFIED. New control: an extra bootloader pass with no kernel session must FAIL. |
| Minors: pstore bit flips, a failed lookup halts, checks before promotion, interrupted runs follow the recovery rule, dropbox counts settled and compared by name, wording, the boot-reason sentence (Opus) | All written into the text. Rerunning once on a spurious extra-boot FAIL belongs to `change batch`. |

## Round-9 findings and how revision 10 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| The uptime allowance formula was backwards, so every normal boot would fail (Opus, Sol) | A is at least the longest normal bootloader pass, and less than the shortest normal pass plus one more pass. If no such A exists, that path is UNVERIFIED. The clock starts at the first USB disconnect after the command. Any fastboot enumeration the harness didn't plan counts as an extra boot. |
| The bootstrap order still flashed Perch before the baseline (Sol) | New order: (1) boot-timing calibration on the running build, with no flash; (2) the baseline, whose pair is closed, or rolled back and run again if interrupted; (3) Perch calibration, only after that; (4) thresholds. |
| Time spent flashing counted against boot timing (Sol) | For the flash path, the clock starts at the disconnect after the final `fastboot reboot` |
| Minors: pairs with no PRs always roll back, the calibration flash count, starting the control at once (Opus) | Written into the text |

## Round-10 findings and how revision 11 handles them

| Finding (who raised it) | Disposition |
| --- | --- |
| The extra-pass control fell outside the uptime clock, and an unplanned-enumeration FAIL could hide it (Sol) | For the control, the clock starts at the disconnect that begins the injected pass. Its enumerations are marked planned. The uptime check itself has to reject it on each path, or that path is UNVERIFIED. |
| The first Perch batch could merge before the controls had qualified the gate (Sol) | New step 5: every control must give its expected result before any run is merge-eligible. Until then, `eligible: false` with the reason "controls not qualified". The results are tied to the harness version and the thresholds. |

Round-10 notes from Opus, to apply during implementation:

- Measure "one more bootloader pass" as reset to kernel start only, using
  the flash path's normal pass. The injected control's fastboot wait would
  make it look longer than it is.
- Record B on the baseline flash. If it is more than A, stop and
  recalibrate. Don't loop.
- Gate Perch calibration on a baseline-closed record in `slot-state.json`.
  A baseline rerun overwrites the seed.
- Store the pair kind (batch, baseline, calibration, experiment) as a field
  in `slot-state.json` and `batch.json`. Recovery must not work it out from
  branches.
- The "tag pushed, then host killed" control uses stubbed `gh` and
  `git ls-remote`, never real tags.

## Amendment A1: the adb-path uptime clock

**Superseded by [amendment
A2](#amendment-a2-drop-the-pstore-reset-anchor-and-add-an-immediate-reboot-bench-path).**
Kept as history; none of the anchor below is in the code any more.

Status: approved with conditions in council code review round 1 (Opus and
Gemini approved; Sol objected to the first wording, and the conditions below
answer that objection). It changes only the uptime signal on the `adb reboot`
path. The fastboot path, the kernel-log chain and the boot count are
unchanged.

**Why.** The first baseline (`baseline-1`, 2026-11-23) showed that river's
USB gadget drops several seconds before the kernel resets. A clock that
starts at the USB disconnect still contains a variable shutdown tail:
16.4-16.7 s for the measured "bootloader pass" during step 1, 20.6 s for the
first reboot after a wipe, and 14.8 s later on the baseline build. An injected
extra bootloader pass adds only 2.3-4.3 s, so no allowance A separates a
normal boot from one with an extra pass. Anchored at the old kernel's reset
instead, three adb reboots measured 10.003-10.012 s, and an injected pass
13.80 s.

**The rule.** On the adb path the uptime clock starts at the old kernel's
reset, mapped onto host time:

1. **One clock on both sides.** When the harness writes the leaving marker to
   `/dev/kmsg`, it brackets the write with host monotonic time and reads the
   marker's own printk timestamp back from `dmesg` straight away. That pairs
   host time with printk time. `/proc/uptime` (`CLOCK_BOOTTIME`) is not used
   for the pairing, because printk uses `local_clock`. A write that takes
   longer than 0.3 s is retried. Every attempt writes a new marker with its
   own nonce, and the write's exit status is checked (all copies chained with
   `&&`). A failed write is never paired and never becomes the leaving
   marker, and the readback looks for that attempt's nonce only, so a retry
   can't pair an older write's printk time with a newer host interval. If no
   write can be bracketed and read back, the boot has no pairing and its
   uptime signal is UNVERIFIED.
2. **Strict anchor: any ambiguity is UNVERIFIED** (code review round 3,
   replacing the "last corroborated run" heuristic). **Superseded in part
   by [amendment A1′](#amendment-a1-reconcile-the-strict-anchor-with-the-phone-data):**
   the marker is found tolerantly and proven by its stamp, the reset comes
   from the final 5 lines only, and there is no `max_stamp_gap`. At the next boot
   completion the previous kernel's log in pstore gives the reset time:
   - **One exact record.** The marker record is the last intact copy of this
     boot's exact marker: a strictly parsed stamp, the full marker straight
     after it, then the end of the line. There is no fuzzy matching. If no
     copy is intact, the result is UNVERIFIED. The marker is written four
     times, so one intact copy is usually enough.
   - **Every number from that record.** The live readback records the
     printk stamp of every copy. The record's pstore stamp must equal one of
     them to the microsecond (the console and `dmesg` print the same
     record), or the readback and the log are not the same write. The gap to
     the reset is measured from that stamp, and the tail starts at that
     record.
   - **The reset is the final line, only if the tail is clean.** Clean
     means: every line from the record to the end of the log starts with a
     stamp in the kernel's own `[%5lu.%06lu] ` layout (no leading zero, no
     damaged byte), and any further stamp on the same line (a record
     printed without a newline) parses the same way; the stamps never
     decrease; no two consecutive stamps are more than the calibrated
     `max_stamp_gap` apart; the last 5 lines span at most 1.5 s; and the
     last 3 lines span at most 50 ms. Anything else is UNVERIFIED; the
     harness never picks the most plausible stamp. Corruption becomes an
     UNVERIFIED boot and a rerun, never a different anchor.
   - **A flip inside the final window.** A flip that leaves the final stamp
     within 50 ms of the two lines before it can't be told apart, so the
     span of the last 3 lines is taken off the anchor. With one flip, the
     anchor is never later than the true reset. A unit test flips every bit
     of every byte from the marker copies to the end and checks this.
   - **Bounds.** The anchor must not precede the marker. Its host time must
     fall between the USB disconnect and the disconnect plus the largest
     shutdown tail calibration measured, allowing for the pairing's
     uncertainty. Of the possible host times, the earliest is used.
   - **Silent tail.** The kernel may log nothing between its last line and
     the real reset (a slower shutdown that goes quiet). That time can't be
     measured from the log, and it isn't bounded. It is safe for the uptime
     check: the anchor stays at the last line while the new kernel starts
     later by the quiet time, so the measured bootloader pass grows by
     exactly that amount. A quiet tail can turn a PASS into a FAIL, never a
     FAIL into a PASS (unit test `test_silent_tail_only_makes_it_stricter`).
     The residual risk is only that the recorded shutdown tail
     (disconnect to last line) understates a slow shutdown; the tail bound
     is a sanity check on the anchor, not a verdict, so this can't hide an
     extra pass.
   - **Measured cost on river.** On the three `baseline-1` adb captures
     the tails after the marker held 130-172 lines with 1, 4 and 68 lines
     whose stamp was damaged. None is clean under this rule, so expect many
     adb boots to be UNVERIFIED and rerun. Two of the three would pass if
     damaged lines before the last 5 were skipped; that relaxation is not
     part of this rule and needs the council.
3. **Only with a passing chain.** The anchor is used only when that boot's
   chain verdict is PASS, so the matching leaving marker is in that same log.
   The adb path never falls back to the disconnect clock: without an anchor,
   its uptime signal is UNVERIFIED (an unplanned fastboot enumeration is still
   a FAIL).
4. **Enumeration window.** The unplanned-enumeration window starts at the
   recorded disconnect event itself, not at an event-list index sampled after
   the wait for the disconnect returns.
5. **Fastboot path.** Unchanged: its clock starts at the disconnect after the
   final `fastboot reboot`, which is stable (10.01-10.08 s).

**Calibration.** (Amendment A1′ rule 4 replaces the bound below: the
largest of at least 3 first-after-wipe tails plus 1.0 s, and no
`max_stamp_gap`.) Step 1 (`phone-qa boot-timing`) sets A on the adb path only
from reboots that got an anchor, and records their shutdown tails
(disconnect to reset); the largest is the bound in rule 2. While it
measures, the gap between consecutive stamps is capped at 5 s; the largest
gap any anchored step-1 tail had, plus 1 s, becomes `max_stamp_gap`. Without
it the adb path has no anchor. The bound must
include the first adb reboot after a wipe, which is every batch's first bench
boot and has the longest tail. So step 1 runs straight after `flash --wipe`,
its first adb reboot is that reboot (one `SYSTEM_BOOT` in dropbox), and the
bound is set only if that reboot got an anchor. Otherwise the adb path has no
bound and its uptime signal stays UNVERIFIED. A later boot with a slower tail
is UNVERIFIED, never PASS. The proofs are as before: a normal boot must pass
and an injected extra pass must be rejected by the uptime check itself, both
on the anchored clock. `phone-qa controls --boot-timing` runs the proofs
again with the recorded A and bound, so they can be renewed under the final
harness version (README, "Order of qualification").

**Reading pstore.** The boot judge reads pstore through a binary-safe read
that carries the remote status (base64 over the shell protocol, with a size
check against `stat` and a closing count). A failed or partial read makes the
chain UNVERIFIED and the boot's combined verdict UNVERIFIED on every path.

**Cost.** The adb-path uptime signal now depends on a readable pstore log,
so it is less independent of the kernel-log chain than before. The boot count
is still independent of both.

**Phone runs still needed.** `flash --wipe` with this code (so `wipes.jsonl`
records the wipe's first boot), then step 1 straight away: the first adb
reboot after the wipe must get an anchor, later adb boots, and both injected
controls on each path. These runs also show on the phone that the checked
kmsg write, the base64 pstore read (its size must match `stat`), the
microsecond live/pstore marker match and the clean-tail rule work as
designed, and how often a tail is clean (each step-1 record keeps its
`anchor_note`). If too few tails are clean for step 1 to set a bound, the
strict rule needs the council before anything is relaxed. Then the order of
qualification in the README.

## Code review round 2: changes to the amendment and the design

Council code review round 2 (Opus and Gemini approved, Sol objected) asked
for the A1 changes above (earliest corroborated stamp, the pstore marker
check, one verified write per attempt, the gap measured in pstore, the
first-after-wipe tail) and these:

- **A7, camera-set changes are manual.** A batch whose `batch.json` has
  `camera_set_change` is never eligible: "camera set change: Zak merges by
  hand". Removing a camera is removing a must-work item, which needs Zak.
  `hardware.cameras` still verifies the approval and the new set, and the
  result stays in the report as evidence.
- **C1, promotion bound to the tested commit set,** and `merge_started` for
  recovery (see "Recovery checks GitHub first").
- **Controls credited only for their own fault.** The extra-reboot controls
  need the injected boot's own signal record (row key and `boot_id`) to be
  rejected; a missed injection never matches; `bt-rfkill` needs a valid
  powered-adapter report replaced by the stub; the torch control is held at
  `max_brightness` 0; every mode except `control` fails on a control
  override.
- **A8 gestures** need `isConnected=true` and `mBound=true` for Perch;
  `mIsEnabled` alone doesn't count. **A9 alarm** samples carry dumpsys's own
  status; an unreadable sample is no evidence; absence needs a successful
  sample finished by target − 10 s.


## Code review round 3: changes to the amendment and the design

Council code review round 3 (Opus and Gemini approved, Sol objected) asked
for the strict A1 anchor above (rule 2: one exact marker record, a clean
tail, otherwise UNVERIFIED; the silent tail) and these:

- **Promotion proof** (see "Recovery checks GitHub first"): the expected PR
  set is frozen at `merge_started`, each PR must be merged into `river-22.2`
  of its own zrm625 repo, the tagged tree must be the tested tree, and JSON
  writes fsync the directory after the rename.
- **Controls.** The torch control qualifies only when a valid capture
  finished and the torch assertion itself (`FLASH_STATE_FIRED` or the luma
  rise) rejected it. `bt-rfkill` honours the exit status of
  `bluetoothctl show`; a failed show is UNVERIFIED and nothing is stubbed.
  The extra-reboot controls need the injection's `boot_id` to be present
  and equal to the injected row's. An injected reboot that is never seen to
  drop off USB is recorded as missed, so its control is unqualified.
- **Enumerating the diagnostics.** The binary read's remote script checks
  that the directory exists, is readable and lists before it expands the
  glob. A missing or unreadable directory fails, and so does a glob that
  matches nothing (marked as "no match"; only `pstore/*` takes that as an
  empty pstore, because the directory was proven readable first). The glob
  is expanded once, and a listed file that vanishes before it is read fails
  the read; it is never skipped.
- **A8 gestures, exact package.** The overview proxy's recents component
  and quickstep intent are parsed, and the package must be exactly
  `app.zak.perch`; `app.zak.perch.fake` is rejected.
- **A9 alarm, firing time.** The firing time lies after the last absent
  sample started and no later than the end of the first firing sample.
  With the notification's recorded post time (`postTime`, or
  `mCreationTimeMs`, which the system sets from it; never the app-chosen
  `when`), that time must fall in target ± 10 s and inside its own
  observation bounds. Without one, PASS needs the whole interval inside
  target ± 10 s; an interval that spans either boundary is UNVERIFIED.
  Absent at −15 s and then a firing poll from −11 s to −9 s is not a PASS.
- **Journal recovery.** The helper is uninstalled through the same checked
  path as other packages: a failed package listing is not proof it is gone.
  Leftover alarms are looked for in the past-due non-wakeup list too, not
  only the pending alarms. Setting restores verify with a checked read.
- **Calibration.** "The first adb reboot after a wipe" comes from the flash
  record: `flash` (dev flash, batch flash and rollback) writes the boot_id
  of the first boot after each wipe to `wipes.jsonl`, and step 1's first adb
  reboot counts only if it rebooted from that boot and dropbox holds exactly
  one `SYSTEM_BOOT` (the count is read only once it has settled at 1 or
  more). `qualified()` needs all three proofs per path, the unmarked
  extra-pass rejection included. The README's order of qualification spells
  out the final step: resolve the previous pair, flash with a wipe, bench,
  set up the phone, then the five clean calibrations.
- **Smaller items.** Any non-null `camera_set_change` makes a batch
  ineligible, and a malformed one (not `{"url": str, "set": [distinct camera
  IDs]}`) is reported as such and makes `hardware.cameras` UNVERIFIED.
  `scripts/build` counts untracked files as dirty even when git is set to
  hide them (`--untracked-files=normal`). Every pass decided from the top
  activity (Tailscale, T3 Code, Brave, Perch, DeskClock, the helper) needs
  the **resumed** activity of the **exact** package; a prefix match or an
  activity on top that isn't resumed never counts.

## Amendment A1′: reconcile the strict anchor with the phone data

**Superseded by [amendment
A2](#amendment-a2-drop-the-pstore-reset-anchor-and-add-an-immediate-reboot-bench-path),**
except rules 1-2 (find the marker tolerantly, prove it by its stamp), which
A2 keeps for the chain.

Status: specified 2026-11-23 from the on-phone validation run. It replaces
rule 2 of amendment A1 (the code review round 3 wording) wherever the two
conflict, and the calibration of the shutdown-tail bound. Rule 1 (the
pairing), rule 3 (only with a passing chain), rule 4 and rule 5 stand.

**Data from the phone.**
- The ramoops log has no ECC, so the marker copies arrive damaged. On one
  real dump, the 4 copies had 2, 1, 2 and 4 wrong bytes.
- Under the round-3 rule (exact copies only, and a fully clean tail from the
  marker to the end of the log) essentially no adb reboot gets an anchor. In
  three `baseline-1` captures there were 1, 4 and 68 damaged lines after the
  marker.
- Under commit `1faf721` (copies within 3 bit flips, the stamp read next to
  the marker, a 50 ms live/pstore check) about 18 of 20 adb reboots got an
  anchor. Both failures fell back to UNVERIFIED.
- The round-2 tail bound came from one first-boot-after-wipe sample with no
  margin. Measured first-after-wipe tails: Perch 8.52 s and 8.04 s; stock
  build 9.08 s, 9.17 s and 9.02 s.

**The rule.**
1. **Find the marker tolerantly.** A copy matches if it is within 3 bit
   flips of this attempt's full marker line (`bootsig.marker_offsets`).
2. **Prove the record by its stamp, not its text.** The strictly parsed
   printk stamp straight before a matched copy (`bootsig.stamp_before`; an
   earlier stamp on a merged line never counts) must equal, to the
   microsecond, one of the stamps read back live for this attempt's write.
   A copy from another write, a nonce off by a character or a damaged stamp
   can't do that, so it is never used. If no copy passes, the result is
   UNVERIFIED. The marker's pstore stamp, its live pairing and its place in
   the log all come from the last proven copy (`bootsig.proven_marker`).
   The chain verdict uses the same proof: PASS needs an exact copy or a
   proven one, with no new kernel session after it.
3. **The reset comes only from the final lines** (`bootsig.final_lines`).
   Take the last 5 strictly parsed stamps after the proven copy, skipping
   lines that don't parse. They must not decrease, the 5 must span at most
   1.5 s and the last 3 at most 50 ms. The reset is the final stamp minus
   the span of the last 3. Damaged lines before the final 5 are ignored,
   because they can't move the estimate. The final stamps must not be
   earlier than the marker's stamp. Anything else is UNVERIFIED. There is
   no consecutive-gap rule, so `max_stamp_gap` is gone.
4. **The shutdown-tail bound needs at least 3 samples and a margin.**
   `max_shutdown_tail` is the largest tail from at least 3 first adb
   reboots after a wipe, plus 1.0 s. Each wipe gives at most one sample:
   step 1's first adb reboot (`calibrate.first_after_wipe`), or the first
   bench boot of a baseline or batch flash, proven the same way from the
   flash record and dropbox. The tail is taken from an anchor judged with
   no bound, so an out-of-bound boot still adds its sample. Samples live in
   `$BENCH_HOME/qa/shutdown-tails.jsonl` and accumulate across runs. Step 1
   and `phone-qa controls --boot-timing` set the bound from them. With
   fewer than 3 samples the adb path is UNVERIFIED. The bound only rejects
   anchors that arrive too late. Anchors now come only from proven records
   and the final lines, so a margin can't hide an extra boot; the uptime
   check against A still decides.
5. **The silent tail.** Rule 2's argument in A1 still applies: a silent
   period after the last line can only make the measured pass longer.

**Tests.** Every round-3 counterexample still gives UNVERIFIED or FAIL,
never PASS: `104.250000` → `104.050000` (both final-line sets fail rule 3),
a nonce one character off (its stamps belong to another write), copy mixing
(the stamp and the text of different copies never combine; every value
comes from one copy), and every single bit flip from the marker to the end
(the anchor is never later, and an extra-pass boot never passes). New: a
real-style dump with all 4 copies damaged and one intact live stamp is
anchored; a stamp 1 µs off every live stamp is rejected; damaged lines
before the final 5 are ignored; a flip that pushes one of the final 3 lines
more than 50 ms later is UNVERIFIED; fewer than 3 tail samples is
UNVERIFIED.

**Diagnosis.** Step 1 saves the raw pstore of every adb reboot that got no
anchor in `$BENCH_HOME/qa/pstore-noanchor/`, with the pairing and the reason.

**Phone runs still needed.** `flash --rollback` and `phone-qa boot-timing`
straight after, repeated until there are 3 tail samples; then the anchor
rate on the adb path and both proofs per path. If the anchor rate is below
about 90%, the rule is not loosened; the failing pstore dumps go to the
council.

## Amendment A2: drop the pstore reset anchor and add an immediate-reboot bench path

Status: specified 2026-11-23 from the on-phone data below. It replaces
amendments A1 and A1′. A1′ rules 1-2 stay, for the chain only.

**Data.**
- **A1′ failed on the phone.** After wipe #15 only 1 of 5 adb reboots got an
  anchor. The lines after the marker had a damaged stamp 41-60% of the time:
  rules 1-2 worked, rule 3 (the final lines) did not. Anchoring the `adb
  reboot` path on pstore can't work on river. The dumps are in
  `$BENCH_HOME/qa/pstore-noanchor/`.
- **The immediate kernel reboot is as stable as fastboot.** `sync; echo b >
  /proc/sysrq-trigger` as root (it works with `kernel.sysrq=0`): command to
  USB gone 1.17-1.28 s (three trials); USB gone to kernel start 9.89-9.93 s
  (the four step-1 normal reboots of 2026-11-23: 9.888, 9.909, 9.909 and
  9.931 s). The fastboot path's normal pass is 10.05-10.09 s, and an
  injected extra pass adds about 4.4 s. USB drops at the reset itself, so
  there is no variable shutdown tail. (This text first gave 10.06-10.08 s
  for the `sysrq` pass, copied from the fastboot path; corrected in R4-7.)

**The rule.**
1. **Bench boot 1 stays a normal `adb reboot`.** The charter's boot metric
   (`host_boot_seconds`, ≤ 55 s) is defined exactly as before, and the boot
   also exercises a clean Android shutdown. Its reset detection is the
   kernel-log chain plus the dropbox boot count. Uptime is recorded as INFO
   only, with no anchor and no gate. An unplanned fastboot enumeration is
   still a FAIL.
2. **The chain** (`bootsig.chain_verdict`; [A2.1](#amendment-a21-the-nonce-proves-the-chain-not-the-stamp) replaces this
   rule: the stamp proof is gone). PASS needs a copy of this
   attempt's marker proven by its stamp (A1′ rules 1-2: a copy within 3 bit
   flips whose strictly parsed stamp equals a live stamp of this attempt's
   write, to the microsecond), with no new kernel session after it. An extra
   kernel that ran after ramoops init replaces the pstore log, and its log
   can't contain this attempt's marker, so a readable log with no copy of
   the marker is a FAIL. A missing, empty or unreadable log is UNVERIFIED.
   So are copies whose stamps are all damaged, and a marker whose stamps
   were not read back: the log is the marker kernel's, but nothing proves
   it. An exact copy without a proven stamp is not enough for PASS.
3. **Every other bench boot uses the immediate reboot** (`sysrq`): `sync`,
   then `echo b > /proc/sysrq-trigger`, as root, after the leaving marker
   (`Device.reboot_sysrq`; a raw sysrq write through `Device.adb` is refused
   like a raw reboot). **Every batch performs at least one**, even when the
   charter needs only 1 bench boot: the first `device-bench --batch` run of a
   flash attempt measures at least 2 boots, and `health.boot_signals` is
   UNVERIFIED without a `sysrq` bench row, or if bench boot 1 is not `adb`.
   That boot's RAM row counts toward the RAM verdict only when the
   charter's three-boot rule applies (R4-4); `device.csv` records each
   row's `reboot_path`. Its gate is the uptime check from the USB
   disconnect with the `sysrq` path's own A, plus the chain and the boot
   count.
4. **The first boot after a flash stays on the fastboot path**, with the
   disconnect clock and its uptime gate unchanged.
5. **Step 1** measures and qualifies the `fastboot` and `sysrq` paths. On
   each: a normal boot passes; an injected extra bootloader pass is rejected
   by the uptime check itself; an unmarked extra enumeration is rejected by
   the enumeration rule. `sysrq-b` can't ask for the bootloader, so the
   `sysrq` path's injected pass uses Motorola's `fastboot oem fb_mode_set`:
   set it in fastboot, boot Android with `fastboot continue` (no reset),
   write the marker, `sysrq` reboot (the clock starts at this disconnect),
   the bootloader stops in fastboot (the injected pass), `fastboot oem
   fb_mode_clear`, `fastboot reboot`. If anything fails in between, the
   harness clears fb_mode before it gives up. The `adb` path has no uptime
   proofs.
6. **Controls.** `extra-reboot-bench` injects on the first `sysrq` bench
   boot, and qualifies only if that boot's uptime gate is FAIL. The new
   step-1 control `boot-timing-adb-extra-reboot` does an `adb reboot` like
   bench boot 1 and, as soon as adb answers and before boot completes (far
   past ramoops init, about 1 s into the kernel), reboots again with no
   marker. It qualifies only if the fault went in and the chain itself is
   FAIL. The step-1 controls are now `boot-timing-{fastboot,sysrq}-extra-pass`,
   their `-unmarked` twins and `boot-timing-adb-extra-reboot`.
7. **Deleted:** the corroborated stamps, the clean-tail rule and the
   final-lines reset estimate, the host-time pairing of the marker write,
   `max_shutdown_tail` and `shutdown-tails.jsonl`, the first-after-wipe tail
   samples (and the first-boot record in `wipes.jsonl` that only they used),
   `clock: reset-anchor`, and `pstore-noanchor/`. Step 1 now keeps the raw
   pstore of every reboot whose chain did not pass in
   `$BENCH_HOME/qa/pstore-nopass/` (A2.1 renamed it from `pstore-unproven/`).

**Residual risk.** A reset during bench boot 1 that happens before the
interrupted kernel initialises ramoops (its first ~1 s) leaves the marker
kernel's log in place, so the chain passes and that boot doesn't detect it.
Only a kernel or bootloader change can cause such a reset, and in the same
batch the fastboot first boot and the `sysrq` bench boot(s) are still
uptime-gated, so the same change is very likely to show up there. On the
gated paths, a reset inside the bootloader in its first ~2 s repeats less
than the margin between A and the normal pass (about 2.2 s on river), so the
uptime gate can't see it, and the chain can't either (no kernel ran). That is
outside the threat model: the gates are there to catch an extra kernel boot
or a whole extra bootloader pass (R4-7). A second
residual: if every marker copy's stamp is damaged, the chain is UNVERIFIED
and the boot is rerun; on the A1′ step-1 data, 2 of 13 boots had exact
copies but no proven one.

**Tests.** The chain: a proven copy passes; an exact copy without a live
match, a damaged stamp, a stamp 1 µs off, a nonce one character off and copy
mixing are UNVERIFIED; another kernel's log and a new session after the
marker are FAIL; no single bit flip turns a good log into a FAIL. The judge:
the gated paths use the disconnect clock, adb uptime is INFO (even with an
old adb A on disk), an adb extra kernel fails through the chain. The `sysrq`
reboot: guarded and root-only, a raw sysrq write is refused, the injected
pass's call order and clock, and fb_mode is cleared after a failure. The
bench plan, the sysrq-row and bench-boot-1 requirements, and the uptime-gate
requirement of `extra-reboot-bench`.

**Phone runs.** `phone-qa boot-timing` (after `flash --rollback` if a wipe is
needed); A for each path, the normal passes, the injected increments and the
controls; several `sysrq` reboots, whose uptime check must PASS every time;
and the adb chain control once.

## Amendment A2.1: the nonce proves the chain, not the stamp

Status: specified 2026-11-23. It replaces A2 rule 2 and the stamp proof (A1′
rules 1-2) that A2 kept for the chain. Everything else in A2 stands.

**Why.** A1′ proved a marker copy by its printk stamp because the pstore
reset anchor needed that copy's time. A2 deleted the anchor, so the stamp
proof has no job left. It still cost reruns: ramoops has no ECC, and on the
phone about a third of normal boots had every copy's stamp damaged, which
made the chain UNVERIFIED. The chain only has to show that the previous
kernel's log belongs to the kernel that wrote this attempt's marker. A
random nonce does that by itself.

**The rule.**
1. **The nonce is 64 bits.** `make_marker` writes
   `RQM:<16 hex>:<8 hex boot_id>` (29 characters), with
   `secrets.token_hex(8)`. Every attempt gets a new nonce, and a failed write
   is retried with a new one and never becomes the leaving marker.
2. **The chain** (`bootsig.chain_verdict(log, marker)`):
   - **PASS:** at least one copy of this attempt's marker within 3 wrong
     characters (`MAX_MARKER_FLIPS`), and no new kernel session (a `Linux
     version` banner) after the first copy.
   - **FAIL:** a readable log with no copy at all (another kernel ran after
     ramoops init and replaced the log), or a new session after the marker.
   - **UNVERIFIED:** no marker was written, or pstore is missing, empty or
     unreadable.
   - Stamps play no part, and nothing is read back from live `dmesg`.
3. **The chain record in step 1 stays.** A path "survives" when every normal
   calibration boot on it got a chain PASS. With the stamp proof gone, a
   path fails that only if pstore does not reach the next boot with the
   marker on it, which is what the round-11 design meant by it. On such a
   path a chain UNVERIFIED still does not count, and a chain FAIL still does.
   (Superseded by round 5, R5-12: the chain counts on every path.)
4. **Deleted:** `proven_marker`, `stamp_before`, `_same_us`,
   `STRICT_STAMP_RE`, `_strict_at`, `TS_RE`, `marker_printk_stamps`, the live
   readback in `reboots.leave_marker`, `BootRecord.marker_stamps`,
   `marker_proven` and the `proven` list in `boot-timing.json`. Step 1 keeps
   the raw pstore of every reboot whose chain did not pass in
   `$BENCH_HOME/qa/pstore-nopass/` (was `pstore-unproven/`).

**The bound.** A copy of a different write (a retry, an earlier attempt or
an earlier boot) shares at most the `RQM:` prefix and the boot ID with this
attempt's marker. Its nonce is independent and uniform, so it matches within
3 flips only if at least 13 of its 16 hex characters happen to equal this
nonce's. The chance is
sum over k = 13..16 of C(16, k) · (1/16)^k · (15/16)^(16−k) ≈
C(16, 3) · 15³ / 16¹⁶ ≈ 1.0 × 10⁻¹³ per pair of writes (C(16, 13) · 16⁻¹³
≈ 1.2 × 10⁻¹³ as a simple upper bound). Over any realistic number of boots
this is negligible. The 32-bit nonce of A2 gave about 4.5 × 10⁻⁵ per pair
(at least 5 of 8 equal), which was only acceptable because the stamp proof
backed it.

**The scan.** `marker_offsets` anchors on 6-character slices taken every 3
characters. A character sits in at most 2 slices, so 3 flips break at most
6. The 29-character marker has 8 slices, so a copy within 3 flips always
keeps one intact and is always found. With A2's 21-character marker there
were 6 slices, and 27 of the 1,330 three-flip patterns broke every slice
(for example flips at 4, 10 and 16), so such a copy was missed.

**Residual risk.** Unchanged from A2: a reset during bench boot 1 before the
interrupted kernel initialises ramoops leaves the marker kernel's log in
place, and that boot does not detect it. A2's second residual (all stamps
damaged, so UNVERIFIED) is gone. A log whose every copy has more than 3
wrong characters is a FAIL, as under A2; on the A1′ and A2 data no boot had
that.

**Tests.** The chain: one copy and four copies pass; damaged stamps pass;
copies with 1-3 flips pass and 4 flips is no copy (FAIL); every 3-flip
pattern over all 29 positions is found; a real-style dump with damaged
stamps and 2, 1, 2 and 4 flips passes; another kernel's log, a new session
after the marker and a new session between copies are FAIL; no marker and a
missing or empty log are UNVERIFIED; a marker from an independently
generated nonce (a different write) does not match while a copy of this
attempt's marker with 3 flipped nonce characters does; 10,000 random pairs of
different nonces never match each other; no single bit flip turns a good
log into anything but PASS. `leave_marker`: a fresh nonce per attempt, a
failed write retried with a new nonce, and no live readback.

**Phone runs.** `phone-qa boot-timing`, no wipe needed; count the chain
verdicts per path (PASS on normal boots should be about 100%), and check that
the injected controls still FAIL for the right reason.

## Code review round 4: changes to A2 and A2.1

Status: specified 2026-11-23 from the round-4 code review of `f0ebde5`.
Everything in A2 and A2.1 stands except as changed here.

**R4-1. The uptime clock is bracketed at both ends.**
- The USB watcher records, for each disconnect, `last_present` (the start of
  the last poll that still saw the phone) and `first_gone` (the end of the
  first poll that did not). The disconnect happened somewhere between them.
- The clock starts at `last_present`, the earliest possible disconnect, and
  stops at the end of the status read that saw `sys.boot_completed=1`. Every
  timing uncertainty therefore makes `elapsed` longer, which leans toward
  FAIL.
- If `first_gone − last_present` is more than 0.5 s
  (`bootsig.DISCONNECT_BOUND_S`), the uptime gate is UNVERIFIED. Before this
  change a busy host that saw the phone gone 2.3 s late turned a 14.23 s
  extra pass into 11.93 s, under A, and passed it.
- `boot_id`, `sys.boot_completed` and `/proc/uptime` come from one shell
  command, and the host clock is read just before and just after it. If that
  read took more than 1 s (`bootsig.STATUS_READ_BOUND_S`), the harness reads
  again, up to 3 times, and uses the first fast read of the same boot (a
  later read only makes `elapsed` longer). If none is fast, the uptime gate
  is UNVERIFIED.
- `BootRecord` and `boot-timing.json` carry `disconnect_gap` and
  `status_read_s`. On the adb path both are recorded but, like the uptime,
  are INFO only.

**R4-2. Calibration uses only clean samples.** `calibrate.compute_paths`
leaves out every normal reboot that shows a sign of an extra boot or a loose
timing bracket (`calibrate.sample_problems`): an unplanned fastboot
enumeration, a chain that isn't PASS, a boot count that isn't PASS, a
disconnect gap above 0.5 s, or a status read above 1 s. The excluded samples
and the reasons are listed in `boot-timing.json` (`paths.<path>.excluded`).
A path needs at least 3 clean normal reboots (`MIN_CLEAN_NORMALS`), and a
clean injected-pass measurement, or it gets no A and is not qualified.
Before this change, normal passes {10.00, 10.00, 14.20} with the 14.20 s
sample a hidden extra pass gave A = 14.80, so a real 14.2 s extra pass
passed. Normal reboots reused with `--reuse-normals` from before this change
have no bracket and are all excluded.

**R4-3. fb_mode is a durable obligation.** A2 rule 5 cleared fb_mode in a
`finally` block, which does nothing if the host dies. Now (`fbmode.py`):
- `fbmode.arm` writes a host entry of kind `fb_mode` to the state journal,
  fsynced, **before** it sends `fastboot oem fb_mode_set`.
  `Device.fb_mode_set` has no other caller. `fbmode.disarm` sends
  `fb_mode_clear` and only then resolves the entry.
- `fbmode.recover` runs under the device lock before the adb guard. With an
  obligation pending it takes the phone to fastboot if needed (`adb reboot
  bootloader`, guarded), clears fb_mode, resolves the entry and boots
  Android. With nothing pending but the phone in fastboot, it clears fb_mode
  and boots Android only when the caller asks. If it can't finish, the
  entry stays pending.
- Callers: `phone-qa boot-timing` and `phone-qa controls --boot-timing`
  (always boot, so calibration resumes from a phone sitting in fastboot);
  every device action of `flash` (clear; boot only if an obligation was
  pending); the full `phone-qa` preflight and `--quick` (only with an
  obligation pending; a full run then stops with `preflight.journal` FAIL,
  since the phone rebooted outside the batch plan); `device-bench` (only
  with an obligation pending; with `--batch` it then stops, for the same
  reason); journal recovery (a handler for the `fb_mode` kind); and the new
  `phone-qa recover`.
- `flash`, rescue and rollback also send `fastboot oem fb_mode_clear` any
  time they are in fastboot (`flashing.to_fastboot`, `write_images`). It is
  harmless when nothing is set. (Changed in round 5, R5-1: slot recovery
  never runs `fbmode.recover`; it clears in fastboot, sets the fallback
  active, then reboots, and `to_fastboot` no longer clears.)
- Unit tests now run with `$BENCH_HOME` pointed at a throwaway directory
  (`qa/tests/_path.py`), so no test can write the real journal.

**R4-4. The RAM schedule follows the charter.** The charter measures RAM
after one boot, and after three when a trigger fires (`regression.
needs_three_boots`: no reference yet, the reference is still the seed, boot
1 more than 10 MB over the reference, boot 1 at or under 1,050 MB, or
`/system` or `/vendor` grew). A2 made every batch take a `sysrq` bench boot
for its boot evidence, so every batch now has at least two RAM rows.
- The mandatory `sysrq` boot and its boot evidence stay.
- `regression.evaluate` uses the 3-boot median only when
  `needs_three_boots` is true. Otherwise the RAM verdict is bench boot 1
  (the `adb` reboot) alone, whatever other rows exist.
  `health.regression` records the rows it used (`ram_rows`).
- Once three boots are required, the median is over bench boots 1-3: boot 1
  is an `adb` reboot and boots 2-3 are `sysrq` reboots, so the median mixes
  the two paths. Both end in the same Android boot and the RAM reading is
  taken after the same settle time, so no difference is expected. There is
  no paired adb/sysrq RAM data yet (see "Phone notes" below).

**R4-5. The torch control needs the torch's own rejection.** The helper
reports `flash_states` as `String.valueOf` of each frame's `FLASH_STATE`,
so a torch frame whose capture result never arrived comes back as
`2,null`. That used to count as "not FIRED" and qualify `torch-disabled`.
Now `features.torch_capture` takes the torch-on flash state only if it is a
real `FLASH_STATE` value (0-4), and `features.torch_rejections` counts only
a usable torch-on flash state that is not FIRED, or a valid luma pair that
fails the delta check. A missing capture result or missing metadata never
qualifies the control. For the normal check a missing flash state is an
incomplete capture (FAIL), as before.

**R4-6. Only a posted notification is alarm evidence.** `dumpsys
notification` lists posted notifications under `  Notification List:` and,
further down, records still waiting under `  Enqueued Notification List:`
(`NotificationManagerService.dumpNotificationRecords` and `dumpImpl`). An
enqueued record was never posted. `features.alarm_firing_event` now looks
only at the posted section (`features.posted_records`: from its header to
the next section, `mMaxPackageEnqueueRate=`, `hideSilentStatusBar=`,
`mArchive=` or the enqueued list). A DeskClock firing record that is only
enqueued never counts.

**R4-7. Smaller changes.**
- **The banner within 3 flips (Opus 2; 2 flips at a line start since R5-10).** The new-session check now matches
  `Linux version ` within 3 wrong characters (`bootsig.banner_offsets`,
  through `bootsig.fuzzy_offsets`, which splits the pattern into 4 disjoint
  pieces so every copy within 3 flips keeps one intact). On river every
  saved pstore log since baseline-1 starts with the banner damaged as
  `Linu\xf8 ver\xf3ion` (2 flips), so the exact match never saw a banner at
  all. No other match within 3 flips appears in the 21 saved logs.
- **What the chain relies on.** The chain's FAIL for an extra kernel rests
  on `persistent_ram_zap` at ramoops init (`ramoops_init_prz`,
  `kernel/motorola/sdm632/fs/pstore/ram.c:495`): each kernel saves the
  previous log as its pstore file, then zaps the live buffer and logs its own
  boot into it. A kernel that ran past ramoops init therefore replaces the
  marker kernel's log. A batch that touches the kernel must keep that
  behaviour, or the chain proves nothing.
- **Kernel batches get 2 `sysrq` bench boots (Opus 3).** A batch that
  changes the kernel or the boot image needs at least 2 `sysrq` bench boots
  after bench boot 1 (`stage.sysrq_boots_required`): `device-bench --batch`
  plans them, and `health.boot_signals` is UNVERIFIED with fewer. A batch
  counts as a kernel batch when, compared with `known-good.json`'s latest
  build, the `kernel/motorola/sdm632` commit, the `dtbo.img` hash or the boot
  image's command line differs, or when any of these can't be compared
  (round 5, R5-6, adds the board configs, toolchain pins, header version
  and IKCONFIG). The
  boot image's own hash can't be the test: it changes on every build (the
  ramdisk carries the build date and the kernel image its build timestamp).
  baseline-1 and baseline-2 have the same kernel commit but different boot,
  kernel and ramdisk hashes.
- **Wording (Opus 5).** A2's data now gives the measured `sysrq` pass,
  9.89-9.93 s, not the fastboot path's figure; A2 rule 7 names
  `pstore-nopass/`; and A2's residual risk states that a bootloader reset in
  the first ~2 s falls under the margin and is outside the threat model.
- **One enumeration for a read (Sol).** `Device.read_files` lists the
  directory once with `ls` and reads exactly the listed names that match the
  pattern (a shell `case`, globbing off). A listed file that is gone when it
  is read fails the whole read.
- **Checked boot counts (Sol).** `reboots.read_boot_count` and
  `calibrate.boot_count_now` use the checked `dumpsys dropbox` read. A failed
  read never counts as zero entries.
- **The sysrq guard (Opus 6).** `Device.adb` refuses an argument that names
  `/proc/sysrq-trigger`, which only takes writes, and allows other uses of
  "sysrq", such as reading `/proc/sys/kernel/sysrq`. (Round 5, R5-9: only
  writes to it are refused.)
- **`allowance_for` (Gemini 1)** needs all three proofs, including
  `proof_unmarked_rejected`.
- **No `console-ramoops*` (Gemini 2).** On a gated path whose chain step 1
  showed does not survive, a pstore with no `console-ramoops*` file is not a
  read error (the chain is UNVERIFIED and doesn't count there). Everywhere
  else it still is. (Removed in round 5, R5-12: missing is a read error on
  every path.)
- **`leave_marker` (Gemini 3)** reads `boot_id` with a checked read and
  writes no marker if the read failed or came back empty.
- **`chain_survives` (Gemini 4)** answers only for the gated paths. On the
  adb path the chain always counts.
- **`recovery.tree_of` (Gemini 5)** accepts only a 40-hex tree hash.
- **`restore._bt_discoverable` (Gemini 6)** checks the exit status of
  `bluetoothctl show` and matches the whole `Discoverable:` line.
- **Log format (Gemini 7).** The app-launch PASS summary prints the top
  activity with `helper.fmt_top`.

**Phone notes (2026-11-23, after R4, build 22-20261123-UNOFFICIAL-river, no
wipe).**
- `phone-qa boot-timing` qualified every path. Across its 17 reboots the
  disconnect gap (`first_gone − last_present`) was 0.110-0.158 s (mean
  0.128 s), and the status read took 0.069-0.184 s, well inside the 0.5 s
  and 1 s bounds. No sample was excluded; all 8 normal boot counts and all
  chains passed.
- With the bracketed clock the passes read about 0.15-0.2 s longer than
  before (the clock starts at the last-present poll and stops at the end of
  the read): fastboot 10.21-10.29 s, `sysrq` 10.08-10.12 s. The injected
  increments are unchanged (4.43 s and 4.35 s). A is 12.46 s (fastboot) and
  12.27 s (`sysrq`).
- fb_mode recovery: armed as the control does, then the process was killed
  with SIGKILL. The journal kept the obligation; a reboot stopped in
  fastboot (fb_mode really set); `phone-qa recover` cleared it, resolved the
  entry and booted Android in 41 s; the next reboot went straight to
  Android.
- adb vs `sysrq` RAM, once, alternating, 120 s settle with the screen off,
  on a set-up phone (not freshly wiped): adb 1,257.5 and 1,301.0 MB, `sysrq`
  1,287.5 and 1,256.8 MB used. The spread within one path (about 43 MB) is
  larger than any difference between the paths, so no path effect shows.
  The same spread is larger than the charter's 5 and 10 MB margins on a
  used phone; RAM rows for verdicts come from freshly wiped phones.

## Code review round 5: changes to R4

Status: specified 2026-11-23 from the round-5 code review of `ee53080`.
Everything in round 4 stands except as changed here. Where this section and
an earlier one disagree, this one wins.

**R5-1. Slot recovery clears fb_mode on the way to the fallback.** Round 4
ran the generic `fbmode.recover` as a preflight of every `flash` mode. With
an obligation pending and the failed candidate active in fastboot, it
cleared fb_mode, resolved the entry and booted the candidate; a candidate
that never booted timed out and `flash` exited before rollback. Now:
- `flash --rollback`, `flash --recover` and rescue never call
  `fbmode.recover`. They get to fastboot (`flashing.to_fastboot`, which
  never clears or boots anything), clear fb_mode
  (`fbmode.clear_in_fastboot`), set the fallback active, erase userdata (the
  rollback procedure), reboot, verify slot and build, and only then resolve
  the entry (`fbmode.settle`). The active slot is never booted first.
- A host that dies between the clear and the reboot leaves the entry
  pending; the next `flash --rollback` or `flash --recover` finds the phone
  in fastboot and goes on from there.
- `fbmode.recover`, for callers that want Android (calibration, `phone-qa`,
  `device-bench`, journal recovery, `phone-qa recover`), resolves the entry
  only after the boot completed, so it too resumes after a kill between the
  clear and the boot. With `boot=False` it never boots, even after taking
  the phone out of Android, and resolves right after the clear.
- A failed `fb_mode_clear` with nothing pending is logged and the rescue or
  rollback goes on. With an obligation pending, the fallback is still set
  active, then the run halts (exit 3) and the entry stays.
- `flash --recover` with no pair and an obligation pending (or the phone
  found in fastboot): if `known_good_slot` is the active slot, clear, set it
  active, reboot, verify slot and build, resolve, and record
  `fb-mode-recovered` in `slot-state.json`. If another slot is active (a
  dev flash), clear, resolve, leave the phone in fastboot and exit 1 with
  the two ways on (`flash --rollback`, which wipes, or `fastboot reboot`).
  It never switches slots without the rollback's wipe. Before the first
  baseline (no `slot-state.json`) it falls back to `fbmode.recover`.
- `flash --recover` that decides promote boots the candidate with
  `fbmode.recover` first (the candidate is the slot to keep). One that
  decides halt leaves fb_mode alone.
- Dev flashes (`flash <env|dir>`, with or without `--no-reboot`) recover
  with `boot=False` (R5-16): they only need fastboot. Batch and dev flashes
  refuse an unresolved pair before they touch the phone. `--batch` and
  `--promote` still recover with a boot, since they need Android.
- The `sysrq` extra-pass control clears fb_mode in fastboot and resolves
  the entry only once Android booted.
- `fbmode.clear_in_fastboot` is used by rollback, rescue, `write_images`
  and the control (R5-18).

**R5-2. The boot time is the first completion seen.** `completed_status`
returns the first status read that saw boot completed and, separately, the
read whose uptime is used. `host_boot_seconds` (the charter's 55 s) comes
from the first; a fast re-read after a slow one only supplies uptime
evidence and makes `elapsed` longer. `BootRecord.status_reads` lists every
read after completion (`at_s` from the reboot command, `read_s`, `used`),
and calibration prints them when a retry happened.

**R5-3. Renewed proofs need a clean, current record.** `boot-timing.json`
records `rules_version` (`calibrate.CALIBRATION_RULES`, 2 for round 4's
bracketed clock and clean-sample rules; bump it when those rules change).
`phone-qa controls --boot-timing` refuses, before it touches the phone, a
record with another `rules_version` or none, or whose own samples, run
through `compute_paths` again, don't give every gated path the stored A (3
clean bracketed normals and a clean injected sample). A legacy record
never qualifies; run `phone-qa boot-timing`. The rules version is used
instead of the harness version: `controls --boot-timing` exists to renew
the proofs after a harness change, so it can't require the harness that
measured A.

**R5-4. The RAM reference follows the verdict.** `regression.ram_decision`
is the one RAM schedule decision, used by `evaluate` and `update_on_tag`.
The reference moves only on a 3-boot median the verdict itself used. When
the verdict judged bench boot 1 alone, the other rows never move it.
Before, a 1,230 MB reference with rows 1,200 / 1,150 / 1,150 on a one-boot
verdict became 1,150 MB.

**R5-5. Only a complete torch capture can qualify the control.**
`torch_capture` sets `complete` only with exactly two frames, both with a
real `FLASH_STATE` (0-4), and a finite luma pair. `torch_rejections`
returns nothing for an incomplete capture, even when its luma pair fails
the delta. The normal check is unchanged.

**R5-6. Kernel batches cover every boot input (R5-17).** A batch also
counts as a kernel batch, compared with the latest known-good build, when:
- `device/motorola/river/BoardConfig*.mk`,
  `device/motorola/sdm632-common/BoardConfigCommon.mk`,
  `vendor/lineage/config/BoardConfigKernel.mk` or
  `vendor/lineage/build/tasks/kernel.mk` changed (`git diff --name-only`
  between the two commit sets' commits);
- a kernel toolchain pin in `default.xml` changed (projects under
  `prebuilts/clang/`, `prebuilts/gcc/`, `prebuilts/kernel-build-tools`,
  `prebuilts/build-tools`, `prebuilts/tools-lineage`, `prebuilts/misc`);
- the boot image header version differs, or either image's header version
  is above 2 (the command line offsets are only known for v0-v2);
- the kernel's embedded config (`IKCONFIG`, gzip between `IKCFG_ST` and
  `IKCFG_ED` in the decompressed kernel) differs, or only one image has
  one. river's kernel has `CONFIG_IKCONFIG=y`; baseline-1 and baseline-2
  carry the same config.
- or anything above can't be compared (a missing commit set entry, a commit
  git doesn't have, an unreadable image).
The defconfig and every fragment named by `TARGET_KERNEL_CONFIG` resolve
inside the kernel source (`arch/<arch>/configs`), so the kernel commit and
IKCONFIG cover them; the `TARGET_KERNEL_CONFIG` line itself is in
`BoardConfig.mk`.

**Smaller changes.**
- **R5-7, R5-19. Tests.** `qa/tests/_path.py` always makes a fresh
  temporary `$BENCH_HOME`, ignores an inherited `QA_TESTS_BENCH_HOME` or
  `BENCH_HOME`, and removes it at exit. `tests/benchhome.bench_home`
  redirects every module-level path under `$BENCH_HOME` in every `riverqa`
  module (found by scanning them, so a new copy is covered too).
- **R5-8.** `disconnect_gap` and `status_read_s` keep full precision; the
  0.5 s and 1 s bounds apply to them unrounded.
- **R5-9.** The sysrq guard refuses only writes to `/proc/sysrq-trigger`
  (a redirection, `tee`, `dd of=`, or `cp`/`mv`/`ln`/`install` onto it).
  Read-only mentions (`ls`, `stat`, `test -e`, `grep`) are allowed.
- **R5-10.** The session banner matches within 2 flips
  (`bootsig.BANNER_FLIPS`), and only at a printk line start: at most 24
  bytes of prefix after the line start, with at most 2 ASCII letters in it
  (the prefix can be damaged too: river's real banner line reads
  `[    0.\xb000000] Linu\xf8 ver\xf3ion`). `minor version ` (3 flips) and
  message text no longer match. All 12 saved river pstore logs still show
  exactly one banner, at the head.
- **R5-11, R5-15.** `device-bench` halts (exit 3) when `fbmode.recover`
  fails; `phone-qa boot-timing` and `controls --boot-timing` exit 1 with
  `error:`; the `flash` preflight turns a recovery `TimeoutError` into a
  HALT. The obligation stays pending in each case.
- **R5-12. The chain counts on every path.** `chain_survives` is a record
  only. A path whose chain doesn't survive can never qualify (a clean
  sample needs a chain PASS, so it gets no A), and a path with 3 clean
  samples plus one that wasn't, which round 4 marked `survives: false`,
  must not lose its chain. A missing `console-ramoops*` is a read error on
  every path. This replaces A2.1 item 3's "a chain UNVERIFIED does not
  count" and round 4's Gemini 2 exception.
- **R5-13.** No device job (`flash`, `device-bench`, `phone-qa`,
  calibration) starts while a build holds `$BENCH_HOME/.lock`; an
  unreadable build lock also refuses. A build that starts after a device job
  is not blocked by this; the runner builds first and then runs the device
  jobs in sequence.
- **R5-14.** `boot_count_now` returns None (no count, so the sample is not
  clean) when its last read failed, never an older count.

**Phone notes (2026-11-23, after R5, build 22-20261123-UNOFFICIAL-river, no
wipe).**
- `phone-qa boot-timing` qualified every path (exit 0) and stamped
  `rules_version` 2; `calibrate.record_problems` finds nothing wrong with
  the record. Over its 17 reboots the disconnect gap was 0.110-0.149 s and
  the status read 0.081-0.207 s. No read needed a retry, so every
  `host_boot_seconds` is its first completion read. No sample was
  excluded. A is 12.46 s (fastboot) and 12.26 s (`sysrq`); the controls
  were rejected as before (marked by uptime, unmarked by enumeration, the
  adb early reboot by the chain).
- R5-1 on the phone: fb_mode armed as the control does
  (`arm_bootloader_stop`, under the device lock), then the process was
  killed with SIGKILL; the journal kept the obligation, and a guarded
  reboot stopped in fastboot on slot a. `flash --recover` (no pair,
  `known_good_slot` a active) cleared fb_mode in fastboot, set slot a
  active, rebooted once (one USB disconnect, uptime matching it), verified
  slot a and build `baseline-2-7cc61b3f30`, resolved the entry and recorded
  `fb-mode-recovered`. The next reboot went straight to Android, and
  `phone-qa --quick` passed. The candidate-active case (rollback with slot b
  active) is covered by the unit tests, not on the phone: it wipes.

## Code review round 6: changes to R5

Status: specified 2026-11-23 from the round-6 code review of `e407aae`.
Everything in round 5 stands except as changed here. Where this section and
an earlier one disagree, this one wins.

**R6-1. Slot recovery always halts on a failure.** Every failure of
`flash --recover`, `flash --rollback` and rescue on the phone exits 3 with
an `error:` line and a `HALT` line, never a traceback or exit 1, and never
resolves the fb_mode obligation or the pair (`flashing.recovery_halt`):
- `rollback` turns every failure before its reboot (a fastboot error such
  as a failed `--set-active`, a timeout on the way to fastboot, a guard
  failure, an unreachable phone) into `Halt`, so rescue's handler catches
  it too. `clear_then_set_active` does the same for a failed
  `--set-active`.
- `flash --recover` with no pair, a dev slot active and a failed clear
  with an obligation pending stays in fastboot, switches no slot, wipes
  nothing and halts (round 5 let `ClearFailed` escape).
- Before the first baseline (no `slot-state.json`), on the promote path,
  and on the rollback path, any `RuntimeError` or `TimeoutError` halts.
- `flash` turns a `TimeoutError` that still reaches its top level into a
  halt when it runs `--recover` or `--rollback`, or when an fb_mode
  obligation is pending.

**R6-2. Calibration samples carry their provenance.** `CALIBRATION_RULES`
is 3. `calibrate.run` stamps every sample that can set A (each normal
reboot in `raw` and each `inject_measure`) with `rules_version` as it
measures it, with full-precision bracket values (R5-8). A sample without
the current stamp is never clean (`sample_problems`), so it can't set A.
`--reuse-normals` reuses the stored normals only if the record is from this
build, has the current `rules_version` and every normal carries the stamp
(`reuse_problems`); otherwise it logs why and measures fresh normals.
`record_problems` also lists every unstamped sample, so `controls
--boot-timing` refuses such a record. Sol's case: a sample with a 0.5004 s
gap stored as 0.500 s, reused, made A 14.8 s; it now isn't reused and A
comes from fresh samples.

**R6-3. More kernel-batch inputs, and "unparsable" is not "absent".**
- The toolchain pins also cover `external/dtc` (it builds the host `dtc`
  that `kernel.mk` passes as `DTC_EXT`, because `BoardConfigCommon.mk` sets
  `BOARD_KERNEL_SEPARATED_DTBO`), `prebuilts/clang-tools` and
  `prebuilts/rust` (on the kernel build's `PATH` in
  `BoardConfigKernel.mk`). `prebuilts/tools-lineage` was already matched; a
  test checks every one against the real `default.xml`.
- The kernel's embedded config and the DTB appended to `Image.gz-dtb` are
  each a hash, `ABSENT` (read completely, not there) or None (could not be
  parsed). None in either image is a kernel batch; "absent in both" is not.
  A gzip stream that is corrupt or cut short is None for both.
- The appended DTB is compared: the bytes after the kernel's gzip stream
  must be whole FDT blobs back to back (magic `d00dfeed`, `totalsize` at
  least the 40-byte header, ending exactly at the end). On river it is one
  316,706-byte blob, the same in baseline-1, baseline-2 and the current
  stripped build. A raw (not gzip) kernel's end can't be told reliably, so
  its DTB counts as unparsable. On the real images, baseline-2 against
  baseline-1 is still not a kernel batch.

**Smaller changes.**
- **R6-4.** `device-bench --flash` recovers fb_mode with `boot=False` and
  leaves the adb guard to `flash_dev`, which works from fastboot.
- **R6-5.** The build-lock probe takes `$BENCH_HOME/.lock` shared
  (`LOCK_SH|LOCK_NB`); the build holds it exclusive, so two device jobs
  probing at once no longer see each other as a build.
  `flash --merge-started` and `flash --promote`, which time nothing, skip
  the probe (`DeviceLock(check_build=False)`).
- **R6-6.** `boot_info` rejects `page_size + kernel_size` past the end of
  the file; `IKCFG_ED` is searched after `IKCFG_ST`.
- **R6-7.** The build-lock test closes its child's stdout; the suite runs
  clean with `python3 -X dev` (no `ResourceWarning`).
- **R6-8.** `flash --batch` and `flash --promote` that find the phone in
  fastboot with nothing pending no longer leave it there for the next adb
  guard (exit 1). `fbmode.recover(slot=...)` boots only the named slot:
  `known_good_slot` for a batch flash, the pair's target for `--promote`.
  If fastboot reports another slot active it raises before clearing or
  booting, and the caller halts. `--promote` now does its host-side proof
  first and touches the phone only after it; `flash --recover`'s promote
  path goes through the same code.
- **R6-9.** The sysrq guard judges `cp`/`mv`/`ln`/`install` on the
  destination (the last operand, or `-t`): a copy with the trigger only as
  its source is allowed. A destination that names `sysrq-trigger`, or one
  that may be `/proc` (`/proc`, `.`, `..`) with a source named
  `sysrq-trigger`, is refused, and so is anything that can't be parsed.
  Indirect targets (`t=/proc/sysrq-trigger; echo b > $t`, or a symlink
  written later) are out of scope: the threat model is accidental breakage
  by agents that follow the rules, not crafted commands.

**Phone notes (2026-11-23, after R6, build 22-20261123-UNOFFICIAL-river, no
wipe).**
- `phone-qa boot-timing --reuse-normals` refused the stored normals
  ("written under calibration rules 2, not 3; 8 normal sample(s) without
  the rules 3 stamp") and measured fresh ones. It qualified every path
  (exit 0). All 10 samples that can set A carry `rules_version` 3, and
  `record_problems` finds nothing wrong with the record. A is 12.45 s
  (fastboot) and 12.27 s (`sysrq`); no sample was excluded. Over its 17
  reboots the disconnect gap was 0.112-0.158 s and the status read
  0.090-0.206 s, and no read needed a retry. The controls were rejected as
  before.
- `phone-qa --quick --wait` passed (7 passed) on slot a,
  `known_good_slot` a, no pair, journal empty.

## Code review round 7: changes to R6

Status: specified 2026-11-23 from the round-7 code review of `62f73de`.
Everything in round 6 stands except as changed here. Where this section and
an earlier one disagree, this one wins.

**The rule for every recovery path.** No journal obligation is resolved,
and no pair is released or settled, until the booted phone has passed the
guard, the slot check and the build check. A mismatch or a failed read
halts (exit 3) with the obligation and the pair intact and the phone not
wiped. Inputs are checked, and a reserved pair refused, before anything
changes on the phone.

**R7-1. Verify before settling.** After a boot, `fbmode.recover` checks
that the phone passes the adb guard and runs the slot it meant to boot (the
named `slot`, or the slot fastboot reported active), then runs the caller's
`verify` (its build check). Only then is the obligation resolved. fastboot
can report a slot active that the bootloader then falls back from; that now
raises.
- `--promote` (and `--recover`'s promote path) pass the candidate check:
  the guard, the candidate slot, and both build IDs equal to `batch.json`.
- The `flash --batch` preflight passes `verify_slot_build` against the
  known-good record (build ID, or boot hash for a provisional one).
- With no pair, a dev slot active and a clear that succeeded, `flash
  --recover` boots nothing and now leaves the obligation pending.
  `flash --rollback` resolves it after verifying `known_good_slot`.
- `recover(boot=False)` still resolves the obligation right after a
  successful clear. Only a dev flash uses it; that flash writes and boots
  its slot at once, so nothing is left for a later recovery to boot. This
  is the one deliberate exception.

**R7-2. Advice that fits the path.** `fbmode.WrongSlot` carries no advice;
each caller gives its own. A promotion halt (`promote_halt`) says the pair
stays reserved, never to run `flash --rollback` (it wipes and releases the
pair of a merged batch), and that Zak brings the phone back to the
candidate. The batch preflight, which has no pair, points at `flash
--rollback`. The no-pair dev-slot halt no longer suggests `flash --recover`
(Opus minor 2). `Halt` can carry its own advice, which replaces
`recovery_halt`'s generic note.

**R7-3. Check, then touch.** `flash_dev` and `flash_batch` run fb_mode
recovery only after their image checks and the pair check, and `flash`'s
`main` no longer runs it before dispatching. `device-bench --flash` leaves
it to `flash_dev`. A refused flash, including `device-bench --flash` with a
reserved pair, leaves the phone where it was and the obligation pending.

**R7-4. The sysrq guard is an allowlist.** The round-5 and round-6 parsers
are gone. An adb call that mentions `sysrq-trigger` is refused unless it is
`adb shell` (or `exec-out`) and its command
- contains no shell syntax at all: none of `< > | & ; # $ ( ) { } * ? ~ !`,
  backtick, backslash or newline;
- splits with `shlex`; and
- is `ls`, `stat`, `cat` or `grep` with any operands, `test -e PATH`,
  `[ -e PATH ]`, or `cp` whose every mention of the trigger is the source
  `/proc/sysrq-trigger` and whose destination neither mentions it nor may
  be `/proc` (`proc`, `.`, `..`), optionally under `su [user] -c`.

`ln`, `mv`, `install`, `dd`, `tee`, `sh -c`, `busybox`/`toybox` and every
other command that mentions the trigger are refused. `stat
/mnt/cp/sysrq-trigger` is allowed (Opus minor 4). Indirect targets stay out
of scope (round 6).

**Smaller changes.**
- **R7-5.** A failed rescue in `flash --batch` (and its fallback-slot
  rollback, and `device-bench`'s rescue) goes through `recovery_halt`, so it
  prints the `error:` line and the obligation note.
- **R7-6.** `allowance_for` gives no A for a record whose `rules_version`
  is not `CALIBRATION_RULES`, or that has any A-setting sample without the
  current stamp; the uptime gate is then UNVERIFIED. `prove` judges its
  fresh reboots as current-rules data.
- **R7-7.** README says what to do when `flash --batch` halts on a dev slot,
  and that a promotion halt must never be answered with `flash --rollback`.
- **R7-8.** `--promote` with the phone already in Android on the wrong slot
  or build halts (exit 3) with the pair open; it used to exit 1.
- **R7-9.** Covered by the R7-4 allowlist: `(cp /tmp/sysrq-trigger /proc)`,
  a trailing `2>/dev/null` or `# comment` are refused.
- **R7-10.** `reuse_problems` refuses an empty `build_date_utc` on either
  side, even when both are empty.

**Phone notes (2026-11-23, after R7, build 22-20261123-UNOFFICIAL-river, no
wipe).**
- `phone-qa --quick` passed (7 passed) before and after calibration.
- `phone-qa boot-timing` (fresh normals) qualified every path (exit 0). The
  proofs ran under R7-6's check: `prove` judged them as current-rules data,
  and the stored record has `rules_version` 3, no unstamped sample, and
  `record_problems` finds nothing, so `allowance_for` gives A = 12.47 s
  (fastboot) and 12.29 s (`sysrq`). No sample was excluded. Over its 17
  reboots the disconnect gap was 0.118-0.158 s and the status read
  0.078-0.155 s. Every `sysrq` reboot went through the R7-4 allowlist
  guard (only `reboot_sysrq` writes the trigger). The controls were
  rejected as before: the marked extra pass by uptime, the unmarked one by
  the enumeration rule, and the adb early reboot by the chain.
- Final state: slot a, `known_good_slot` a, no pair, journal empty.

## Code review round 8: changes to R7

Status: specified 2026-11-23 from the round-8 code review of `1897d45`.
Everything in round 7 stands except as changed here. Where this section and
an earlier one disagree, this one wins. Round 7's "one deliberate
exception" (`recover(boot=False)` settling after the clear) is gone.

**The single rule (R8-1).** An fb_mode obligation is resolved in one place,
`fbmode.settle(proof)`, and only with a `fbmode.VerifiedBoot`.
- `fbmode.verify_boot(dev, want)` is the only function that makes a
  `VerifiedBoot` (the dataclass is sealed: any other construction raises).
  On the booted phone, in this order, it runs the adb guard (serial
  ZY225DPT7F, product river), reads the boot status (boot completed,
  `boot_id`, uptime), reads `ro.boot.slot_suffix`, reads both build IDs
  (`ro.river.build_id` from `/system/build.prop` and
  `ro.vendor.river.build_id` from `/vendor/build.prop`, as root), and
  hashes `boot_<slot>` when `want` asks for it. Any failed read or mismatch
  raises. `boot_id` is read before the slot and builds, so `settle`'s
  `boot_id` check covers every read.
- `want` (`fbmode.Want`) says what the phone must be: the slot, both build
  IDs (or, for a provisional known-good record or an unstamped dev build,
  a boot hash instead), an optional boot hash to compare, and whether to
  record the hash for the caller.
- `settle` refuses unless the proof is a `VerifiedBoot` of this phone, the
  entry was stamped cleared (`cleared_at`, written after a successful
  `fb_mode_clear`), the verified boot began after that clear (host time of
  the read minus the phone's uptime), and the phone's `boot_id`, read once
  more, is still the proof's.
- `Journal.resolve` refuses an fb_mode entry. Only `settle` calls the
  journal's private resolver, which a unit test checks by parsing
  `qa/lib`. The same test pins the set of `settle` callers (below) and
  that only `verify_boot` builds a `VerifiedBoot`.
- The journal reads and writes `journal.json` on every call. Before, a
  `Journal` object kept its entries in memory, so phone-qa's run journal
  could write back a list from before the `sysrq` control armed fb_mode
  through another object, and drop the obligation.

**What the entry records.** `fbmode.arm(dev, slot, build_ids)` refuses with
an obligation already pending and records `serial`, `armed_at`, the slot,
both build IDs and `cleared_at: null`. The `sysrq` extra-pass control
reads the slot and IDs in Android before it goes to the bootloader to arm;
an unstamped build can't be armed. Generic recovery (`phone-qa recover`,
the phone-qa preflight and `--quick`, calibration, journal restore,
`device-bench`, and `flash --recover` before the first baseline) verifies
against this identity, not against whatever fastboot reports now: another
active slot is a `WrongSlot` before anything is cleared, a fallback or
another build fails `verify_boot`, and both halt (exit 3) with the entry
pending. An entry without an identity (armed before round 8) can't be
settled by generic recovery; a dev or batch flash, or `flash --rollback`,
settles it with its own proof.

**Who settles, with what proof.**

| Caller | Proof it passes to `settle` |
| --- | --- |
| `fbmode.recover` (generic recovery; `flash --batch` preflight; `--promote` and `--recover`'s promote path) | `verify_boot` after its own boot, against the caller's `want` or the armed identity |
| `flashing.rollback` (`flash --rollback`, rescue, `--recover`'s rollback path) | `verify_boot` of `known_good_slot`: both build IDs, or the boot hash for a provisional record |
| `flashing.recover_fb_mode_to_known_good` (no-pair `--recover`) | `verify_boot` of `known_good_slot`, as for rollback |
| `flashing.flash_dev` (dev flash, `device-bench --flash`) | `verify_boot` of the slot it wrote: both build IDs from the images' `build.prop`, or, unstamped, the hash of `boot.img` against the start of `boot_<slot>` |
| `reboots.planned_reboot` (the `sysrq` extra-pass control) | `verify_boot` against the armed identity, after every timing value and USB event was taken |

Promotion passes the candidate slot, both build IDs from `batch.json` and
`hash_boot`, and promotes with the proof's build ID and hash. The batch
preflight passes `known_good_slot`, its build IDs, the recorded boot hash
and `hash_boot`, and reserves the pair with the proof's hash. Neither reads
the phone again after `settle`: a timeout on any of those reads halts with
the pair and the obligation intact.

**No clear-only settlement.** `fbmode.clear` (was `clear_in_fastboot`) and
`fbmode.recover(boot=False)` clear and stamp, and never settle.
`write_images` clears and never settles. A dev flash settles only after its
boot is verified; `flash --no-reboot` leaves the entry pending and says so;
a kill before the first image write leaves it pending (the active slot is
still the armed one, so generic recovery settles it). `disarm` is gone.

**R8-2. An unreadable active slot.** `getvar current-slot` that gives no
slot raises `fbmode.SlotUnreadable` ("cannot read the active slot; recheck
with `fastboot -s ZY225DPT7F getvar current-slot`") before fb_mode is
cleared. It is never a `WrongSlot` and never gets rollback advice. Rollback
advice is only for a confirmed other slot: `WrongSlot` in fastboot, or
`SlotMismatch` from `verify_boot` in Android. With no pair, `flash
--recover` on a confirmed dev slot clears fb_mode and halts (exit 3, was
exit 1), the phone in fastboot and the entry pending.

**R8-3. Disk first.** `slots.preflight(state, batch_id, kind)` is
host-only (it no longer takes the running slot; `verify_boot` checks that
on the phone). `flash_batch` runs it after the batch, image and control
checks and before any phone call, so a missing slot-state for a non-
baseline batch, a missing closed baseline, an unfinished baseline
checkpoint or a reserved pair refuse with the phone untouched.

**R8-4. Messages.** The no-pair dev-slot halt says to run `flash --rollback`
(it wipes) or to flash a dev build again (`flash stripped`), which settles
the obligation without a wipe once it has verified the slot it boots. A
`ClearFailed` during `flash --rollback` ends with "flash --rollback
finishes it" (`Halt.advice` is set from the command the message names).
`recovery_halt` takes an empty advice to mean "the message said it all".

**R8-5. The sysrq copy.** The only `cp` the guard allows is exactly `cp
/proc/sysrq-trigger DEST`, no options, `DEST` free of `sysrq-trigger` and
not `/proc`, `.`, `..` or `/` (optionally under `su [user] -c`). That
closes `-pt`/`-rt` clusters and Toybox `cp -s`.

**Exit codes.** `phone-qa recover`, `phone-qa --quick`, `phone-qa
boot-timing` and `phone-qa controls --boot-timing` exit 3 (HALT) when fb_mode
recovery fails with the obligation still pending, and 1 otherwise.

**State-change ordering.** Each step, then the state left if it fails or
the host dies right after it.

1. `fbmode.recover`, boot case.
   1. Read the entry and `fastboot devices`. Fail: nothing changed.
   2. `adb reboot bootloader` (guarded) if in Android. Die: entry pending,
      fb_mode set, phone in fastboot.
   3. `getvar current-slot`: unreadable → `SlotUnreadable`; another slot
      than `want` or the armed one → `WrongSlot`. Fail: entry pending, fb_mode
      not cleared, phone in fastboot.
   4. `fb_mode_clear`, then stamp `cleared_at`. Fail: entry pending, fb_mode
      set. Die between: entry pending, unstamped (a later `settle` refuses
      until a clear is stamped again).
   5. `fastboot reboot`, wait for adb and boot completed. Fail: entry
      pending, stamped, phone booting or hung.
   6. `verify_boot`. Fail: entry pending, phone in Android.
   7. `settle` (`boot_id` again, then resolve). Die before the resolve: the
      entry stays; the next recovery redoes 2-7.
2. `fbmode.recover(boot=False)` (dev flash): 1-4 only; the entry stays.
3. `flash_dev`: input and pair checks (phone untouched), then row 2, then
   `write_images` (clear and stamp, flash,
   `--set-active`, optional wipe), then boot, `verify_boot` of the target,
   `settle`. Die before the first write: entry pending, active slot still
   the armed one. Die after `--set-active`: entry pending; generic recovery
   halts on `WrongSlot`; the next `flash stripped` or `flash --rollback`
   settles it. `--no-reboot`: entry pending, said.
4. `flash_batch`: batch, image, control checks; `slots.preflight` (disk);
   all before the phone. Then row 1 with `want` = `known_good_slot`, build
   IDs, boot hash, `hash_boot` (or, before the first baseline, generic
   recovery, then `first_run`). `WrongSlot` or `SlotMismatch`: HALT with
   rollback advice; anything else: HALT. Both: no pair, entry pending.
   Then `reserve` with the proof's hash, `save_state`, the writes.
5. `promote_batch`: host proof (phone untouched), then row 1 with `want` =
   candidate, both IDs, `hash_boot`. Any failure: `promote_halt`, pair
   reserved, entry pending. Then `slots.promote` with the proof's values and
   `save_state`. Die between `settle` and `save_state`: entry resolved,
   pair reserved; rerunning `--promote` verifies the phone in Android and
   promotes.
6. `rollback`: `to_fastboot`, `fastboot_guard`, clear and stamp,
   `--set-active` fallback, erase userdata, record the wipe, reboot. Any
   failure: HALT, pair and entry as they were (`ClearFailed` still sets the
   fallback active). Then wait, `verify_boot` of `known_good_slot`,
   `settle`: failure → HALT, pair reserved, entry pending. Then the pair is
   released (`rolled_back`) and saved.
7. `recover_fb_mode_to_known_good` (no pair): `to_fastboot`, guard, read the
   slot. Unreadable: HALT before the clear. Confirmed dev slot: clear and
   stamp, HALT, entry pending, phone in fastboot. `known_good_slot`: clear
   and stamp, `--set-active`, reboot, `verify_boot`, `settle`, history.
8. `planned_reboot`, `sysrq` extra pass: in Android read slot and IDs; `adb
   reboot bootloader`; `arm` (journal, then `fb_mode_set`); `fastboot
   continue`; marker; sysrq; the stop; clear and stamp; `fastboot reboot`;
   boot status; timing and USB data; `verify_boot` against the armed
   identity; `settle`. A failure before the clear: `clear_fb_mode` runs row
   1. A failure after it: the entry stays for the next recovery.
9. Journal restore (`_fb_mode`): row 1, then re-read the journal;
   `restore_entry` never resolves an fb_mode entry itself.

**Phone notes (2026-11-23, after R8, build 22-20261123-UNOFFICIAL-river, no
wipe).**
- `phone-qa --quick` passed (7 passed) before the checks and, with `--wait`,
  after them.
- `phone-qa boot-timing` (fresh normals) qualified every path (exit 0):
  A = 12.45 s (fastboot, range 10.27-14.64, extra pass 4.404 s) and 12.58 s
  (`sysrq`, range 10.72-14.45, extra pass 4.355 s). The record has
  `rules_version` 3 and no problems. Over its 17 reboots the disconnect
  gap was 0.116-0.160 s and the status read 0.082-0.177 s. Each of the
  three injected `sysrq` passes armed fb_mode with the identity read in
  Android (slot a, `baseline-2-7cc61b3f30` twice), cleared it at the stop,
  and settled only with a `VerifiedBoot` of the next boot (three "fb_mode
  obligation settled: verified slot a, ..." lines, each with a new
  `boot_id`). The controls were rejected as before.
- fb_mode kill test, with a watcher on `journal.json`: armed as the control
  does, then SIGKILL with fb_mode set (entry pending, `cleared_at` null);
  `phone-qa recover` killed the moment the clear was stamped (phone left in
  fastboot, fb_mode clear, entry still pending, `cleared_at` set); `phone-qa
  recover` again: it stamped a new clear, booted slot a, verified slot and
  both build IDs, and only then resolved the entry (the watcher saw it go
  with the phone back in adb). Exit 0.
- Final state: slot a, `known_good_slot` a, no pair, journal empty.

## Code review round 9: changes to R8

Status: specified 2026-11-23 from the round-9 code review of `100df60`.
Round 8 stands except as changed here: one proof type (`VerifiedBoot`, sealed
by `verify_boot`), one settle (`fbmode.settle`), and the same settle callers.
Every change is on the paths before the first baseline and on the bootstrap.
Where this section and an earlier one disagree, this one wins.

**R9-1. The first baseline is seeded from its proof.** With no
`slot-state.json`, `flash --batch` calls `first_run_proof`:
- With an obligation pending, `recover` runs with `want` = the armed identity
  plus `hash_boot`. It verifies slot, both build IDs and the whole
  `boot_<slot>` hash, then settles with that proof.
- With nothing pending, `recover` only boots a phone found in fastboot
  (nothing to settle). Then `running_identity` reads the slot and both build
  IDs in Android, and `verify_boot` checks them with `hash_boot`.
- `slots.first_run(proof.slot, proof.boot_sha)` seeds the state, and the pair
  is reserved with `proof.boot_sha`. Nothing is read after the settle.
- A failed read (a hash timeout, say) halts (exit 3) with the obligation
  pending and no `slot-state.json`.

**R9-2. Only a path that boots needs the armed identity.** `recover` decides
whether it will boot before it changes anything, and reads the armed identity
only on a path that boots and has no `want`. `recover(boot=False)` (the dev
flash) only clears and stamps, so an entry armed before round 8 (no slot, no
build IDs) no longer stops a dev flash. The dev flash then settles it with
the verified boot of the slot it wrote. On a path that boots, such an entry
still raises before anything is changed.

**Old entries (corrects round 8).** An entry with no identity is settled:
- by a dev flash (`flash stripped`), always;
- with a `slot-state.json`, also by `flash --rollback` and by a batch flash,
  which verify `known_good_slot` against its record.
Generic recovery and the first-baseline flash halt (exit 3) on it, with this
advice.

**R9-3. Stamped builds only for calibration and the first baseline.** There
is no boot-hash armed identity. `fbmode.running_identity(dev)` runs the adb
guard and reads the running slot and both build IDs. It raises
`fbmode.Unstamped` ("running build is unstamped; dev-flash a build from this
tree first (`flash stripped`)") if either ID is missing. Three paths use it:
- `phone-qa boot-timing` and `phone-qa controls --boot-timing` exit 1 on it.
  The check runs after `recover_fb_mode`, which reboots only a phone left in
  fastboot or with an obligation pending, and before any calibration reboot.
- The first-baseline flash, with nothing pending, exits 1 with nothing
  written.
- The `sysrq` arm, which says "cannot arm fb_mode".

Every build from this tree is stamped by `device.mk` (device PR #7). The
bootstrap now starts with a dev flash of such a build (see below), and a dev
flash needs no calibration: it times nothing and judges no signal.

`build_id_verdict`'s unstamped-baseline fallback (`qa.py`) is unchanged. It
judges the build being flashed, not the running one, and a baseline built
from `river-22.2` HEAD is stamped anyway.

**R9-4. A dev flash reads the slot before it clears.** `flash_dev` reads the
active slot first: `ro.boot.slot_suffix` in Android, else `getvar
current-slot` in fastboot. Before the first baseline that slot picks the
write target, so an unreadable one halts (exit 3, `SlotUnreadable`) with
fb_mode untouched. With a `slot-state.json` the target is the other slot of
`known_good_slot`, and the slot read is only logged.

**R9-5. Nothing read after a dev flash settles; no traceback.**
- `ro.lineage.display.version` is read between `verify_boot` and `settle`.
- `write_images` still clears fb_mode, which also clears an fb_mode set with
  no obligation. With an obligation pending, a failure of that second clear
  is a `ClearFailed`. That, and any other fastboot failure in the writes,
  halts (exit 3) with the obligation pending. Before, it escaped `main` as a
  traceback. The second case (a failed `fastboot flash`) was found in the
  self-review. With nothing pending, these failures exit 1 with an
  `error:` line.

**R9-7. The freshness check uses the host's boot clock.** `cleared_at` is
now `{"boottime", "host_boot_id", "wall"}`:
- `boottime` is the host's `CLOCK_BOOTTIME` and `host_boot_id` is
  `/proc/sys/kernel/random/boot_id`. `wall` is only for people.
- `BootStatus.host_boottime` is `CLOCK_BOOTTIME` just before the status read.
  `VerifiedBoot.booted_at` is that value minus the phone's uptime, so at the
  latest the boot's start.
- A wall-clock step (NTP, a date change) moves neither value.
- `settle` refuses in these cases:
  - the host booted again after the clear, because the two clocks can't be
    compared;
  - a round-8 float stamp;
  - a boot that began before the clear.
- A rerun of the recovery clears and stamps again, so each refusal fails
  safe.

**R9-8. `replace` can't carry the seal.** `VerifiedBoot._seal` is
`field(init=False)`. `verify_boot` sets it with `object.__setattr__` after
construction, and nothing else sets it. So a direct construction, or
`dataclasses.replace` of a sealed proof, gives an unsealed object, and
`settle` refuses it (`TypeError`). The single-settle test now also checks
two things: only `verify_boot` sets `_seal`, and no module but `fbmode`
mentions it.

**R9-9.** A `getprop` that gives nothing while arming is the clean error
"cannot arm fb_mode: cannot read the running slot ('')", not an
`AttributeError`.

**R9-10.** `flash ... --no-reboot` with an obligation pending says what
happens next. Until a flash that boots, or `flash --rollback`, verifies its
slot, `phone-qa --quick` halts (exit 3), because the phone no longer runs the
armed build.

**R9-11.** The `--set-active` and `erase userdata` halts in rollback name
their own command ("flash --rollback finishes it").

**Who settles, with what proof.** The callers are the same as in round 8,
and the AST test still pins them. One `want` changed: the first-baseline
`flash --batch` with an obligation pending passes the armed identity with
`hash_boot` (R9-1).

| Caller | Proof it passes to `settle` |
| --- | --- |
| `fbmode.recover` (generic recovery; `flash --batch` preflight, first baseline included; `--promote` and `--recover`'s promote path) | `verify_boot` after its own boot, against the caller's `want` or the armed identity |
| `flashing.rollback` | `verify_boot` of `known_good_slot` (build IDs, or the boot hash for a provisional record) |
| `flashing.recover_fb_mode_to_known_good` | as for rollback |
| `flashing.flash_dev` (dev flash, `device-bench --flash`) | `verify_boot` of the slot it wrote |
| `reboots.planned_reboot` (`sysrq` extra pass) | `verify_boot` against the armed identity |

**State-change ordering (rows changed or added; the rest of round 8's table
stands).**

1. `fbmode.recover`, boot case. Step 1 is now: read the entry and `fastboot
   devices`, decide whether this path boots, and on a path that boots with no
   `want`, read the armed identity. No identity: raise, nothing changed.
   Steps 2 to 7 are unchanged. The `cleared_at` stamp in step 4 is the host
   boot clock (R9-7).
2. `fbmode.recover(boot=False)`, exactly as the code runs it:
   1. Read the entry and `fastboot devices`. Nothing pending and not in
      fastboot: return, nothing changed.
   2. In Android (an obligation pending): check the adb state (neither adb
      nor fastboot: raise, nothing changed), then `adb reboot bootloader`
      (guarded) and wait for fastboot. Die: entry pending, fb_mode set, phone
      in fastboot.
   3. No `getvar current-slot` and no armed identity. This path boots
      nothing, and its caller has already read the slot (row 3).
   4. `fb_mode_clear`, then the stamp. Fail: entry pending, fb_mode set. Die
      between: entry pending and unstamped.
   5. Return. The entry stays pending and the phone stays in fastboot.
3. `flash_dev`:
   1. Images, product and pair checks. The phone is untouched.
   2. Read the active slot (R9-4): in Android, the guard and
      `ro.boot.slot_suffix`; otherwise `to_fastboot`, `fastboot_guard` and
      `getvar current-slot`. With no `slot-state.json`, an unreadable slot
      halts (3) with nothing changed.
   3. Row 2.
   4. `write_images`: guard, clear and stamp, flash, `--set-active`,
      optional wipe. `ClearFailed` (before any write) or any other fastboot
      failure: HALT 3 with the entry pending, or exit 1 if nothing is
      pending. `--no-reboot` ends here: entry pending, and the output says
      `--quick` halts until a verified boot (R9-10).
   5. Reboot. Timeout: HALT 3 with the entry pending.
   6. `verify_boot` of the target, read the version, `settle`. Fail: HALT 3
      with the entry pending, or exit 1 if nothing was pending.
   7. History and log. Nothing is read from the phone.

   States left:
   - Die before the first write: entry pending, active slot unchanged.
   - Die after `--set-active`: entry pending. Generic recovery halts; the
     next dev flash or `flash --rollback` settles it.
4. `flash_batch` with a `slot-state.json`: unchanged from round 8.

4a. `flash_batch`, first baseline (new):
   1. Batch, image and control checks, then `slots.preflight` (disk only).
      The phone is untouched.
   2. With an obligation pending: row 1 with `want` = the armed identity
      plus `hash_boot`.
   3. With nothing pending: row 1 with no `want` (it boots only a phone
      found in fastboot and settles nothing). Then `running_identity`, then
      `verify_boot` of that slot and those IDs with `hash_boot`.
   4. Any failure in steps 2 and 3: HALT 3, the entry pending, no
      `slot-state.json`. An unstamped running build exits 1 with nothing
      written.
   5. `first_run(proof.slot, proof.boot_sha)` and `save_state`. Die between
      `settle` and `save_state`: entry resolved, no state. A rerun has
      nothing pending, so it verifies the phone as it is and seeds the
      state.
   6. `reserve` with `proof.boot_sha`, `save_state`, then the writes, as
      in row 4.
8. `planned_reboot`, `sysrq` extra pass. Step 1 is now `running_identity`:
   the guard, the slot and both IDs. Unreadable or unstamped: "cannot arm
   fb_mode", nothing changed. The rest is unchanged.

10. Calibration (`boot-timing`, `controls --boot-timing`) (new):
   1. `recover_fb_mode`: row 1 when an obligation is pending or the phone is
      in fastboot; otherwise nothing.
   2. `running_identity`. Unstamped or unreadable: exit 1 before any
      calibration reboot.
   3. The measurements.

**Bootstrap order (corrects "Calibration and bootstrap").** The order is:
1. a dev flash of a stamped build from this tree (`scripts/build`, then
   `flash stripped`);
2. `phone-qa boot-timing`;
3. the baseline batch;
4. Perch calibration, thresholds and controls, as before.

The dev flash needs no `boot-timing.json`, and before the first baseline it
writes the slot that isn't active. Its verified boot settles any leftover
obligation, old ones included (R9-2).

## Code review round 10: changes to R9

Status: specified 2026-11-23 from the round-10 code review of `ceb1574`.
Round 9 stands except as changed here. The round-8 structure is untouched:
one proof type (`VerifiedBoot`, sealed by `verify_boot`), one settle
(`fbmode.settle`) and the same five settle callers, which the AST test
still pins. The three majors are on the bootstrap before the first
baseline. They are closed by refusing the risky combinations before any
phone write, not by new recovery paths. Where this section and an earlier
one disagree, this one wins.

**R10-1. The `unstamped` sentinel is no stamp.** `device.mk` sets
`RIVER_BUILD_ID ?= unstamped`, so a build made without `scripts/build`
says `unstamped` on both partitions. `fbmode.stamp()` maps that sentinel,
and an empty value, to None, as `slots`, `stage` and `qa` already did. It
applies in these places:
- `partition_build_ids`, so `running_identity` raises `Unstamped` when
  either partition has the sentinel. Calibration, the `sysrq` arm and the
  first-baseline proof with nothing pending refuse such a build before
  any reboot or write.
- `product_want`: a product with the sentinel on either partition is
  verified by its boot hash.
- `known_good_want`.
- `Want`, which rejects the sentinel as a build ID, so `arm` can't record
  it.

An entry already armed with the sentinel for either ID is treated like an
entry from before round 8: `armed_want` raises, so generic recovery and
the first-baseline flash refuse it (exit 3) before any phone call. A dev
flash's own proof settles it, and so does a known-good proof (`flash
--rollback`, or `flash --recover` with no pair on `known_good_slot`).

**R10-2. Bootstrap step 0 wipes.** Step 0 is `flash stripped --wipe`
whenever the running build is not from this tree (an official nightly, or
any build signed with other keys): builds from this tree are signed with
its test keys, and over other keys the phone boot-loops unless userdata is
erased. Steps 1 and 2 need no data, and step 2 wipes again. The stamp comes
from device PR #7 (`9851f11`). That PR and harness PR #10 merge together,
in one `change merge` set; until then a checkout builds with both
`qa-harness` branches. README and `workspace/AGENTS.md` say this. This
corrects step 1 of round 9's "Bootstrap order".

**R10-3. Before the first baseline, no flash leaves the armed slot as a
target.** With no `slot-state.json`, the slot an obligation was armed on is
the only slot known to boot, and a dev flash writes the slot that is not
active. So `flash_dev` refuses, before any phone write:
- `--no-reboot` while an obligation is pending. Exit 1, host-only: the
  phone is untouched. It would leave the other slot active with the
  obligation pending, and the next flash would write the armed slot.
- A flash whose target is the armed slot (`fbmode.armed_slot`, recorded
  even when the IDs are the sentinel). This happens when the other slot
  is active, for example after a flash that stopped after `--set-active`.
  HALT 3, after the slot read and before anything is cleared or written.

The advice in this state (`flashing.armed_way_back`) never names `flash
--rollback`, which needs `slot-state.json`, nor another flash, which could
write the armed slot. If another slot is active, it first says to set the
armed slot active by hand (`fastboot -s ZY225DPT7F
--set-active=<slot>`). Then it says `phone-qa recover`, which boots the
armed slot, verifies it against the armed identity and settles the
obligation. For a sentinel entry, which generic recovery refuses, it says
`flash stripped` with the armed slot active: that writes the other slot
and settles the obligation with its verified boot. The halts after the
write use the same advice: a boot timeout, a failed verify, a failed write
and a failed erase. An entry from before round 8 records no slot, so
nothing can protect its slot. Its advice is a dev flash that boots.

**R10-4. Exit codes in the dev flash.**
- The slot read before the clear is one `try`:
  - an `Unreachable` goes to `main`, which halts (3);
  - with an obligation pending, any failure, a guard failure included,
    halts (3);
  - with no `slot-state.json`, any failure but a guard failure halts (3),
    as an unreadable slot does (R9-4);
  - otherwise it exits 1 with an `error:` line, never a traceback.
- `Unreachable` from `to_fastboot` or the writes is re-raised past
  `write_failed`, so `main` halts (3) in `--no-reboot` too.
- `device-bench --flash`, which calls `flash_dev` directly, halts (3) on
  it as well. This was found in the self-review.

**R10-5.** A failed `fastboot erase userdata` after `--set-active` in a
dev flash raises `flashing.WipeFailed` and always halts (3): the new slot
is active and unwiped. With nothing pending, the advice is to erase by hand
and reboot, or to set the previous slot active. With an obligation
pending, it is the R10-3 advice (before the first baseline) or "flash
again, or `flash --rollback`".

**R10-6.** `SlotUnreadable` names its read. With `getprop
ro.boot.slot_suffix` in Android, the recheck command is `adb -s
ZY225DPT7F shell getprop ro.boot.slot_suffix`. With fastboot, it is
`fastboot -s ZY225DPT7F getvar current-slot`, as before. `settle` refuses a
`bool` as the stamp's `boottime`.

**R10-7.** After `flash ... --no-reboot` with an obligation pending (only
possible with a `slot-state.json` now), the note depends on what was
written:
- If the slot written and its build IDs are the armed identity, it says
  that generic recovery (`phone-qa recover`, or the preflight of
  `--quick`) boots that slot, verifies it and settles the obligation. A
  test runs that recovery.
- Otherwise it keeps the R9-10 warning: `--quick` halts until a flash that
  boots, or `flash --rollback`, verifies its slot.

**Old entries (corrects round 9).** These are an entry from before round 8
(no slot, no IDs) and an entry with the sentinel (a slot, but no usable
IDs). Generic recovery and the first-baseline flash refuse both (exit 3).
What settles them depends on the slot state:
- **A dev flash** (`flash stripped`, without `--no-reboot` before the
  first baseline) settles them with the verified boot of the slot it
  writes, but only with no pair reserved, because a pair refuses every
  flash. Before the first baseline, a dev flash of a sentinel entry runs
  only with the armed slot active (R10-3).
- **With a `slot-state.json`, `flash --rollback`** settles them. It
  releases a pair if one is reserved (wiping userdata) and otherwise
  returns to `known_good_slot`.
- **With a `slot-state.json` and no pair:** a batch flash (from
  `known_good_slot`) settles them. So does `flash --recover` when
  `known_good_slot` is active.
- **With a pair:** `flash --recover` decides first (promote, roll back or
  halt). Only its rollback or promotion settles them.

**Who settles, with what proof.** Unchanged from round 9:

| Caller | Paths | Proof passed |
| --- | --- | --- |
| `fbmode.recover` | generic recovery, the batch preflight (first baseline included), `--promote` and `--recover`'s promote path | `verify_boot` after its own boot, against the caller's `want` or the armed identity (read only on a path that boots; the sentinel is no identity) |
| `flashing.rollback` | `flash --rollback`, rescue, `--recover`'s rollback | `verify_boot` of `known_good_slot` |
| `flashing.recover_fb_mode_to_known_good` | no-pair `flash --recover` | as for rollback |
| `flashing.flash_dev` | dev flash, `device-bench --flash` | `verify_boot` of the slot it wrote |
| `reboots.planned_reboot` | the `sysrq` extra pass | `verify_boot` against the armed identity |

**State-change ordering (rows changed; the rest of rounds 8 and 9
stand).**

1. `fbmode.recover`, boot case, step 1: an entry whose IDs are the
   sentinel has no armed identity, like one from before round 8. It raises
   with nothing changed.
3. `flash_dev`:
   1. Images, product and pair checks. Then, with no `slot-state.json`,
      an obligation pending and `--no-reboot`: exit 1 (R10-3). The phone
      is untouched.
   2. Read the active slot (R9-4). `Unreachable`: HALT 3 (from `main`).
      Any other failure: HALT 3 with an obligation pending, or before the
      first baseline (except a guard failure); otherwise exit 1. Nothing
      changed (R10-4).
   3. Pick the target. Before the first baseline: an unreadable slot is
      HALT 3 (`SlotUnreadable`, naming its read), and a target that is the
      armed slot is HALT 3 (R10-3). Nothing changed.
   4. Row 2 (clear and stamp; an entry stays pending only if one existed).
   5. `write_images`: guard, clear and stamp, flash, `--set-active`,
      optional wipe.
      - A failed clear with no obligation pending is logged and ignored,
        and the flash goes on (`fbmode.clear`). With an obligation it is
        `ClearFailed`, before any write: HALT 3, entry pending.
      - Any other fastboot failure: HALT 3 with an entry pending, else
        exit 1.
      - A failed erase after `--set-active` (`WipeFailed`): HALT 3 always
        (R10-5).
      - `Unreachable`: HALT 3 (from `main`).
      - `--no-reboot` ends here (only with a `slot-state.json` if an entry
        is pending). The entry stays pending, with the R10-7 note.
   6. Reboot. Timeout: HALT 3 with an entry pending, else exit 1.
   7. `verify_boot` of the target, the version read, `settle`. Fail: HALT
      3 with an entry pending, else exit 1.
   8. History and log. Nothing is read from the phone.

   Before the first baseline, the armed-slot refusal and every halt after
   the first write, with an entry pending, give the R10-3 advice. Halts
   before any write (a failed slot read, an unreadable slot, a failed
   clear) give `recovery_halt`'s default advice or `ClearFailed`'s
   manual-clear advice: nothing was written, so the armed slot is intact
   (round 11, Gemini m3).

   States left:
   - Die before the first write: entry pending, active slot unchanged.
   - Die after `--set-active`, before the first baseline: entry pending,
     the other slot active. Generic recovery halts on `WrongSlot`, and the
     next dev flash refuses (R10-3). Set the armed slot active by hand,
     then run `phone-qa recover`.
   - Die after `--set-active`, with a `slot-state.json`: the next dev
     flash or `flash --rollback` settles it, as in round 9.

4a. `flash_batch`, first baseline:
   - Step 2 is unchanged.
   - Step 3: `running_identity` refuses the sentinel on either partition
     (exit 1, nothing written).
   - Step 4: any failure in steps 2 and 3 is HALT 3 with no
     `slot-state.json`. An entry stays pending only if one existed. A
     sentinel entry halts in step 2 before any phone call.
8. `planned_reboot`, `sysrq` extra pass, step 1: the sentinel on either
   partition is "cannot arm fb_mode: running build is unstamped", with
   nothing changed.
10. Calibration, step 2: the sentinel on either partition exits 1 before
    any calibration reboot.

## Code review round 11: changes to R10

Round 11 (head `0fb68fa`): Opus 9 and Gemini approve; Sol 7 objects with
two majors, both in the dev flash. Fixed in `flashing.py` without new
recovery or settle paths; the five `fbmode.settle` callers are unchanged.

**R11-1. A slotless entry before the first baseline.** An obligation from
before round 8 records no slot. With no `slot-state.json` and slot b left
active by an interrupted flash, slot a may be the only slot that boots,
and the R10 advice ("a dev flash that boots") wrote all four a
partitions. Now, before the first baseline with such an entry pending, a
dev flash goes on only if it found the phone running Android: that shows
the active slot boots, and the target is the other slot. In fastboot it
halts (3) after the slot read and before any clear or write. The advice
(`armed_way_back`) names no flash until a slot has been shown to boot:
clear fb_mode by hand, set a slot active, reboot, check that it reaches
Android, leave it active and run `flash stripped`; if no slot boots, stop
and tell Zak.

Ordering row 3 gains a step 3a: before the first baseline, with a
slotless entry pending and the slot read from fastboot, HALT 3 with
nothing changed.

**R11-2. Timeouts at `--set-active` and the wipe.** `write_images` turns
a `TimeoutError` from `--set-active` or from `fastboot erase userdata`
into `WipeFailed`, so the dev flash halts (3) in both `--no-reboot` and
the rebooting mode, saying the slot is, or may be, active and its wipe is
not confirmed. Rollback's own erase already halted on any failure (R6-1).

**Minors.** The README describes the slot-read exit codes as R10-4 does
(Opus m1). Advice to set a slot active from Android says to reboot to the
bootloader first (Opus m2).

Regressions: `test_round9.test_legacy_obligation_in_fastboot_writes_nothing`
and `test_round10.R11_2WriteTimeouts` (8 subcases). At `0fb68fa` the first
wrote `boot_a` and the rest; the second exited 1 in every subcase.


Gemini's round-11 minors: the `Unstamped` message names `flash stripped
--wipe` for a phone running a build with other keys (m1); the erase
timeout (m2) is R11-2; row 3's advice wording is corrected (m3).

## Code review round 12: changes to R11

Round 12 (head `c4064da`): Opus 9 and Gemini approve; Sol 7 objects with
two majors and says no other blocker or major remains. Fixed in
`flashing.flash_dev` without new recovery or settle paths; the five
`fbmode.settle` callers are unchanged.

**R12-1. Before the first baseline, the active slot must be shown to
boot.** R11-1 took "adb answers with a slot suffix" as proof that the
active slot boots. A slot can answer adb and never complete boot, and the
same gap existed with nothing pending: two pre-baseline `--no-reboot`
flashes both returned 0, and the second wrote the original slot before the
first target had booted. Now, with no `slot-state.json`, the dev flash
reads `sys.boot_completed` with the slot suffix and writes only when the
phone is in Android and the boot has completed. Otherwise it refuses after
the slot read and before any clear or write: HALT 3 with an obligation
pending (the `armed_way_back` advice), exit 1 with nothing pending ("boot
the phone to Android on a slot that works, then flash again"). This
replaces R11-1's fastboot-only refusal. Before the first baseline with an
obligation pending, the pre-write halts (failed slot read, unreadable
slot, failed clear) also give `armed_way_back`, never `flash --recover`.

Ordering row 3, step 3a, now reads: before the first baseline, unless the
slot was read from Android with `sys.boot_completed=1`, refuse with
nothing changed.

**R12-2. Timeout advice keeps the caller's `--wipe` choice.** After a
`--set-active` or wipe timeout (`WipeFailed`), the advice first says to
check `fastboot getvar current-slot`. Only a flash run with `--wipe`
mentions erasing userdata; without it, the advice is to reboot the slot
that is active or go back to the previous one.

**Minors.** Opus m2 (the message said "in fastboot" for a phone found in
recovery) is covered by R12-1's message, which names where the phone was.
Opus m3 (single-item test loop) is fixed. Opus m4 (unauthorized adb with a
slotless entry halts through `Unreachable`) is unchanged and safe.

Regressions: `test_round12.py` (5 tests). At `c4064da` the not-booted case
wrote all four a partitions, the second `--no-reboot` flash exited 0, the
slotless pre-write halt named `flash --recover`, and the no-wipe timeout
advice named an erase and no `getvar current-slot`.


Gemini's round-12 minors: m1 (adb up is not a completed boot) and m2
(no-wipe timeout advice) are R12-1 and R12-2. m3 wording: the pre-baseline
`--no-reboot` refusal no longer names an armed slot a slotless entry
lacks; `armed_way_back` says to reboot to the bootloader before
`getvar current-slot`; `armed_want`'s hint mentions the pre-baseline
booted-phone rule.

## Round 13 follow-ups (minors)

- Opus m1: for an obligation armed with the `unstamped` sentinel before
  the first baseline, `armed_way_back` said "run flash stripped with slot X
  active", which R12-1 refuses while the phone is in fastboot. It now says
  to clear fb_mode by hand, boot slot X and wait for it to finish booting,
  then run `flash stripped`; if slot X doesn't reach Android, stop and tell
  Zak. Test: `test_round12.R13FollowUps`.
- Opus m2: the R12-1 refusal for a phone in Android that hasn't finished
  booting first says to wait and run the flash again.
- Opus m3: README wording matches the `--no-reboot` refusal message.

## Code review round 13: changes to R12

Round 13 (head `a1abb2a`): Opus and Gemini approve; Sol 7 objects with two
majors. The five `fbmode.settle` callers are unchanged.

**R13-1. One boot for the slot and the boot state.** The slot suffix and
`sys.boot_completed` came from two adb calls; a phone falling back from b
to a between them paired b's suffix with a's completed boot, and the flash
targeted a. `Device.slot_boot_state()` reads both in one checked adb shell
call (`echo "S=$(getprop ro.boot.slot_suffix)"; echo "C=$(getprop
sys.boot_completed)"`), so both values come from the same boot; a failed
read raises. The dev flash uses it for its pre-write slot read in Android.

**R13-2. A guarded way out for a recorded slot with unusable build IDs.**
An obligation armed with the `unstamped` sentinel records its slot but no
build IDs generic recovery can verify. Before the first baseline, R12-1
refuses the dev flash in fastboot, and `phone-qa recover` refused the
entry before clearing fb_mode, so nothing could boot the phone. Now
`fbmode.recover(boot_only_ok=True)`, used only by `phone-qa recover`,
clears fb_mode for such an entry, boots the recorded slot (after checking
it is the active one; another slot raises `WrongSlot` before the clear),
waits for boot completion, checks the phone runs that slot, and returns
**without settling**: the obligation stays pending until a dev flash's
verified boot settles it. Every other caller of `fbmode.recover` still
refuses such an entry. `armed_way_back` for this case says to run
`phone-qa recover`, then the same flash with the caller's `--wipe` kept.

**Minors.** README step 0 and the install guide state the booted-phone
prerequisite and how to get there from fastboot. The R12-1 message says
where the phone was found (recovery, unauthorized adb, fastboot). The
"no clear" test asserts the fake's actual `("fb_mode_clear",)` call. Opus's
round-13 minors (sentinel advice dead end, "wait for boot" advice, README
wording) are in the "Round 13 follow-ups" note above.

Regressions (`test_round12.R13FollowUps`): at `a1abb2a` the fallback race
wrote slot a; the sentinel advice named the refused flash; the not-booted
advice had no "wait".


Gemini's round-13 minors: m1, m4 and m5 were already fixed (R13-2, the
no-clear assertion, the README wording). m2: before the first baseline a
`ClearFailed` from the second clear gives the R10-3 way back instead of
"run the flash again". m3: the not-booted advice from Android says to
reboot to the bootloader before `fastboot --set-active`. Tests:
`test_round12.R13GeminiMinors`.

# river workspace: stripped LineageOS for the Moto G7

Start here. This directory ties together everything for zrm625's
stripped-down LineageOS 22.2 (Android 15) build for the Moto G7 (`river`): the
source trees, the build and flash scripts, and the benchmark results.

This file is `workspace/AGENTS.md` in
[zrm625/android_manifest](https://github.com/zrm625/android_manifest). To change
it, edit it there and open a pull request (see [Rules](#rules)).

## Layout

| Path | Points to | What it is |
| --- | --- | --- |
| `tree/` | `~/git/lineageos-river` | The source tree you work in (manifest branch `river-22.2`) |
| `baseline/` | `~/git/lineageos-river-baseline` | Frozen stock tree for comparison (locked branch `baseline-22.2`). Never edit it. |
| `scripts/` | `tree/.repo/manifests/scripts` | `change`, `build`, `flash`, `phone-qa`, `phone-setup`, `device-lock`, `bench/bench`, `bench/device-bench`, `merge-upstream`, `setup-forks` |
| `bench/` | `/mnt/storage/lineageos-bench` | Working build output (`out-stripped/`), benchmark output (`out-bench-*/`), `results/`, `logs/` |
| `phone` | Moto G7, USB serial `ZY225DPT7F` | The only test phone |

`tree/AGENTS.md` has the detailed rules for the source tree: which repos are
forks, branches, upstream merges, and how to strip things out.

## Build

Builds run in a pinned podman container. Only one build runs at a time, and
the scripts enforce this.

**Day to day, use `scripts/build`.** It is incremental: the output in
`bench/out-stripped/` is kept, so after a change only what depends on it is
rebuilt. That takes minutes, where a cold build takes a couple of hours and
uses every CPU core.

```sh
scripts/build                    # incremental build of tree/ into bench/out-stripped/
scripts/build --installclean     # after removing anything (see below)
scripts/build --clean            # start over (rarely needed)
scripts/build --tree baseline    # incremental build of baseline/ into bench/out-baseline/
```

**After removing a package, blob or feature, use `--installclean`.** A plain
incremental build can leave removed files in the staged partitions, and they
end up in the image (seen with removed IMS app libraries). `--installclean`
deletes the staged partitions and images but keeps compiled objects, so it
only adds a few minutes. Adding or changing things needs no install-clean.

**Do not run cold builds unless Zak asks for one.** Measure each change on the
phone with `scripts/bench/device-bench` instead (see "Build and run a change").
For the record, `scripts/bench/bench <stripped|baseline>` builds cold, with
ccache off, into a separate `bench/out-bench-<tree>/` and adds a row to
`bench/results/results.csv`. It takes a couple of hours of every CPU core.

The build produces, in `bench/out-stripped/target/product/river/`:

- `boot.img`, `dtbo.img`, `system.img` and `vendor.img`, which `scripts/flash` installs
- `lineage-22.2-*-UNOFFICIAL-river.zip`, a flashable zip for recovery sideload

## Install on the phone

Requirements:

- The Moto G7 is plugged in over USB and appears in `adb devices` as
  `ZY225DPT7F`, or is in fastboot mode. Before the QA harness has a
  known-good slot (no `$BENCH_HOME/slot-state.json`), it must be booted to
  Android with adb authorized: `flash` writes only after it has read that
  the active slot finished booting. From fastboot, `fastboot -s ZY225DPT7F
  reboot` and wait for Android first.
- The bootloader is unlocked. It is.
- Run all `adb` and `fastboot` commands with `-s ZY225DPT7F`. A Pixel is
  sometimes connected over wireless adb. Never touch it.

Install:

```sh
scripts/flash stripped --wipe     # first install, or after switching from another build
scripts/flash stripped            # update over a previous build of this tree
```

`scripts/flash` reboots the phone to the bootloader and checks that it is
`river`. It writes `boot`, `dtbo`, `system` and `vendor` to the **inactive**
A/B slot, switches to that slot, and waits for the phone to boot. The slot that
was running stays untouched. Once the QA harness has a known-good slot
(`$BENCH_HOME/slot-state.json`), `flash` always writes the other slot, never
the known-good one; `scripts/flash --status` shows which is which, and
`scripts/flash --rollback` returns to the known-good build (it wipes data).

`--wipe` erases all data on the phone. It is needed whenever the signing keys
change. Builds from this tree are signed with test keys, so moving from an
official LineageOS nightly to this build, or the other way, needs `--wipe`.
Without it the phone boot-loops.

**First install for the QA harness.** Before the first baseline, `phone-qa`
needs the phone to run a stamped build from this tree. So the first install
(step 0 in `.repo/manifests/qa/README.md`, "Calibration and bootstrap") is
`scripts/flash stripped --wipe` whenever the phone runs a build that is not
from this tree (an official nightly, or any build with other signing keys):
without the wipe it boot-loops. The stamp (`ro.river.build_id`) comes from
device PR #7 (zrm625/android_device_motorola_river, `9851f11`), which merges
together with harness PR #10. Until both are merged, build with branch
`qa-harness` checked out in `device/motorola/river` and in the manifest.

**Roll back** if the new build does not boot. Hold Power + Volume Down to get
to the bootloader, then:

```sh
fastboot -s ZY225DPT7F getvar current-slot     # the slot that fails
fastboot -s ZY225DPT7F --set-active=<other slot>
fastboot -s ZY225DPT7F reboot
```

## Use

- The build is `userdebug`: `adb -s ZY225DPT7F root` works, and
  `adb -s ZY225DPT7F shell` gives a root shell after that.
- Recovery is LineageOS recovery inside `boot.img`. To get to it, run
  `adb -s ZY225DPT7F reboot recovery`, or use Power + Volume Down and choose
  Recovery mode.
- The installed build: `adb -s ZY225DPT7F shell getprop ro.lineage.display.version`.
- The image has no Google apps. That is intentional for a stripped build.
- Apps: Files, Settings, Clock, the keyboard and the launcher. No calls, texts,
  mobile data, camera app, browser or gallery. Install apps with
  `adb -s ZY225DPT7F install <apk>` if you need one for a test.

## Measure on the phone

```sh
scripts/bench/device-bench stripped --flash --wipe   # flash, then measure 3 boots
scripts/bench/device-bench current --boots 1         # measure what is installed now
```

This records boot time, RAM use, process and package counts, partition use,
and idle CPU to `bench/results/device.csv`. Compare stripped against baseline by
running the same commands with `baseline`.

## Build and run a change

The whole loop, from an idea to merged. Steps 3 to 5 take about 10 minutes.

1. **Start a change.** One branch name across every repo:

   ```sh
   scripts/change start <name>          # e.g. drop-fm-radio
   ```

2. **Edit and commit** in `tree/`, in whichever repos the change needs (see
   "Strip the build down" in `tree/AGENTS.md`). Commit in each repo as usual.
   `scripts/change status` shows which repos have commits or uncommitted files.

3. **Build** (incremental, 3 to 8 minutes):

   ```sh
   scripts/build --installclean         # the change removes anything
   scripts/build                        # the change only adds or edits
   ```

   On failure, the last lines name the log in `bench/logs/`. Search it for
   `FAILED:` to find the step that broke.

4. **Install on the phone** (about 4 minutes, including the boot):

   ```sh
   scripts/flash stripped               # same signing keys as what the phone runs: keeps data
   scripts/flash stripped --wipe        # first install, or the phone runs a build with other keys
   ```

   Every build of this tree uses the same test keys, so updates keep the data.
   After `--wipe`, adb and `adb root` work straight away: debug builds turn on
   USB debugging and rooted debugging from first boot (set in
   `device/motorola/river`). The phone sits at the setup wizard with no Wi-Fi;
   that is fine for checks and benchmarks.

5. **Check the phone works:**

   ```sh
   scripts/phone-qa --quick             # add --wait right after a flash
   ```

   It takes a few seconds and checks the health group: boot finished, SELinux
   enforcing, no crashes since boot, no new tombstones, no services crashing
   or restarting in a loop, and no telephony. These gate: it exits 1 on any
   FAIL. It also prints INFO lines for the build in `bench/out-stripped`,
   Wi-Fi, Bluetooth, GPS, cameras, sensors, fingerprint and audio, which need
   a look but never fail the run. The full QA (`phone-qa --batch`) is for
   batches; see `tree/.repo/manifests/qa/README.md`. Then use the feature you
   changed on the phone itself.

   If the phone does not boot, see "Roll back" above: the previous build is
   still on the other slot.

6. **Measure** (about 12 minutes, the phone reboots 3 times):

   ```sh
   scripts/bench/device-bench stripped --boots 3
   ```

   Compare with the previous rows in `bench/results/device.csv`. For
   baselines, measure right after `scripts/flash stripped --wipe`, without
   touching the phone (setup wizard open, no Wi-Fi), so every baseline is
   measured the same way.

   **Then hand the phone back ready to use.** Never leave it sitting in the
   setup wizard, or with screens stacked on top of the wizard (Back then walks
   through every one). After measuring, and after any crash sweep of Settings
   screens, run:

   ```sh
   scripts/phone-setup --wifi SSID_FILE PSK_FILE
   ```

   It mutes touch sounds, connects Wi-Fi, clears stacked Settings screens, and
   walks the wizard to the home screen: location off, "Help improve
   LineageOS" stats unticked, gesture navigation, no screen lock. Then it
   turns on Extra dim and prints the result. The image already defaults to
   dark theme, gesture navigation, no lock screen, low brightness, location
   off and no click sounds, and it never sends LineageOS stats. Extra dim has
   no image default, so the script sets it.

7. **Open the pull requests and merge** them, one command each however many
   repos the change touched. Paste the `phone-qa --quick` and `device-bench`
   results into the description:

   ```sh
   scripts/change pr -t "<title>" -F notes.md
   scripts/change merge
   ```

8. **Now and then**, mark a milestone with `scripts/change tag baseline-<n>`.
   The `device-bench` run of the merged change is its measurement; no cold
   build is needed.

## Rules

- Pull requests go only to `zrm625` repos, base `river-22.2`. Never send one to
  LineageOS, TheMuppets, or AOSP. Details are in `tree/AGENTS.md`.
- Do not edit `baseline/`.
- Only one build at a time.
- adb and fastboot only ever target `-s ZY225DPT7F`.

## Set up this directory on a new machine

After the steps under "New checkout" in `tree/AGENTS.md`:

```sh
<tree>/.repo/manifests/scripts/setup-workspace ~/git/river
```

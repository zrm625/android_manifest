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
| `scripts/` | `tree/.repo/manifests/scripts` | `build`, `flash`, `bench/bench`, `bench/device-bench`, `merge-upstream`, `setup-forks` |
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

**For measurements, use `scripts/bench/bench`.** It builds cold, with ccache
off, into a separate `bench/out-bench-<tree>/`, so it never wipes your working
output.

```sh
scripts/bench/bench stripped     # cold build time of tree/
scripts/bench/bench baseline     # cold build time of baseline/, for comparison
```

Each benchmark adds a row to `bench/results/results.csv` (build time, peak
memory, output size, commits) and keeps the log and Soong metrics in
`bench/results/<run id>/`.

The build produces, in `bench/out-stripped/target/product/river/`:

- `boot.img`, `dtbo.img`, `system.img` and `vendor.img`, which `scripts/flash` installs
- `lineage-22.2-*-UNOFFICIAL-river.zip`, a flashable zip for recovery sideload

## Install on the phone

Requirements:

- The Moto G7 is plugged in over USB and appears in `adb devices` as
  `ZY225DPT7F`, or is in fastboot mode.
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
was running stays untouched.

`--wipe` erases all data on the phone. It is needed whenever the signing keys
change. Builds from this tree are signed with test keys, so moving from an
official LineageOS nightly to this build, or the other way, needs `--wipe`.
Without it the phone boot-loops.

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

## Measure on the phone

```sh
scripts/bench/device-bench stripped --flash --wipe   # flash, then measure 3 boots
scripts/bench/device-bench current --boots 1         # measure what is installed now
```

This records boot time, RAM use, process and package counts, partition use,
and idle CPU to `bench/results/device.csv`. Compare stripped against baseline by
running the same commands with `baseline`.

## Typical loop

1. `scripts/change start <name>`, then remove something in `tree/` (see
   "Strip the build down" in `tree/AGENTS.md`) and commit in each repo you touch.
2. `scripts/build --installclean` (incremental, minutes)
3. `scripts/flash stripped` (add `--wipe` if the build no longer boots over old data)
4. Check the phone works, and run `scripts/bench/device-bench current`.
5. `scripts/change pr -t "<title>" -b "<test results>"`, then
   `scripts/change merge` once you are happy. One command each, however many
   repos the change touched.
6. Now and then, run `scripts/bench/bench stripped` to measure cold build time.

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

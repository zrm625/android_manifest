# LineageOS 22.2 for Moto G7 (river): zrm625 fork

This tree is **zrm625's fork** of LineageOS 22.2 for the Moto G7 (`river`). The
`repo` manifest is [zrm625/android_manifest](https://github.com/zrm625/android_manifest).
The aim is a stripped-down build. Removing projects and features is expected.

Goals, the must-work list, and the batch pipeline rules (council approval,
queue, merge) are in [CHARTER.md](CHARTER.md). Read it before you change
anything.

The start-here guide for building, installing on the phone, and using the
build is `~/git/river/AGENTS.md` (source: `workspace/AGENTS.md` in this repo).
This file covers the source tree.

`AGENTS.md` and `CLAUDE.md` at the top of the tree link to
`.repo/manifests/AGENTS.md`. Edit that file and open a pull request against the
manifest repo.

## Where changes and pull requests go

All changes go to zrm625's GitHub repos. **Never** open a pull request, issue,
or Gerrit change against LineageOS, AOSP, or any other upstream, even if a
remote, a `gh` default, or the GitHub UI suggests it.

| Path in tree | Pull requests go to | Base branch |
| --- | --- | --- |
| `.repo/manifests` (this manifest) | `zrm625/android_manifest` | `river-22.2` |
| `build/make` | `zrm625/android_build` | `river-22.2` |
| `device/motorola/river` | `zrm625/android_device_motorola_river` | `river-22.2` |
| `device/motorola/sdm632-common` | `zrm625/android_device_motorola_sdm632-common` | `river-22.2` |
| `kernel/motorola/sdm632` | `zrm625/android_kernel_motorola_sdm632` | `river-22.2` |
| `hardware/motorola` | `zrm625/android_hardware_motorola` | `river-22.2` |
| `packages/inputmethods/LatinIME` | `zrm625/android_packages_inputmethods_LatinIME` | `river-22.2` |
| `vendor/lineage` | `zrm625/android_vendor_lineage` | `river-22.2` |
| `vendor/motorola/river` | `zrm625/proprietary_vendor_motorola_river` | `river-22.2` |
| `vendor/motorola/sdm632-common` | `zrm625/proprietary_vendor_motorola_sdm632-common` | `river-22.2` |

### Making a change: use `scripts/change`

One change often touches several repos (a device repo and its blob repo, for
example). Handle it as one unit with `.repo/manifests/scripts/change`, not by
hand:

```sh
S=.repo/manifests/scripts
$S/change start <name>                  # branch <name> from river-22.2 in every fork and the manifest
# ...edit and commit in whichever repos need it...
$S/change status                        # which repos have commits, uncommitted files, PRs
$S/change pr -t "<title>" -b "<why>"    # push, open/update one PR per repo, cross-link them all
$S/change merge                         # check all PRs are mergeable, merge them together, sync local
$S/change tag <tag>                     # optional: tag river-22.2 in every repo (e.g. baseline-2)
```

Re-run `change pr` after more commits; it pushes and refreshes the PR
descriptions. Write the test plan once in `-b` or `-F file`; every PR gets it,
plus its own commit list and links to the others. `change merge` merges
nothing unless every PR in the set can merge.

For a one-off PR by hand, always name the repo and base explicitly:

```sh
gh pr create --repo zrm625/<repo> --base river-22.2
```

- Do not run `repo upload`. It sends changes to LineageOS Gerrit.
- Every project not in the table tracks LineageOS or AOSP, read-only, pinned to
  a commit in `default.xml`. Do not commit in those projects. To change one,
  fork it first (see [Fork another project](#fork-another-project)).
- In each fork, the remote `zrm625` is the fork and the remote `lineage` is
  upstream. Pushing to `lineage` is disabled on purpose.

## Branches

- `river-22.2` is the long-lived branch in every fork and in the manifest.
- Do work on a topic branch made from `river-22.2` (`change start`), and
  merge it with pull requests (`change pr`, then `change merge`).
- Never rebase or force-push `river-22.2`. Upstream is merged in, not rebased,
  so published history stays stable.

## Pull in upstream changes

From anywhere in the tree:

```sh
.repo/manifests/scripts/merge-upstream            # merge into local river-22.2 only
.repo/manifests/scripts/merge-upstream --push     # merge and push to zrm625
.repo/manifests/scripts/merge-upstream kernel/motorola/sdm632   # one project
```

The script reads each fork's upstream from the `UPSTREAM_REPO` and
`UPSTREAM_BRANCH` annotations in `default.xml`. It skips projects with
uncommitted changes and reports conflicts without leaving a half-done merge.
Build and boot-test before you push a merge.

Projects that are not forked stay pinned to the commits in `default.xml`. To
move them forward, regenerate those pins in a manifest pull request.

## Strip the build down

- **Fewer projects in the tree.** Delete the `<project>` entries from
  `default.xml` in a manifest pull request. Other modules often still refer to
  a removed project, so run `m nothing` after `breakfast river` and before you
  commit. Remove projects in small batches.
- **Less in the image.** Remove packages and features in `vendor/lineage`
  or `device/motorola/*`.
- **Fewer proprietary blobs.** The blobs in `vendor/motorola/*` are closed-source
  binaries from Motorola's stock firmware (forked from TheMuppets); there is no
  source for them. To drop a blob, remove it from `proprietary-files.txt` in
  the matching `device/motorola/*` repo, then regenerate the vendor makefiles
  with that repo's `setup-makefiles.py` (run it as `./setup-makefiles.py`).
  Commit both repos on the same `change` branch.

## Benchmarks

The goal is to cut build time and image size against a frozen baseline.

- **Baseline tree:** `~/git/lineageos-river-baseline`, synced to the locked
  `baseline-22.2` manifest branch. Do not edit it or sync it to anything else.
- **Stripped tree:** this tree, on `river-22.2`.
- **Day-to-day build:** `.repo/manifests/scripts/build`. It is incremental,
  in a pinned podman image, into `/mnt/storage/lineageos-bench/out-stripped/`.
  Do not cold-build to try a change.
- **Build benchmark:** `.repo/manifests/scripts/bench/bench <baseline|stripped>`.
  It runs a cold `m bacon` into a separate `out-bench-<tree>/`. Results go to
  `/mnt/storage/lineageos-bench/results/results.csv`. Only one build of any
  kind runs at a time; the scripts enforce this. Run it only when Zak asks for
  a cold build; changes and milestones are measured with `device-bench`.
- **Device benchmark:** `.repo/manifests/scripts/bench/device-bench <baseline|stripped> --flash --wipe`.
  It flashes the build from that tree to the Moto G7 test phone (serial
  `ZY225DPT7F`), then records boot time, RAM, processes, packages, partition
  use, and idle CPU to `device.csv`.
- **Install a build:** `.repo/manifests/scripts/flash <baseline|stripped> [--wipe]`.
  It uses fastboot to flash the inactive A/B slot, so the running slot stays
  as a fallback.
- **Check a build on the phone:** `.repo/manifests/scripts/phone-check` after
  every flash. It must pass before you open pull requests. The full
  edit-build-flash-check loop is in `~/git/river/AGENTS.md` under "Build and run
  a change".
- The test phone is the USB Moto G7 only. Never run adb or fastboot commands
  against any other device, such as the Pixel on wireless adb. Always pass
  `-s ZY225DPT7F`.

## New checkout

```sh
repo init -u https://github.com/zrm625/android_manifest -b river-22.2 --git-lfs
repo sync -c -j8
.repo/manifests/scripts/setup-forks
```

`setup-forks` sets up the `lineage` remote, makes `gh` default to the zrm625
fork, checks out `river-22.2` in each fork, and links `AGENTS.md` and `CLAUDE.md`
at the top of the tree. Run it again after every new fork.

Build with `.repo/manifests/scripts/build`, which runs in the pinned build
container. A plain `source build/envsetup.sh && breakfast river && m bacon` on
the host also works, but it uses the host toolchain and a different `out/`.

## Fork another project

1. `gh repo fork <upstream owner>/<name> --clone=false --default-branch-only`
2. Create `river-22.2` at the commit pinned in `default.xml`:
   `gh api -X POST repos/zrm625/<name>/git/refs -f ref=refs/heads/river-22.2 -f sha=<pinned sha>`
3. Set the fork's default branch to `river-22.2`:
   `gh api -X PATCH repos/zrm625/<name> -f default_branch=river-22.2`
4. In `default.xml`, change the project entry to match the existing forks:
   `remote="zrm625"`, `name` without the owner prefix,
   `revision` and `dest-branch` set to `refs/heads/river-22.2`, `zrm625-fork`
   added to `groups`, and `UPSTREAM_REPO` and `UPSTREAM_BRANCH` annotations.
5. Add a row to the table above.
6. Run `repo sync <path>` and then `.repo/manifests/scripts/setup-forks`.
7. Copy `AGENTS.md` and the `CLAUDE.md` link from an existing fork into the new
   one, change the repo name, and commit them on `river-22.2`.

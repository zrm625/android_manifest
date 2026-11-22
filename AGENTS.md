# LineageOS 22.2 for Moto G7 (river): zrm625 fork

This tree is **zrm625's fork** of LineageOS 22.2 for the Moto G7 (`river`). The
`repo` manifest is [zrm625/android_manifest](https://github.com/zrm625/android_manifest).
The aim is a stripped-down build. Removing projects and features is expected.

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
| `device/motorola/river` | `zrm625/android_device_motorola_river` | `river-22.2` |
| `device/motorola/sdm632-common` | `zrm625/android_device_motorola_sdm632-common` | `river-22.2` |
| `kernel/motorola/sdm632` | `zrm625/android_kernel_motorola_sdm632` | `river-22.2` |
| `hardware/motorola` | `zrm625/android_hardware_motorola` | `river-22.2` |
| `vendor/lineage` | `zrm625/android_vendor_lineage` | `river-22.2` |
| `vendor/motorola/river` | `zrm625/proprietary_vendor_motorola_river` | `river-22.2` |
| `vendor/motorola/sdm632-common` | `zrm625/proprietary_vendor_motorola_sdm632-common` | `river-22.2` |

Always name the repo and base explicitly:

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
- Do work on a topic branch made from `river-22.2`, and merge it with a pull
  request.
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
  with that repo's `setup-makefiles.py`. Change both repos in matching pull
  requests.

## New checkout

```sh
repo init -u https://github.com/zrm625/android_manifest -b river-22.2 --git-lfs
repo sync -c -j8
.repo/manifests/scripts/setup-forks
```

`setup-forks` sets up the `lineage` remote, makes `gh` default to the zrm625
fork, checks out `river-22.2` in each fork, and links `AGENTS.md` and `CLAUDE.md`
at the top of the tree. Run it again after every new fork.

Build:

```sh
source build/envsetup.sh
breakfast river
m bacon
```

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

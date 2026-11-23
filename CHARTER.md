# River charter

This file sets out what the river build aims for, what has to keep working, and
how a change goes from an idea to a merge without Zak watching it. Zak decided
these rules on 2026-11-23. Change them only in a pull request that Zak approves.

## Goals

| Goal | Target | How it is measured |
| --- | --- | --- |
| RAM | At most 1,000 MB used (1,233 MB on 2026-11-23) | `device-bench` "used RAM", taken after a boot on a freshly wiped phone. See [Measuring RAM](#measuring-ram). |
| Must-work list | Every item passes | `phone-qa` (the QA harness) on every batch. |
| Autonomy | No hands | Build, flash, set up, QA, then merge or send back, with no one at the phone. |
| No regressions | Boot ≤ 55 s; RAM no worse; `/system` no bigger unless RAM went down | See [Regressions](#regressions). |
| Stay current | Monthly | Security patches and upstream LineageOS merges go through the same queue. Moving the pinned projects in `default.xml` is done by hand for now (see [Tooling changes](#tooling-changes)). |

### Measuring RAM

- Each batch measures RAM after one boot.
- Once a batch gets to 1,050 MB or less, measure three boots and use the median.
- A boot takes several minutes, so don't measure more often than that.

### Regressions

- **Boot:** 55 s or less, timed from the workstation (`host_boot_seconds` in
  `device.csv`) for the first `device-bench` boot. That is the reboot after the
  first boot following the wipe. The first boot itself runs setup work, takes
  minutes, and is only recorded.
- **RAM:** no worse than the reference. The reference is the lowest
  three-boot median of any known-good build. If one boot comes out more than
  10 MB over the reference, measure three boots. The batch fails if the median
  is more than 5 MB over the reference. This allows for measurement noise.
- **Size:** `/system` and `/vendor` may not grow compared with the latest
  known-good build, unless the same batch lowers the RAM median below the
  reference. This applies to upstream merges as well.

Low-RAM mode (`ro.config.low_ram`) is an experiment. Run it in its own batch
and record the result, but don't merge it. Zak decides whether it ships.

## Must-work list

Every batch must pass all of these. "Auto" means adb does the check by itself.
"Fixture" means a check that needs something on the desk. "Build check"
means QA inspects the build output, not the phone.

| Area | Feature | Check | Level |
| --- | --- | --- | --- |
| Radios | Wi-Fi | Connects after the wipe, loads a web page, and still works after the screen goes off and on. | auto |
| | Bluetooth | Turns on, scans, and pairs with the workstation (`forge`). | fixture (have it) |
| | NFC | The test phone is an XT1962-1 (US model) and has no NFC chip, so it can't turn NFC on. QA checks that the image still has the NFC stack: the HAL service, the NFC APEX, the Tag app, and the `sku_nfc` permission files. Reading a tag needs a phone that has NFC hardware. | build check |
| | GPS | The GNSS HAL is up and reports satellites. | auto |
| Hardware | Cameras | Exactly the cameras enabled in the build are present, and each one delivers live frames. The rear main camera takes a photo, with the torch on, that isn't black. See [Cameras](#cameras). | auto |
| | Speaker and mic | The speaker plays a tone and the mic picks it up. | auto |
| | Fingerprint | The fingerprint HAL is up. | auto |
| | Sensors | The accelerometer reads about 9.8 m/s² at rest. Light and proximity report values. | auto |
| | Vibration | The vibrator accepts a pattern. | auto |
| | Screen | Brightness and Extra dim change. Rotation works. | auto |
| | Battery | Level and charging state are reported. | auto |
| | USB | adb works, and the workstation sees the phone's storage in file-transfer mode (MTP). | auto |
| System | Boot | Boot finishes with SELinux enforcing, no crashes, and no services restarting in a loop. | auto |
| | Home and gestures | Perch is home, swiping up goes home, and Back works. | auto |
| | Alarms | An alarm set for one minute ahead goes off on time. | auto |
| | Keyboard | Text typed into a text field arrives. | auto |
| | Sideloading | An APK installs, opens, and uninstalls. | auto |
| | WebView | An app that shows a web page loads it. | auto |
| | Video | A sample video plays with the hardware decoder. | auto |
| | Time | The clock syncs over Wi-Fi. | auto |
| Apps | Brave | Installs, opens, and shows a web page. | auto |
| | Tailscale | Installs and opens. Signing in needs Zak, so QA doesn't. | auto |
| | T3 Code | Installs and opens. | auto |
| | Perch | Home screen (see Home and gestures). | auto |

QA installs the sideloaded apps from pinned APKs in
`$BENCH_HOME/qa/fixtures/`: Brave from its GitHub releases (arm64), Tailscale
from pkgs.tailscale.com, and T3 Code from its nightly Android builds. Update
an app by replacing its APK and its pinned hash in one change. If an app's APK
can no longer be found, drop the app from this list instead of failing
batches.

Not on the list, and not checked:

- The headphone jack.
- Deep sleep. The phone can't deep-sleep while it is plugged into USB: the USB
  controller and the charger keep it awake. Check it by hand now and then.

### Cameras

The river has three cameras: the rear main, the rear depth, and the front.

- Each camera gets its own build-time switch. When a camera is switched off,
  the build leaves it out, so to Android it doesn't exist. It acts like a
  hardware switch.
- QA expects exactly the cameras that the build switches on, and tests only
  those.
- Only the rear main camera has a flash, and the phone may lie in a dark room.
  So QA lights the rear main with its torch and requires a photo that isn't
  black. The front and depth cameras must deliver frames that change from
  frame to frame at maximum exposure, which shows the sensor and pipeline
  work. A black photo from those two is noted in the report but doesn't fail
  the batch.
- A change that changes which cameras are switched on never merges
  automatically. Zak merges it by hand. Then a new baseline is run, which
  records the new camera set and keeps the lowest known-good RAM reference.
  The new baseline changes the QA harness version, so the harness is
  qualified again before the next batch.

## Pipeline

Building and testing are the bottleneck, so changes are tested in batches.

1. **Council.** Every change goes to a council: three reviewers from three
   different AI providers. All three must approve it. If any one objects, the
   change goes back to be fixed.
2. **Queue.** A change can join the queue only after the council approves it.
   The queue is a local file on the workstation, and the tester owns it.
3. **Batch.** The tester builds the queued changes together. Then it wipes and
   flashes the phone, sets it up, runs the full QA, and measures it. Wiping the
   phone for every batch is fine.
4. **Clean batch.** If everything passes, the tester merges every change in the
   batch and tags the known-good state. Zak doesn't have to approve anything
   else. The tester merges through one batch pull request per repo, holding
   exactly the commits that were tested, and closes the changes' own pull
   requests as merged.
5. **Failed batch.**
   - A batch of several changes is split: each change is tested again on its
     own, in its own batch.
   - When a change fails on its own, the tester reads the evidence and finds
     out why. If the change caused it, the tester marks it as tried and
     records how it failed; it is queued again once it is fixed and the
     council has approved the fix. If the phone, the desk or the workstation
     caused it, the change goes back into the queue. After a change's third
     failure, only Zak can put it back in the queue.
   - If several different changes fail the same way in a row, the tester
     stops and reports a likely problem with the desk or the workstation.

### Tooling changes

The QA harness and the batch tools can't be tested by the batch they run.
Changes to them (`qa/`, `scripts/`, `workspace/` and other tooling in the
manifest repo, never `default.xml`) go through the council as a code review.
After all three approve, they are merged by hand with `change merge`, and the
harness or batch tools are qualified again before the next batch.

For now, moving the pinned upstream projects in `default.xml` is also done by
hand, with a council review and a new baseline, until the batch tools can test
manifest changes.

### What still needs Zak

These need Zak's approval even when the council has approved the change:

- Removing anything on the must-work list.
- Forking `frameworks/base` or another upstream project.
- Spending money.
- Touching any device other than the test phone `ZY225DPT7F`.
- Shipping low-RAM mode.
- Changing this charter.

## Running without anyone at the phone

- **Recovering from a bad flash.** `flash` writes the inactive A/B slot, so the
  previous system stays there as a fallback. If a build doesn't boot, the
  tester switches back to the old slot over fastboot, and marks the batch as
  failed.
- **The phone stays on USB.** If the phone hangs and stops answering on USB,
  someone has to hold the power button. When that happens the tester stops and
  tells Zak.
- **Known-good tags.** Every clean batch gets a tag, so there is always a
  recent build that works.
- **Reports.** After every batch Zak gets a short summary, as an HTML page. It
  also lists anything that is waiting on Zak.

## Order of work

1. The QA harness, `phone-qa`. This is the first task to go through the council.
2. The queue and batch tools: batch branches in the main tree (a second
   checkout would need a cold build), the batch runner, and slot rescue.
3. The tester thread.
4. Perch through the council, then the first real batch.
5. Per-camera build switches.
6. RAM work toward 1 GB, then the backlog for removing compatibility code.

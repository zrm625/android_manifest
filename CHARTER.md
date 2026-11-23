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
| No regressions | Boot ≤ 50 s; RAM and `/system` size no worse | Compared with the last known-good tag on every batch. |
| Stay current | Monthly | Security patches and upstream LineageOS merges go through the same queue. |

### Measuring RAM

- Each batch measures RAM after one boot.
- Once a batch gets to 1,050 MB or less, measure three boots and use the median.
- A boot takes several minutes, so don't measure more often than that.

Low-RAM mode (`ro.config.low_ram`) is an experiment. Run it in its own batch
and record the result, but don't merge it. Zak decides whether it ships.

## Must-work list

Every batch must pass all of these. "Auto" means adb does the check by itself.
"Fixture" means a check that needs something on the desk.

| Area | Feature | Check | Level |
| --- | --- | --- | --- |
| Radios | Wi-Fi | Connects after the wipe, loads a web page, and still works after the screen goes off and on. | auto |
| | Bluetooth | Turns on, scans, and pairs with the workstation (`forge`). | fixture (have it) |
| | NFC | NFC turns on and the NFC HAL answers. Reading a tag needs an NFC sticker on the phone's back, and we don't have one yet. Until then the tag read is a WARN. | auto; tag read is a fixture |
| | GPS | The GNSS HAL is up and reports satellites. | auto |
| Hardware | Cameras | Each camera that is enabled in the build opens and takes a photo that isn't black. See [Cameras](#cameras). | auto |
| | Speaker and mic | The speaker plays a tone and the mic picks it up. | auto |
| | Fingerprint | The fingerprint HAL is up. | auto |
| | Sensors | The accelerometer reads about 9.8 m/s² at rest. Light and proximity report values. | auto |
| | Vibration | The vibrator accepts a pattern. | auto |
| | Screen | Brightness and Extra dim change. Rotation works. | auto |
| | Battery | Level and charging state are reported. | auto |
| | USB | adb works, and the workstation sees the phone's storage in file-transfer mode (MTP). | auto |
| System | Boot | Boot finishes with SELinux enforcing, no crashes, and no services restarting in a loop. | auto |
| | Sleep | With the screen off for 10 minutes the phone goes into deep sleep, then wakes up and responds. | auto |
| | Home and gestures | Perch is home, swiping up goes home, and Back works. | auto |
| | Alarms | An alarm set for one minute ahead goes off on time. | auto |
| | Keyboard | Text typed into a text field arrives. | auto |
| | Sideloading | An APK installs, opens, and uninstalls. | auto |
| | WebView | An app that shows a web page loads it. | auto |
| | Video | A sample video plays with the hardware decoder. | auto |
| | Time | The clock syncs over Wi-Fi. | auto |
| Apps | Brave Origin | Sideloaded; opens a web page. | auto |
| | Perch | Home screen (see Home and gestures). | auto |

The headphone jack is not on the list. Nothing checks it.

### Cameras

The river has three cameras: the rear main, the rear depth, and the front.

- Each camera gets its own build-time switch. When a camera is switched off,
  the build leaves it out, so to Android it doesn't exist. It acts like a
  hardware switch.
- QA expects exactly the cameras that the build switches on, and tests only
  those.

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
   else.
5. **Failed batch.**
   - Find out which change caused the failure and why.
   - Mark that change as tried, and record how it failed.
   - Queue it again once it is fixed.
   - The other changes in the batch go into the next batch and are tested the
     same way.

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
2. The queue and batch tools: worker checkouts, `change batch`, and slot rescue.
3. The tester thread.
4. Perch through the council, then the first real batch.
5. Per-camera build switches.
6. RAM work toward 1 GB, then the backlog for removing compatibility code.

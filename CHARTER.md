# River charter

This file sets out what the river build aims for, what has to keep working, and
how a change goes from an idea to a merge without Zak watching it. Zak decided
these rules on 2026-11-23. Change them only in a pull request that Zak approves.

## Goals

| Goal | Target | How it is measured |
| --- | --- | --- |
| RAM | 1,044.4 MB settled accepted by Zak on 2026-11-26 | Use the settled reference to check for regressions. See [Measuring RAM](#measuring-ram). |
| Must-work list | Every item passes | `phone-qa --quick` on every try, the full `phone-qa` daily. |
| Autonomy | No hands | Build, flash, set up, QA, then keep or drop, with no one at the phone. |
| No regressions | Boot ≤ 55 s; RAM no worse; `/system` no bigger unless RAM went down | See [Regressions](#regressions). |
| Stay current | Monthly | Security patches and upstream LineageOS merges go through the same queue. Moving the pinned projects in `default.xml` is a try like any other. |

### Measuring RAM

Zak chose the after-setup RAM gate on 2026-11-25. The first reference
reading was 1,047.8 MB on the NFC-enabled Stack I rebuild.
On 2026-11-26, Zak accepted 1,044.4 MB on the kept system server build as
good enough and closed the 1,000 MB goal. RAM measurements now check
for regressions against the settled reference.

- After `phone-setup`, leave Perch in the foreground and the screen on.
  Take the first Used RAM sample after five minutes. Take two more settled
  samples on the same boot and use the median. If the phone cannot provide
  all three samples, record what was collected and leave the gate unverified.
- Continue to record the median Used RAM across three benchmark boots
  before setup.

### Regressions

- **Boot:** 55 s or less, timed from the workstation (`host_boot_seconds` in
  `device.csv`) for the first `device-bench` boot. That is the reboot after the
  first boot following the wipe. The first boot itself runs setup work, takes
  minutes, and is only recorded.
- **RAM:** use the median of the three settled samples. The reference is the
  lowest verified settled median from a kept, NFC-enabled build. The
  initial Stack I reading is the reference until a kept run has a verified
  median. The try fails if its median is more than 5 MB over the reference.
- **Size:** `/system` and `/vendor` may not grow compared with the latest
  known-good build, unless the same try lowers the RAM median below the
  reference. This applies to upstream merges as well.

Low-RAM mode (`ro.config.low_ram`) is an experiment. Run it as its own try
and record the result, but don't merge it. Zak decides whether it ships.

## Must-work list

Every try must pass all of these that `phone-qa --quick` checks, and the
daily full run checks all of them. "Auto" means adb does the check by itself.
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
tries.

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
  the try.
- A change that changes which cameras are switched on is never kept
  automatically. Zak merges it by hand.

## Pipeline: tries

Zak changed the pipeline on 2026-11-23: the build is the bottleneck, so work
is a list of things to try, paced by compile time. The council, the batch
runner and the harness qualification (calibrations, controls, `batch
qualify`) are no longer part of it.

1. **List.** Every idea is a row in [TRIES.md](TRIES.md): what it changes,
   why, and later its result. The tester owns the list and picks the order,
   cheapest and most promising first.
2. **Try.** A try is a topic branch from `river-22.2` (`change start`), with
   PRs to the zrm625 repos. Several small tries can share one build.
3. **Build while testing.** The next try builds (`scripts/build`, incremental)
   while the phone tests the last one, so the phone never waits for a review.
4. **Test.** Flash the try's images with a wipe to the slot that isn't
   known-good and run `device-bench current` before setup. Run `phone-setup`,
   record settled RAM, and run `phone-qa --quick`.
5. **Keep or drop.**
   - **Keep** when QA passes, boot is 55 s or less, and the settled RAM
     median passes [Regressions](#regressions). The tester merges the try's PRs into
     `river-22.2` and writes the result in TRIES.md.
   - **Drop** when any of that fails. The tester writes why in TRIES.md and
     closes the PRs. A try comes back only with a fix for the reason it failed.
6. **Full check.** Once a day, the merged `river-22.2` gets a full
   `phone-qa` run. If something on the must-work list broke, the tester
   finds the try that broke it among that day's kept tries and reverts it.
7. **Second opinion.** A change that touches signing keys, permissions,
   SELinux or hidden-API exemptions gets one reviewer from another AI provider
   before it is kept. Nothing else waits for a review.

The phone goes back to known-good (`flash --rollback`, then `phone-setup`)
whenever the tester stops.

### What still needs Zak

These need Zak's approval:

- Removing anything on the must-work list.
- Forking `frameworks/base` or another upstream project.
- Spending money.
- Touching any device other than the test phone `ZY225DPT7F`.
- Shipping low-RAM mode.
- Changing this charter.

## Running without anyone at the phone

- **Recovering from a bad flash.** `flash` writes the inactive A/B slot, so the
  previous system stays there as a fallback. If a build doesn't boot, the
  tester switches back to the old slot over fastboot and drops the try.
- **The phone stays on USB.** If the phone hangs and stops answering on USB,
  someone has to hold the power button. When that happens the tester stops and
  tells Zak.
- **Reports.** TRIES.md is the record. Zak gets a short summary of kept and
  dropped tries, and anything waiting on Zak, at the end of each session.

## Order of work

1. Perch and the GPS fix, the first tries.
2. Check for RAM regressions against the settled reference.
3. Better UI.
4. Per-camera build switches.
5. The backlog for removing compatibility code.

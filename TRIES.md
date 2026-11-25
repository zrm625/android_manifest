# Tries

Things to try on the river build. Small tries are stacked into one image;
a stack that passes is kept whole, a stack that fails is split.
How a try runs and when it is kept: [CHARTER.md](CHARTER.md#pipeline-tries).

RAM reference: baseline-2, 1,266 MB (median of 3 boots, 2026-11-23).
Target: 1,000 MB. Now: 1,053 MB (stack H, kept).

## Kept

| Try | PRs | Result |
| --- | --- | --- |
| Perch replaces Trebuchet | river #6, vendor_lineage #5 | Alone (with the first GPS fix): 1,256 MB, −10 MB. Home, swipe-up and Back pass. |
| 64-bit zygote only (`ZYGOTE_FORCE_64`) | river #8 | Alone: 1,211 MB, −55 MB (native PSS −82 MB). |
| Lazy camera HAL (`ro.camera.enableLazyHal`) | sdm632-common #4 | The camera HAL (42 MB) runs only while a camera is open. All three cameras pass. |
| Java heap headroom | sdm632-common #5 | heapmaxfree 16m → 8m, target 0.6 → 0.75 for every Java process. |
| IPC logging off | kernel #1 | 62 Qualcomm debug-log contexts gone; MemTotal +4 MB. |
| f2fs bitmaps in one block | kernel #2 | 105k 64-byte kmallocs (rounded to 128 on arm64) became two blocks; SUnreclaim 127 → 115 MB. |
| Two SurfaceFlinger framebuffers | sdm632-common #6 | `max_frame_buffer_acquired_buffers` 3 → 2; SurfaceFlinger 87 → 77 MB. |
| No ClearKey DRM service | sdm632-common #7 | Test DRM scheme; Widevine stays. |
| Tiny default wallpaper | vendor_lineage #6 | 32×32 dark image; the wallpaper surface shrank and SurfaceFlinger went 77 → 63 MB. |
| No CAS HIDL service, traced off | build #3 | Broadcast-TV CAS service and Perfetto daemons not resident. `setprop persist.traced.enable 1` for a trace. |

**Stack A** (Perch, zygote64, lazy camera): 1,144 MB (−122 MB), boot 49.7 s.
`phone-qa --probe`: 34 pass. Failed: GPS (open, below), Brave and Tailscale
(harness, fixed in manifest #21 and #22), camera-HAL process watch (fixed in
manifest #19).

**Stack B** (stack A + heap headroom + IPC logging off, tested together with
the GPS change and slab statistics): 1,141 MB (−3 MB), boot 50.2 s.
`phone-qa --probe`: 39 pass. Failed: GPS (as before) and the rear-camera
flash check, which passed on two reruns (flaky in the dark; it failed the same
way once before). Kept: heap headroom and IPC logging off. Not merged: the GPS
change (it did not fix GPS) and slab statistics (diagnostic only).

**Stack C** (f2fs bitmaps, two framebuffers, no ClearKey, no CAS HIDL, traced
off; branched before the stack B merges, so without them): 1,122 MB (−19 MB
against stack B), boot 51.8 s. `phone-qa --probe`: 39 pass. Failed: GPS (as
before) and the rear-flash check, 4 of 4 times. The rear camera reads luma 1.4
with the flash off, so its lens is against the desk; the camera daemon turned
the flash on and the LED driver was set. No stack C change touches the camera.
Zak: please check what the rear of the phone rests on.

**Stack D** (river-22.2 with stacks A to C merged, plus the tiny wallpaper):
1,104 MB (−18 MB), boot 49.7 s. This is the first image with stacks B and C
together. `phone-qa --probe`: 39 pass; GPS and the rear-flash check fail as
before.

**Stack F** (no startup file pinning, no SecureElement): 1,059.2 MB, down
44.3 MB from stack D, and a 51.73 s benchmark boot. Kept in sdm632-common #8
and build #4. Pinner reported 0 pinned bytes and QA found no tombstones or
LiveDisplay restart loop. The NFC APEX, Tag APK, and vendor NFC files were
present. Quick QA passed. The full probe and targeted reruns passed Wi-Fi,
GPS, audio, and the core apps. The rear flash reproduced the known dark
frame. Settings launched cold in 838 ms median across three runs. There is
no matching stack D launch measurement.

**Stack H** (MIDI and Device Diagnostics removed on Stack F): kept in
sdm632-common #9 and build #5. The three boots used 1,057.6, 1,042.2, and
1,053.3 MB. The median was 1,053.3 MB, down 5.9 MB from Stack F. The first
benchmark boot took 51.43 s. System used 7,720 KiB less. Quick QA passed and
the probe passed 39 checks. The known GPS and rear-flash failures reproduced.
Default NFC files and SKU rules were present on the phone. The audio probe
measured 63.5 to 65.0 dB above silence. The isolated images are being measured
against Stack F for comparison.

## Dropped

| Try | Branch | Why dropped |
| --- | --- | --- |
| No debugfs | kernel `debugfs-off` | Doesn't compile: Qualcomm audio (q6asm, apr, audio codecs) and clock-debug code keep real functions under `CONFIG_DEBUG_FS`. Worth about 6 MB; it would need driver patches. |
| No ftrace | kernel `ftrace-off` | Stack E (with no SecureElement): 1,097 MB (−6.5 MB, not the 16 MB the tracefs file count suggested). `vendor.lineage.livedisplay-service.sdm` aborted at every boot ("PictureAdjustment backend not ready"), then init restarted it 5 s later. A tombstone on every boot isn't worth 6 MB. |

**Stack G** (virtual-device manager and MIDI service disabled on Stack F):
dropped. Used RAM was 908.7 MB median (933.8, 908.7, and 871.9 MB). The first
benchmark boot took 56.26 s. The next two took 47.37 and 124.3 s. SystemUI
repeatedly crashed during startup because a required Dagger provider returned
null. SystemUI requires the virtual-device manager. Setup could not advance
past Start, so the probe could not run. Do not use the RAM result as a
reference. The MIDI-only image is staged separately.

## Testing

**MIDI-only comparison on Stack F:** 1,041.7 MB median (1,058.6, 1,041.7,
and 1,032.0 MB) with a 51.29 s first benchmark boot. Quick QA passed. The
probe passed 40 checks, including GPS, and reproduced the known rear-flash
failure. Speaker and microphone measured 65.2 to 68.2 dB above silence.
MIDI is already in Stack H. The Device Diagnostics-only run is pending, so
the difference between these medians does not isolate its effect.

**Next:** run the staged Device Diagnostics-only image. Profile the kept
Stack F reference before and after setup to rank the next set of changes.

**Low-RAM mode** (experiment on Stack D, branch `try-low-ram`): 1,076.2 MB
median across three boots (1,076.2, 1,081.6, and 1,067.8 MB), with a 50.20 s
first benchmark boot. The median is 27.3 MB below Stack D. Shipping this mode
requires Zak's approval. The probe passed 40 checks, including Wi-Fi, GPS,
audio, and the core apps. The rear-flash check reproduced the dark frame.

## Open problems

- **GPS** (must-work). `radios.gps` gets 0 status callbacks in 120 s with
  stack B, with assisted GPS on or off, and with xtra-daemon not running. So
  xtra-daemon is not the cause after all; the earlier manual runs that saw
  satellites (62–106 s) were not reproduced. The QTI HAL answers the start
  request with `onResponseCb ERROR: 5` (ID unknown). Indoor reception at the
  desk is weak. Next: compare the official build with the same check at the
  same spot, then try by a window. Branch `gnss-standalone` (sdm632-common #3)
  stays open.

## Ideas

| Try | Why |
| --- | --- |
| Rest of `kmalloc-128` | kernfs names 21k objects (2.6 MB), ASoC DAPM paths 17.6k (2.2 MB). arm64 has no kmalloc cache below 128 bytes. |
| 32-bit media processes | mediaserver (23 MB) and the audio HAL are 32-bit; with no 32-bit zygote their libraries are private (.so 20 MB, 16 MB private clean). mediaserver's bitness is set in frameworks/av: a fork needs Zak. |
| media.swcodec (22 MB) | Loads every software codec at start (.so 19 MB, 13 MB private). Trimming the APEX needs a frameworks/av fork. |
| Kernel threads | 597 kernel threads: 97 bioset, 35 loop, 21 kverityd and 21 kdmflush (APEX loop mounts), 42 ext4 rsv-conver. Fewer preallocated loop devices. |
| Process count | 60k of 100k `vm_area_struct` are copies of zygote's mappings in forked apps; each app that doesn't start saves its VMAs as well as its PSS. |
| system_server (262 MB) and SystemUI (103 MB) | system_server is mostly file mappings (.jar 53 MB, .apk 40 MB, .dex 40 MB, .so 27 MB); heaps are 22 MB each. Fewer services means fewer classes loaded. |
| Low-RAM mode (`ro.config.low_ram`) | Measure only; shipping it needs Zak. |
| Remove x86 and x86_64 WebView projects | Manifest #30 removed both for new syncs. `m nothing` passed with both project directories absent. |
| Perch follow-ups | Go-edition launcher overrides, `PRODUCT_DEXPREOPT_SPEED_APPS`, GestureService only in gesture nav. |
| Per-camera build switches | Charter item; changing the camera set needs Zak to merge. |

## Harness notes

- Manifest #32 reads the test slot's bootloader status before writing and
  verifies it after activation. On Stack H, rewriting the exhausted slot and
  activating it took its reported retries from 0 to 7. Current kernels
  disable SLUB debugging, so the trial saves `slab-sysfs.tsv` when
  `/proc/slabinfo` is absent.

- Stack F received `192.168.4.61` after a wipe, outside the documented
  Wi-Fi QA firewall rule. Manifest PR #28 makes `phone-setup` select the
  factory MAC for the test SSID, but the phone still received `.61` after
  the low-RAM wipe, and again after the Stack H trials. Zak chose to move
  the reservation, the QA fixture, the docs and the firewall rule to `.61`
  instead of restarting the eero network.

- `hardware.cameras`: the rear flash check (`on >= 20`) fails now and then in
  a dark room. Consider a relative threshold.
- `phone-setup`: the pocket check blocked waking the screen (fixed, #18).

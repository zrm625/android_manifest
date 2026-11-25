# Tries

Things to try on the river build. Small tries are stacked into one image;
a stack that passes is kept whole, a stack that fails is split.
How a try runs and when it is kept: [CHARTER.md](CHARTER.md#pipeline-tries).

The target is 1,000 MB Used RAM after setup. The first settled reference is
1,047.8 MB from the default Stack I rebuild. Its pre-setup median was
989.8 MB. The old baseline-2 pre-setup median was 1,266 MB.

## Kept

| Try | PRs | Result |
| --- | --- | --- |
| Perch replaces Trebuchet | river #6, vendor_lineage #5 | Alone (with the first GPS fix): 1,256 MB, −10 MB. Home, swipe-up and Back pass. |
| 64-bit zygote only (`ZYGOTE_FORCE_64`) | river #8 | Alone: 1,211 MB, −55 MB (native PSS −82 MB). |
| Lazy camera HAL (`ro.camera.enableLazyHal`) | sdm632-common #4 | The camera HAL (42 MB) runs only while a camera is open. All three cameras pass. |
| Java heap headroom | sdm632-common #5 and #10 | Vendor init overwrote #5's product properties on the test phone. Stack I's #10 made the effective settings 0.75 target utilization, 512k minimum free, and 8m maximum free. |
| IPC logging off | kernel #1 | 62 Qualcomm debug-log contexts gone; MemTotal +4 MB. |
| f2fs bitmaps in one block | kernel #2 | SUnreclaim fell from 127 to 115 MB. About half the allocation moved to vmalloc, for an estimated net saving of 6–7 MB. |
| Two SurfaceFlinger framebuffers | sdm632-common #6 | `max_frame_buffer_acquired_buffers` 3 → 2; SurfaceFlinger 87 → 77 MB. |
| No ClearKey DRM service | sdm632-common #7 | Test DRM scheme; Widevine stays. |
| Tiny default wallpaper | vendor_lineage #6 | 32×32 dark image; the wallpaper surface shrank and SurfaceFlinger went 77 → 63 MB. |
| No CAS HIDL service, traced off | build #3 | The HIDL service and default Perfetto daemons are gone. The AIDL CAS example service still runs at about 4.8 MB PSS. Use `setprop persist.traced.enable 1` for a trace. |

**Stack A** (Perch, zygote64, lazy camera): 1,144 MB (−122 MB), boot 49.7 s.
`phone-qa --probe`: 34 pass. Failed: GPS (open, below), Brave and Tailscale
(harness, fixed in manifest #21 and #22), camera-HAL process watch (fixed in
manifest #19).

**Stack B** (stack A + heap headroom + IPC logging off, tested together with
the GPS change and slab statistics): 1,141 MB (−3 MB), boot 50.2 s.
`phone-qa --probe`: 39 pass. Failed: GPS (as before) and the rear-camera
flash check, which passed on two reruns (flaky in the dark; it failed the same
way once before). IPC logging was disabled. The heap properties took
effect in Stack I. The GPS change and diagnostic slab statistics stayed
on their trial branches.

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
measured 63.5 to 65.0 dB above silence. The isolated comparisons on Stack F
are below.

**Stack I.** It defaults Bluetooth to off, selects the 64-bit mediaserver,
sets the HWUI app cache policy to `extremelowram`, skips zygote GL preload,
sets zram to 1 GiB, disables LOWI, and applies ART heap settings in vendor
init. Kept in [river #9](https://github.com/zrm625/android_device_motorola_river/pull/9)
and [sdm632-common #10](https://github.com/zrm625/android_device_motorola_sdm632-common/pull/10).
The median was 993.1 MB before setup, down 60.2 MB from Stack H. Individual
boots used 1,002.5, 993.1, and 978.8 MB. The first benchmark boot took 51.87 s.
The settled sample used 1,045.9 MB after five minutes on Perch.
The phone ran mediaserver64 and used 1 GiB zram. It started with Bluetooth
off and paired successfully when QA enabled it. Quick QA passed. The probe
passed 40 checks, including Wi-Fi at 192.168.4.61, GPS without LOWI, and
hardware H.264 playback. The known dark rear-flash check failed. The audio
bursts measured 65.1–77.5 dB above silence at −42.5 to −35.0 dBFS. The
default NFC APEX, Tag APK, HAL, vendor configs, and SKU files were present.

**Opt-in NFC-off build.** The default includes NFC. For a stripped
development build without NFC hardware support, run
`scripts/build --without-nfc`. The opt-in image retains the standalone NFC
framework jar that Android needs at startup. Run the build again without
the flag to restore the default packages in the shared build output.
Kept in [build #6](https://github.com/zrm625/android_build/pull/6),
[river #10](https://github.com/zrm625/android_device_motorola_river/pull/10),
and [manifest #39](https://github.com/zrm625/android_manifest/pull/39).

| Build | Median RAM before setup | Settled RAM | First benchmark boot |
| --- | ---: | ---: | ---: |
| NFC off | 986.8 MB | 1,050.5 MB | 49.70 s |
| Default restored after NFC-off build | 989.8 MB | 1,047.8 MB | 50.09 s |

Both variants passed quick QA. Each probe passed 39 checks and reproduced
the known intermittent GPS and dark rear-flash failures. The default
passed the installed-file check. The phone had the mounted NFC APEX, Tag
app, HAL, vendor configs, SKU files, and init rules. These checks cover
build contents on the XT1962-1. Reading a tag requires an NFC-equipped SKU.
The 1,047.8 MB default settled sample is the first after-setup reference.

## Dropped

| Try | Branch | Why dropped |
| --- | --- | --- |
| No debugfs | kernel `debugfs-off` | Doesn't compile: Qualcomm audio (q6asm, apr, audio codecs) and clock-debug code keep real functions under `CONFIG_DEBUG_FS`. Worth about 6 MB; it would need driver patches. |
| No ftrace | kernel `ftrace-off` | Stack E (with no SecureElement): 1,097 MB (−6.5 MB, not the 16 MB the tracefs file count suggested). `vendor.lineage.livedisplay-service.sdm` aborted at every boot ("PictureAdjustment backend not ready"), then init restarted it 5 s later. A tombstone on every boot isn't worth 6 MB. |
| Standalone GNSS | sdm632-common `gnss-standalone` | GPS status callbacks still timed out after the change. Later probes reported satellites without it. [PR #3](https://github.com/zrm625/android_device_motorola_sdm632-common/pull/3) is closed and the branch is retained. |

**Stack G** (virtual-device manager and MIDI service disabled on Stack F):
dropped. Used RAM was 908.7 MB median (933.8, 908.7, and 871.9 MB). The first
benchmark boot took 56.26 s. The next two took 47.37 and 124.3 s. SystemUI
repeatedly crashed during startup because a required Dagger provider returned
null. SystemUI requires the virtual-device manager. Setup could not advance
past Start, so the probe could not run. Do not use the RAM result as a
reference. The MIDI-only image is staged separately.

## Testing

**Default NFC repeat under the settled RAM gate (2026-11-25).** The NFC-enabled
image `try-default-nfc-restored` matched the current build inputs. Its first
benchmark reboot took 51.33 s. Used RAM before setup was 1,004.9, 990.1, and
978.8 MB across three boots, with a median of 990.1 MB. With Perch in the
foreground and the display awake, the readings on one boot were 1,050.1,
1,053.5, and 1,055.9 MB. The 1,053.5 MB median was 5.7 MB above the
1,047.8 MB reference, so the repeat missed the gate by 0.7 MB. The
reference remains 1,047.8 MB. Quick QA passed all eight checks, including
the NFC file check. The probe passed 40 checks, including GPS and hardware
H.264. The known dark rear-flash check failed. Raw evidence is in
`tries/default-nfc-settled-repeat/` under the benchmark directory.

**MIDI-only comparison on Stack F:** 1,041.7 MB median (1,058.6, 1,041.7,
and 1,032.0 MB) with a 51.29 s first benchmark boot. Quick QA passed. The
probe passed 40 checks, including GPS, and reproduced the known rear-flash
failure. Speaker and microphone measured 65.2 to 68.2 dB above silence.
MIDI is already in Stack H. See the Device Diagnostics-only comparison below.

**Device Diagnostics-only comparison on Stack F:** 1,043.1 MB median
before setup (1,058.2, 1,043.1, and 1,029.5 MB), 1,133.6 MB after five
minutes on Perch, and a 51.69 s first benchmark boot. Quick QA passed.
The probe passed 40 checks, including GPS, and reproduced the known
rear-flash failure. Wi-Fi used 192.168.4.61 after setup. Both isolated
comparisons passed the try gate against Stack F. Stack H included both
changes. The separate boots do not establish an additive RAM saving.

**Stack F live profiling.** A matched boot used 1,057.2 MB before setup
and 1,127.9 MB after five minutes on Perch. The persistent Bluetooth process
accounted for 27.3–27.4 MB PSS. The 32-bit mediaserver's shared-library
mappings accounted for 19.6 MB PSS. The live captures and the ranked plan
are in the benchmark's `tries/investigations/` directory.

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
  same spot, then try by a window.

## Ideas

| Try | Why |
| --- | --- |
| Rest of `kmalloc-128` | kernfs names 21k objects (2.6 MB), ASoC DAPM paths 17.6k (2.2 MB). arm64 has no kmalloc cache below 128 bytes. |
| 32-bit audio HAL | Stack I switched mediaserver to 64-bit using the existing device switch. The vendor audio HAL still runs in 32-bit mode. |
| media.swcodec (22 MB) | Loads every software codec at start (.so 19 MB, 13 MB private). Trimming the APEX needs a frameworks/av fork. |
| Kernel threads | 597 kernel threads: 97 bioset, 35 loop, 21 kverityd and 21 kdmflush (APEX loop mounts), 42 ext4 rsv-conver. Fewer preallocated loop devices. |
| Process count | 60k of 100k `vm_area_struct` are copies of zygote's mappings in forked apps; each app that doesn't start saves its VMAs as well as its PSS. |
| system_server (262 MB) and SystemUI (103 MB) | system_server is mostly file mappings (.jar 53 MB, .apk 40 MB, .dex 40 MB, .so 27 MB); heaps are 22 MB each. Fewer services means fewer classes loaded. |
| Low-RAM mode (`ro.config.low_ram`) | Measure only; shipping it needs Zak. |
| Vendor AIDL CAS example | About 4.8 MB PSS. Try removing the example service APEX and check hardware video and the core apps. |
| Remove x86 and x86_64 WebView projects | Manifest #30 removed both for new syncs. `m nothing` passed with both project directories absent. |
| Perch follow-ups | Go-edition launcher overrides, `PRODUCT_DEXPREOPT_SPEED_APPS`, GestureService only in gesture nav. |
| Per-camera build switches | Charter item; changing the camera set needs Zak to merge. |

## Harness notes

- The first live flash with manifest #42 booted the default image on slot
  b in 50 s. Fastboot reported seven retries before and after activation.
  Android finished its first boot without a repeat boot. The early
  rollback path was unexercised.

- Manifest #32 reads the test slot's bootloader status before writing and
  verifies it after activation. On Stack H, rewriting the exhausted slot and
  activating it took its reported retries from 0 to 7. Current kernels
  disable SLUB debugging, so the trial saves `slab-sysfs.tsv` when
  `/proc/slabinfo` is absent.

- Manifest #36 introduced the settled reading with Perch visible and the
  display awake. The new gate uses three samples, starting five minutes
  after setup. The pre-setup median remains part of the record.

- Stack F received `192.168.4.61` after a wipe, outside the documented
  Wi-Fi QA firewall rule. Manifest PR #28 makes `phone-setup` select the
  factory MAC for the test SSID, but the phone still received `.61` after
  the low-RAM wipe, and again after the Stack H trials. Zak chose to move
  the reservation, the QA fixture, the docs and the firewall rule to `.61`
  instead of restarting the eero network.

- `hardware.cameras`: the rear flash check (`on >= 20`) fails now and then in
  a dark room. Consider a relative threshold.
- `phone-setup`: the pocket check blocked waking the screen (fixed, #18).

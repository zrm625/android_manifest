# Tries

Things to try on the river build. Small tries are stacked into one image;
a stack that passes is kept whole, a stack that fails is split.
How a try runs and when it is kept: [CHARTER.md](CHARTER.md#pipeline-tries).

RAM reference: baseline-2, 1,266 MB (median of 3 boots, 2026-11-23).
Target: 1,000 MB. Now: 1,104 MB (stack D, kept).

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

## Testing

**Stack E**: on river-22.2.

| Try | Branch | Why |
| --- | --- | --- |
| No ftrace | kernel `ftrace-off` | 1,700 trace events are 20,800 pinned tracefs files (about 16 MB of inodes and dentries). |
| No debugfs | kernel `debugfs-off` | 7,400 pinned debugfs files (about 6 MB). Some Qualcomm drivers may mind. |
| No SecureElement | build `drop-secure-element` | Persistent OMAPI app (6.7 MB). No NFC or eSE on the test phone; the NFC stack stays. |

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
| Remove x86 and x86_64 WebView projects | Sync and checkout size (`default.xml`). |
| Perch follow-ups | Go-edition launcher overrides, `PRODUCT_DEXPREOPT_SPEED_APPS`, GestureService only in gesture nav. |
| Per-camera build switches | Charter item; changing the camera set needs Zak to merge. |

## Harness notes

- `hardware.cameras`: the rear flash check (`on >= 20`) fails now and then in
  a dark room. Consider a relative threshold.
- `phone-setup`: the pocket check blocked waking the screen (fixed, #18).

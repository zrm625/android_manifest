# Tries

Things to try on the river build. Small tries are stacked into one image;
a stack that passes is kept whole, a stack that fails is split.
How a try runs and when it is kept: [CHARTER.md](CHARTER.md#pipeline-tries).

RAM reference: baseline-2, 1,266 MB (median of 3 boots, 2026-11-23).
Target: 1,000 MB. Now: 1,141 MB (stack B, kept).

## Kept

| Try | PRs | Result |
| --- | --- | --- |
| Perch replaces Trebuchet | river #6, vendor_lineage #5 | Alone (with the first GPS fix): 1,256 MB, −10 MB. Home, swipe-up and Back pass. |
| 64-bit zygote only (`ZYGOTE_FORCE_64`) | river #8 | Alone: 1,211 MB, −55 MB (native PSS −82 MB). |
| Lazy camera HAL (`ro.camera.enableLazyHal`) | sdm632-common #4 | The camera HAL (42 MB) runs only while a camera is open. All three cameras pass. |
| Java heap headroom | sdm632-common #5 | heapmaxfree 16m → 8m, target 0.6 → 0.75 for every Java process. |
| IPC logging off | kernel #1 | 62 Qualcomm debug-log contexts gone; MemTotal +4 MB. |

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
| `kmalloc-128` (30 MB) | 242k objects, all allocated by 40 s after boot; a one-off allocation, not a leak. The call site is being found with `slub_debug=U` and a diagnostic kernel. |
| SurfaceFlinger buffers (87 MB) | `max_frame_buffer_acquired_buffers` 3 → 2 frees one 10 MB framebuffer. Mind smoothness. |
| Small native services | clearkey DRM (1.9 MB), CAS (2.4 MB), traced and traced_probes (2.7 MB). |
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

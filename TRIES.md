# Tries

Things to try on the river build. Small tries are stacked into one image;
a stack that passes is kept whole, a stack that fails is split.
How a try runs and when it is kept: [CHARTER.md](CHARTER.md#pipeline-tries).

RAM reference: baseline-2, 1,266 MB (median of 3 boots, 2026-11-23).
Target: 1,000 MB. Now: 1,144 MB (stack A, kept).

## Kept

| Try | PRs | Result |
| --- | --- | --- |
| Perch replaces Trebuchet | river #6, vendor_lineage #5 | Alone (with the first GPS fix): 1,256 MB, −10 MB. Home, swipe-up and Back pass. |
| 64-bit zygote only (`ZYGOTE_FORCE_64`) | river #8 | Alone: 1,211 MB, −55 MB (native PSS −82 MB). |
| Lazy camera HAL (`ro.camera.enableLazyHal`) | sdm632-common #4 | The camera HAL (42 MB) runs only while a camera is open. All three cameras pass. |

**Stack A** (all three): 1,144 MB (−122 MB), boot 49.7 s. `phone-qa --probe`:
34 pass. Failed: GPS (see stack B), Brave and Tailscale (harness, below),
camera-HAL process watch (fixed in manifest #19).

## Testing

**Stack B**: on top of stack A.

| Try | Branch | Why |
| --- | --- | --- |
| GPS: `SUPL_MODE=0` and no xtra-daemon | sdm632-common `gnss-standalone` (#3) | With "Use assisted GPS" on, xtra-daemon keeps the GNSS engine from starting (0 status callbacks in 4 min). Off: a satellite in 62–106 s. |
| Java heap headroom | sdm632-common `art-heap` | heapmaxfree 16m → 8m, target 0.6 → 0.75 for every Java process. |
| IPC logging off | kernel `ipc-logging-off` | 62 Qualcomm debug-log contexts in kernel memory. Small. |
| Slab statistics (diagnostic) | kernel `slub-stats` | 140 MB of kernel slab can't be broken down without it. |

## Ideas

| Try | Why |
| --- | --- |
| Kernel slab | 140 MB unreclaimable; rank the caches with stack B's slabinfo. |
| SurfaceFlinger (88 MB) | Triple framebuffer, color management. Mind smoothness. |
| system_server (251 MB) and SystemUI (103 MB) | Services and features nobody uses. |
| Low-RAM mode (`ro.config.low_ram`) | Measure only; shipping it needs Zak. |
| Remove x86 and x86_64 WebView projects | Sync and checkout size (`default.xml`). |
| Perch follow-ups | Go-edition launcher overrides, `PRODUCT_DEXPREOPT_SPEED_APPS`, GestureService only in gesture nav. |
| Per-camera build switches | Charter item; changing the camera set needs Zak to merge. |

## Harness fixes owed

- `apps.brave`: the page loads and its beacon arrives, but the page-script
  text isn't found on screen after the first-run taps.
- `apps.tailscale` and `system.sideload`: Tailscale opens its VPN consent
  dialog at launch, so the check never sees Tailscale in front.
- `phone-setup`: the pocket check blocked waking the screen (fixed, #18).

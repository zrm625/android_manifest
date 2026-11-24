# Tries

Things to try on the river build, in the order the tester means to try them.
How a try runs and when it is kept: [CHARTER.md](CHARTER.md#pipeline-tries).

RAM reference: baseline-2, 1,294–1,312 MB used over 3 boots (2026-11-23).
Target: 1,000 MB.

| # | Try | Why | Status | Result |
| --- | --- | --- | --- | --- |
| 1 | `perch-launcher`: Perch replaces Trebuchet | Better UI; one launcher instead of Trebuchet's recents and taskbar | testing (image `cal-1`) | |
| 2 | `gnss-standalone`: `SUPL_MODE=0` | GPS reported 0 satellites without telephony | testing (image `cal-1`) | |
| 3 | Per-process memory breakdown | Rank the next RAM tries by what actually uses memory (`dumpsys meminfo` on a wiped phone) | next | |
| 4 | 64-bit-only zygote | The 32-bit zygote and its preloads stay resident; every app Zak uses ships arm64 | idea | |
| 5 | zram and swappiness | Compressed swap turns idle memory into available memory | idea | |
| 6 | LMKD and cached-process limits | Fewer cached app processes kept after boot | idea | |
| 7 | Disable idle mainline services | AdServices, OnDevicePersonalization, FederatedCompute, HealthConnect start processes nobody uses (disable, not remove) | idea | |
| 8 | Trim idle system daemons | statsd, incidentd, traced and similar run all the time | idea | |
| 9 | Low-RAM mode (`ro.config.low_ram`) | Measure only; shipping it needs Zak | idea | |
| 10 | Remove x86 and x86_64 WebView projects | Sync and checkout size; the phone is arm64 (`default.xml`) | idea | |
| 11 | Perch follow-ups | Go-edition launcher overrides, `PRODUCT_DEXPREOPT_SPEED_APPS`, start GestureService only in gesture nav | idea | |
| 12 | Per-camera build switches | Charter item; changing the camera set needs Zak to merge | idea | |

"""report.html for one phone-qa run."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

COLORS = {"PASS": "var(--pass)", "FAIL": "var(--fail)", "UNVERIFIED": "var(--unv)", "INFO": "var(--info)"}


def write_html(path: Path, doc: dict, batch: dict | None) -> None:
    e = html.escape
    rows = []
    for cid, r in doc["results"].items():
        det = json.dumps(r.get("details") or {}, indent=1, default=str)
        rows.append(
            f"<tr><td><code>{e(cid)}</code></td>"
            f"<td><span class=st style='background:{COLORS[r['state']]}'>{e(r['state'])}</span></td>"
            f"<td>{e(r['summary'])}"
            + (f"<details><summary>details</summary><pre>{e(det)}</pre></details>" if r.get("details") else "")
            + f"</td><td class=num>{r.get('seconds', 0):.0f}s</td></tr>")
    counts = {s: sum(1 for r in doc["results"].values() if r["state"] == s) for s in COLORS}
    reasons = "".join(f"<li>{e(x)}</li>" for x in doc.get("not_eligible_because") or [])
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(doc["started"]))
    bline = ""
    if batch:
        bline = (f"<p>Batch <code>{e(batch['batch'])}</code> ({e(batch.get('kind', ''))}), build ID "
                 f"<code>{e(str(batch.get('build_id')))}</code>, experiment {str(batch.get('experiment')).lower()}</p>")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>phone-qa {e(doc['run_id'])}</title>
<style>
:root {{ --bg:#fbfbfa; --fg:#1d1d1b; --muted:#6b6b66; --line:#e2e2de;
  --pass:#2f7d4f; --fail:#b3261e; --unv:#9a6a00; --info:#4b5b8a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --fg:#ececea; --muted:#a3a39e; --line:#2e2e2b;
  --pass:#3f9b64; --fail:#d4493f; --unv:#b8860b; --info:#6a7bb0; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; margin:0 auto;
  max-width:1100px; padding:16px; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.muted {{ color:var(--muted); }}
table {{ border-collapse:collapse; width:100%; margin-top:12px; }}
td {{ border-top:1px solid var(--line); padding:6px 8px; vertical-align:top; }}
td.num {{ text-align:right; color:var(--muted); white-space:nowrap; }}
.st {{ color:#fff; border-radius:4px; padding:1px 6px; font-size:12px; font-weight:600; }}
pre {{ white-space:pre-wrap; font-size:12px; max-height:320px; overflow:auto; }}
code {{ font-size:13px; }}
.big {{ font-size:17px; font-weight:600; }}
@media (max-width:640px) {{ td.num {{ display:none; }} }}
</style></head><body>
<h1>phone-qa {e(doc['mode'])} run</h1>
<p class=muted>{e(doc['run_id'])} · {e(when)} · harness {e(doc['harness_version'])} · phone {e(doc['serial'])}</p>
{bline}
<p class=big>Exit {doc['exit_code']} · eligible: {str(doc['eligible']).lower()}</p>
<p>{counts['PASS']} passed, {counts['FAIL']} failed, {counts['UNVERIFIED']} unverified, {counts['INFO']} info.
{'<b>Halted: ' + e(doc['halted']) + '</b>' if doc.get('halted') else ''}</p>
{'<details><summary>Why not eligible</summary><ul>' + reasons + '</ul></details>' if reasons else ''}
<p class=muted>Not checked, by charter: the headphone jack and deep sleep. Auto-rotate (the sensor path) is not
tested, only user rotation.</p>
<table>{''.join(rows)}</table>
</body></html>
"""
    path.write_text(page)

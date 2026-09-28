#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_state_prn_trend.py; original SHA-256 9189969d69dfffb1b72a51a56850dd23a5870632d00b0400615a71df8975c74e; classification active by owner override (was needs-decision, low; scenario-builders); versioned 2026-09-11.
"""build_state_prn_trend.py — State PRN scenario trend chart (reference-only).

Owner directive 10 Aug 2026: states now have federal-parity scenario capability
(build_state_prn.py v2 writes <state>-state-scenarios.json each build). This
script accumulates per-state PRN scenario history and emits a self-contained
HTML trend chart (vanilla JS + inline SVG, no CDN) — reference only, NEVER for
Aila (same rule as the federal forecast-trend.html).

History source: work/reports/prn-history/<state>.json — an append-only
snapshot of each state's scenario table (base-case BN/PH/PN + narrative rows)
captured on every build_state_prn.py run.

Output: 02_FORECAST/history/state-prn-trend.html
"""

import json
import os
from pathlib import Path
from datetime import datetime

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    anchor_path = Path(anchor or __file__).resolve()
    return anchor_path.parents[2]


ROOT = str(resolve_repository_root())
RES_STATES = os.path.join(ROOT, "work", "forecast", "latest", "states")
HIST_DIR = os.path.join(ROOT, "work", "reports", "prn-history")
OUT_DIR = os.path.join(ROOT, "02_FORECAST", "history")
OUT_FILE = os.path.join(OUT_DIR, "state-prn-trend.html")

STATES = ["Melaka", "Sarawak", "Pahang", "Perak", "Perlis"]
COLORS = {"BN": "#3b82f6", "PH": "#ef4444", "PN": "#10b981"}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    series = {}
    for st in STATES:
        scen_json = os.path.join(RES_STATES, f"DUN {st}", f"{st.lower()}-state-scenarios.json")
        if not os.path.exists(scen_json):
            continue
        with open(scen_json, encoding="utf-8") as f:
            data = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        os.makedirs(HIST_DIR, exist_ok=True)
        hist_path = os.path.join(HIST_DIR, f"{st.lower()}.json")
        hist = []
        if os.path.exists(hist_path):
            with open(hist_path, encoding="utf-8") as f:
                hist = json.load(f)
        # append today's snapshot if the base case changed since the last entry
        base = next((s for s in data.get("scenarios", [])
                     if s.get("scenario", "").startswith("Southern resurgence")), None)
        if not base:
            base = data.get("scenarios", [{}])[0]
        snapshot = {"date": today, "base": {
            "BN": base.get("BN", 0), "PH": base.get("PH", 0), "PN": base.get("PN", 0)},
            "scenarios": data.get("scenarios", [])}
        if hist and hist[-1].get("date") == today:
            hist[-1] = snapshot          # same-day rebuild → replace, don't duplicate
        else:
            hist.append(snapshot)
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=1)
        series[st] = hist
        print(f"{st}: {len(hist)} history points (latest base BN {base.get('BN')} / PH {base.get('PH')} / PN {base.get('PN')})")

    html = _render(series)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved: {OUT_FILE}")


def _render(series):
    states_js = json.dumps(series, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>State PRN Scenario Trends</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:24px;background:#0f172a;color:#e2e8f0}}
h1{{font-size:18px}} h2{{font-size:14px;color:#94a3b8;margin-top:28px}}
select{{background:#1e293b;color:#e2e8f0;border:1px solid #334155;padding:6px 10px;border-radius:6px}}
svg text{{fill:#cbd5e1;font-size:11px}}
.legend{{font-size:12px;color:#cbd5e1}} .note{{color:#64748b;font-size:12px;margin-top:6px}}
</style></head><body>
<h1>📈 State PRN Scenario Trends — reference only (never deployed)</h1>
<p class="note">Base-case (Southern resurgence) BN/PH/PN seat lines per update cycle for each Tier-1 state. Built from <code>&lt;state&gt;-state-scenarios.json</code> snapshots (build_state_prn.py v2, 10 Aug 2026).</p>
<label>State: <select id="sel"></select></label>
<div id="charts"></div>
<script>
const DATA = {states_js};
const COLORS = {json.dumps(COLORS)};
const states = Object.keys(DATA);
const sel = document.getElementById('sel');
states.forEach(s => {{ const o = document.createElement('option'); o.value = s; o.text = s; sel.appendChild(o); }});
function draw(){{
  const st = sel.value; const hist = DATA[st] || [];
  const dates = hist.map(h => h.date);
  const blocs = ['BN','PH','PN'];
  let out = '';
  out += '<h2>Base-case seats over time</h2>';
  out += '<svg width="860" height="300"></svg>';
  out += '<div class="legend">' + blocs.map(b => '<span style="color:' + COLORS[b] + '">■ ' + b + '</span> ').join('') + '</div>';
  document.getElementById('charts').innerHTML = out;
  const svg = document.querySelector('svg');
  if (!dates.length) return;
  const W = 860, H = 300, pad = {{l:40, r:16, t:16, b:30}};
  const maxY = Math.max(...hist.map(h => Math.max(h.base.BN, h.base.PH, h.base.PN))) + 5;
  const x = i => pad.l + i * (W - pad.l - pad.r) / Math.max(1, dates.length - 1);
  const y = v => H - pad.b - (v / maxY) * (H - pad.t - pad.b);
  blocs.forEach(b => {{
    let path = hist.map((h, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(h.base[b]).toFixed(1)).join(' ');
    let el = document.createElementNS('http://www.w3.org/2000/svg','path');
    el.setAttribute('d', path); el.setAttribute('fill','none');
    el.setAttribute('stroke', COLORS[b]); el.setAttribute('stroke-width','2.5');
    svg.appendChild(el);
    // points + labels
    hist.forEach((h, i) => {{
      let c = document.createElementNS('http://www.w3.org/2000/svg','circle');
      c.setAttribute('cx', x(i)); c.setAttribute('cy', y(h.base[b]));
      c.setAttribute('r','3'); c.setAttribute('fill', COLORS[b]); svg.appendChild(c);
      if (i === hist.length - 1) {{
        let t = document.createElementNS('http://www.w3.org/2000/svg','text');
        t.setAttribute('x', x(i) + 4); t.setAttribute('y', y(h.base[b]) + 3);
        t.textContent = b + ' ' + h.base[b]; svg.appendChild(t);
      }}
    }});
  }});
  dates.forEach((d, i) => {{
    let t = document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x', x(i)); t.setAttribute('y', H - 10); t.setAttribute('text-anchor','middle');
    t.textContent = d.slice(5); svg.appendChild(t);
  }});
  for (let v = 0; v <= maxY; v += Math.ceil(maxY/6)) {{
    let t = document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x', pad.l - 6); t.setAttribute('y', y(v) + 3); t.setAttribute('text-anchor','end');
    t.textContent = v; svg.appendChild(t);
  }}
}}
sel.addEventListener('change', draw);
draw();
</script></body></html>"""


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
build_vercel.py — Build HitungKerusi 222 Vercel deploy pages.

This folder is SELF-CONTAINED: it has its own copy of data.js, report.js,
CSS, and JS. No dependency on the Aila folder.
To refresh data: manually copy js/data.js + js/report.js from hitung_kerusi_222_aila/

Run:  python3 build_vercel.py [--preflight]
  --preflight: run the read-only production validation gates, including a live
               `origin/main` SHA query. This script never invokes Vercel;
               run `vercel deploy --prod --yes` separately after preflight.
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
JS_DIR = os.path.join(ROOT, "js")
CSS_DIR = os.path.join(ROOT, "css")

# Gate limits
MAX_BUNDLE_BYTES = 2_000_000     # data.js + report.js combined (generous — this
                                  # repo's constraint is per-file/page weight, not
                                  # Aila's tight 300KB; adjust if a real limit is set)
MAX_PAGE_BYTES = 900_000         # single HTML page sanity ceiling

# Every browser-served JavaScript file referenced by a required page. Generated
# staging copies are deliberately excluded: only the deployed asset names count.
REQUIRED_JS_ASSETS = (
    "data.js", "report.js", "components.js", "dun-data.js", "app-landing.js",
    "app-dashboard.js", "app-maps.js", "app-news.js", "app-report.js",
    "app-scenarios.js", "app-seats.js", "app-states.js",
)


# ---------------------------------------------------------------------------
# Data loading (pure read — no computation)
# ---------------------------------------------------------------------------

def load_data():
    """Load data.js as a Python dict."""
    raw = open(os.path.join(JS_DIR, "data.js"), encoding="utf-8").read()
    raw = raw.replace("window.GE16_APP_DATA = ", "").strip().rstrip(";").strip()
    return json.loads(raw)


def load_report(lang="ms"):
    """Extract report sections from report.js."""
    raw = open(os.path.join(JS_DIR, "report.js"), encoding="utf-8").read()
    pattern = r"window\.GE16_REPORT_MS\s*=\s*(\[.*?\])\s*;" if lang == "ms" else r"window\.GE16_REPORT_EN\s*=\s*(\[.*?\])\s*;"
    m = re.search(pattern, raw, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    return []


# ---------------------------------------------------------------------------
# HTML page builders
# ---------------------------------------------------------------------------

NAV_LINKS = [
    ("Halaman Utama", "Home", "/"),
    ("Panel Kawalan", "Dashboard", "/app/"),
    ("Peta", "Map", "/app/maps.html"),
    ("Jadual Kerusi", "Seats", "/app/seats.html"),
    ("Senario", "Scenarios", "/app/scenarios.html"),
    ("Berita", "News", "/berita.html"),
    ("Laporan", "Report", "/laporan.html"),
]

STATE_LINKS = [
    ("Johor", "Johor", "/state/johor.html"),
    ("Kedah", "Kedah", "/state/kedah.html"),
    ("Kelantan", "Kelantan", "/state/kelantan.html"),
    ("Melaka", "Melaka", "/state/melaka.html"),
    ("N. Sembilan", "N. Sembilan", "/state/n9.html"),
    ("Pahang", "Pahang", "/state/pahang.html"),
    ("Perak", "Perak", "/state/perak.html"),
    ("Perlis", "Perlis", "/state/perlis.html"),
    ("Pulau Pinang", "Penang", "/state/penang.html"),
    ("Sabah", "Sabah", "/state/sabah.html"),
    ("Sarawak", "Sarawak", "/state/sarawak.html"),
    ("Selangor", "Selangor", "/state/selangor.html"),
    ("Terengganu", "Terengganu", "/state/terengganu.html"),
]


def build_landing(data):
    """Landing page — editorial splash with 3 CTA cards."""
    # The landing page is now a hand-authored public-facing surface.
    # Keep this builder as a safe passthrough so data refreshes do not erase its UI.
    html_path = os.path.join(ROOT, "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    s = data.get("summary", {})
    updated = s.get("updated", "")[:10] if s.get("updated") else "—"

    return f"""<!DOCTYPE html>
<html lang="ms">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="Hitung Kerusi 222 — projeksi telus PRU16 Malaysia. 222 kerusi Parlimen, senario berbeza, semua nilai dikira daripada data.">
  <title>Hitung Kerusi 222 — Projeksi PRU16</title>
  <link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
  <link rel="icon" type="image/png" href="/assets/favicon-hk222.png">
  <link rel="stylesheet" href="/css/style.css">
</head>
<body class="landing">
  <div id="topbar-root"></div>
  <div class="landing-wrap">
    <div class="landing-content">
      <div class="kicker">Persekutuan · PRU16</div>
      <h1 class="landing-title">Hitung <span>Kerusi</span> 222</h1>
      <p class="landing-subtitle">Projeksi telus Pilihan Raya Umum ke-16<br>222 kerusi Parlimen — setiap nilai dikira daripada data</p>

      <div class="cta-grid">
        <a href="/app/" class="cta-card">
          <div class="cta-icon">📊</div>
          <h3>Panel Kawalan</h3>
          <p>222 kerusi, 8 senario, {s.get('flips', 21)} kerusi bertukar tangan</p>
        </a>
        <a href="/app/maps.html" class="cta-card">
          <div class="cta-icon">🗺️</div>
          <h3>Peta Interaktif</h3>
          <p>Semua 222 kerusi — klik untuk lihat maklumat penuh</p>
        </a>
        <a href="/app/seats.html" class="cta-card">
          <div class="cta-icon">📋</div>
          <h3>Jadual Kerusi</h3>
          <p>Semak, susun, dan tapis 222 kerusi</p>
        </a>
      </div>

      <div class="landing-meta">
        <span>Dikemas kini: <strong>{updated}</strong> · PRU16</span>
        <span><a href="/tentang.html">Metodologi</a></span>
      </div>
    </div>
  </div>
  <footer class="landing-footer">
    <p>© Hitung Kerusi 222 · Projeksi analisis sendiri · <a href="/laporan.html">Laporan Penuh</a></p>
  </footer>

  <script src="/js/data.js"></script>
  <script src="/js/components.js"></script>
</body>
</html>
"""


def build_maps_static(data):
    """Maps page — static HTML, uses p5.js from CDN + app-maps.js renderer."""
    html_path = os.path.join(ROOT, "app", "maps.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def build_seats_static(data):
    """Seats table page — static HTML, uses app-seats.js renderer."""
    html_path = os.path.join(ROOT, "app", "seats.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def build_scenarios_static(data):
    """Scenarios page — static HTML, uses app-scenarios.js renderer."""
    html_path = os.path.join(ROOT, "app", "scenarios.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def build_states_static(data):
    """State explorer — static shell with client-side state selection."""
    html_path = os.path.join(ROOT, "app", "states.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def build_news_static(data):
    """News page — static HTML, uses app-news.js renderer."""
    html_path = os.path.join(ROOT, "berita.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def build_report_static(data):
    """Report page — static HTML, uses app-report.js renderer."""
    html_path = os.path.join(ROOT, "laporan.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def build_dashboard(data):
    """Dashboard page — pure renderer, all data from window.GE16_APP_DATA."""
    # Dynamic values render client-side; preserve the hand-authored dashboard shell.
    html_path = os.path.join(ROOT, "app", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    s = data.get("summary", {})
    ranges = s.get("govt_range", [s.get("govt_p10", s["govt_p50"]), s.get("govt_p90", s["govt_p50"])])

    return f"""<!DOCTYPE html>
<html lang="ms">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="Panel Kawalan Hitung Kerusi 222 — projeksi telus PRU16.">
  <title>Panel Kawalan — Hitung Kerusi 222</title>
  <link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
  <link rel="icon" type="image/png" href="/assets/favicon-hk222.png">
  <link rel="stylesheet" href="/css/style.css">
</head>
<body class="dashboard federal">
  <div id="topbar-root"></div>

  <main>
    <!-- Hero KPIs -->
    <section class="section hero-kpi">
      <div class="container">
        <div class="kicker">Persekutuan · PRU16</div>
        <div class="kpi-hero">
          <div class="kpi-hero__num">{s.get('govt_p50', 140)} / 222</div>
          <div class="kpi-hero__label">Kerusi Kerajaan (P50)</div>
          <div class="kpi-hero__sub">dari 222 · ±{abs(ranges[0] - s.get('govt_p50', 140))}–{abs(s.get('govt_p50', 140) - ranges[1])} rentang P10–P90</div>
        </div>
        <div class="kpi-grid">
          <div class="kpi-card"><div class="kpi-card__num">{s.get('majority_pct', 100)}%</div><div class="kpi-card__label">P(majoriti)</div></div>
          <div class="kpi-card"><div class="kpi-card__num">+{len(s.get('flips_list', []))}</div><div class="kpi-card__label">Kerusi bertukar dalam unjuran asas</div></div>
          <div class="kpi-card"><div class="kpi-card__num">+{s.get('econ_term', 4.59)}%</div><div class="kpi-card__label">Pecahan ekonomi</div></div>
        </div>
        <div class="coalition-bar">
          <span class="coalition-text">{s.get('coalition_text', '')}</span>
        </div>
      </div>
    </section>

    <!-- Scenario Chips -->
    <section class="section">
      <div class="container">
        <div class="kicker">Senarai Senario</div>
        <div class="scenario-chips" id="scenario-chips">
          <!-- Populated by app-dashboard.js -->
        </div>
      </div>
    </section>

    <!-- Seat Composition Bar -->
    <section class="section">
      <div class="container">
        <div class="chart-wrap">
          <canvas id="house-bar" width="1200" height="180"></canvas>
        </div>
      </div>
    </section>

    <!-- Flip Table -->
    <section class="section">
      <div class="container">
        <div class="kicker">Kerusi Bertukar Tangan</div>
        <div class="table-wrap">
          <table class="flip-table" id="flip-table">
            <!-- Populated by app-dashboard.js -->
          </table>
        </div>
      </div>
    </section>

    <!-- Tight Seats + History -->
    <section class="section">
      <div class="container">
        <div class="tight-grid">
          <div class="tight-card">
            <h3>3 Terdekat</h3>
          </div>
          <div class="history-card">
            <h3>Evolutif Mingguan</h3>
          </div>
        </div>
      </div>
    </section>
  </main>

  <footer>
    <div class="container">
      <p>Hitung Kerusi 222 · Unjuran telus PRU16 · Sumber: EC · ElectionData.MY / MECo · DOSM · Parlimen</p>
    </div>
  </footer>

  <script src="/js/data.js"></script>
  <script src="/js/report.js"></script>
  <script src="/js/components.js"></script>
  <script src="/js/app-dashboard.js"></script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Validation gates for the explicit read-only production preflight.
# ---------------------------------------------------------------------------

def gate_git_release_integrity():
    """Require a clean tree at the same SHA as the live origin/main ref."""
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
    )
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip() or status.stdout.strip()}"
    if status.stdout.strip():
        return False, "working tree is not clean"

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
    )
    if head.returncode != 0:
        return False, f"git rev-parse HEAD failed: {head.stderr.strip() or head.stdout.strip()}"
    local_sha = head.stdout.strip()

    remote = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/main"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if remote.returncode != 0:
        return False, f"live origin/main query failed: {remote.stderr.strip() or remote.stdout.strip()}"
    fields = remote.stdout.strip().split()
    if len(fields) < 2 or fields[1] != "refs/heads/main":
        return False, "live origin/main query returned no refs/heads/main SHA"
    remote_sha = fields[0]
    if local_sha != remote_sha:
        return False, (
            f"local HEAD {local_sha} does not match live origin/main {remote_sha}"
        )
    return True, f"clean tree; HEAD matches live origin/main ({local_sha})"

def gate_manifest_verify():
    """Re-run build_adapter.py --check: manifest hash verify, fully read-only.
    Returns (ok, message)."""
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "build_adapter.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    ok = r.returncode == 0
    out = r.stdout + r.stderr
    if ok:
        m = re.search(r"delivery_id=(\S+)\s+files=(\d+)", out)
        summary = f"delivery_id={m.group(1)} files={m.group(2)}, --check PASSED, zero writes" if m else "PASSED"
    else:
        lines = [l for l in out.splitlines() if l.strip()]
        summary = lines[-1] if lines else "FAILED (no output)"
    return ok, summary


def gate_link_check():
    """Run link_checker.py: 0 broken internal links required."""
    checker = os.path.join(ROOT, "link_checker.py")
    if not os.path.isfile(checker):
        return False, "link_checker.py not found in repo"
    r = subprocess.run([sys.executable, checker], cwd=ROOT, capture_output=True, text=True)
    ok = r.returncode == 0
    out = r.stdout + r.stderr
    lines = [l for l in out.splitlines() if l.strip()]
    summary = lines[-1] if lines else ("PASSED" if ok else "FAILED (no output)")
    return ok, summary


def gate_bundle_and_pages():
    """Check data.js+report.js combined size and every generated HTML page
    against sanity ceilings. Returns (ok, message)."""
    problems = []
    for fn in REQUIRED_JS_ASSETS:
        p = os.path.join(JS_DIR, fn)
        if not os.path.isfile(p):
            problems.append(f"js/{fn} is missing")

    bundle = 0
    for fn in ("data.js", "report.js"):
        p = os.path.join(JS_DIR, fn)
        if os.path.isfile(p):
            bundle += os.path.getsize(p)
    if bundle > MAX_BUNDLE_BYTES:
        problems.append(f"js bundle {bundle:,} B exceeds {MAX_BUNDLE_BYTES:,} B limit")

    page_count = 0
    for _tmpl, filename in PAGES:
        p = os.path.join(ROOT, filename)
        if not os.path.isfile(p):
            problems.append(f"{filename} is missing")
            continue
        page_count += 1
        size = os.path.getsize(p)
        if size > MAX_PAGE_BYTES:
            problems.append(f"{filename} is {size:,} B, exceeds {MAX_PAGE_BYTES:,} B page limit")

    msg = f"bundle={bundle:,} B (limit {MAX_BUNDLE_BYTES:,}), pages checked={page_count}"
    if problems:
        return False, msg + " — " + "; ".join(problems)
    return True, msg


def run_gates():
    """Run every gate in order; fail closed unless every gate passes."""
    gates = [
        ("git_release_integrity (clean HEAD = live origin/main)", gate_git_release_integrity),
        ("manifest_verify (build_adapter.py --check)", gate_manifest_verify),
        ("link_check (link_checker.py)", gate_link_check),
        ("bundle_and_page_limits", gate_bundle_and_pages),
    ]
    results = []
    all_ok = True
    for name, fn in gates:
        ok, msg = fn()
        results.append((name, ok, msg))
        if not ok:
            all_ok = False
    return all_ok, results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PAGES = [
    ("landing", "index.html"),
    ("dashboard", "app/index.html"),
    ("maps_static", "app/maps.html"),
    ("seats_static", "app/seats.html"),
    ("scenarios_static", "app/scenarios.html"),
    ("states_static", "app/states.html"),
    ("news_static", "berita.html"),
    ("report_static", "laporan.html"),
]


def main():
    preflight = "--preflight" in sys.argv

    if "--deploy" in sys.argv or "--preview" in sys.argv:
        sys.exit(
            "FATAL: build_vercel.py does not invoke Vercel. Run --preflight, then "
            "use the separate explicit command: vercel deploy --prod --yes"
        )

    if preflight:
        print("=== build_vercel.py — production preflight (read-only) ===\n")
        all_ok, results = run_gates()
        for name, ok, msg in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {msg}")
        if not all_ok:
            sys.exit("\n=== PREFLIGHT FAILED — Vercel was not invoked. ===")
        print("\n=== PREFLIGHT PASSED — Vercel was not invoked. ===")
        return

    print("=== build_vercel.py — Self-contained build ===\n")

    required_ok, required_message = gate_bundle_and_pages()
    if not required_ok:
        print(f"FATAL: required deploy files failed validation: {required_message}")
        sys.exit(1)

    print("[1/3] Loading data.js for page builders...")
    data = load_data()
    s = data.get("summary", {})
    print(f"  ✓ Loaded data.js: {len(data.get('master', []))} seats, "
          f"{len(s.get('scenarios_display', []))} scenarios, "
          f"{len(s.get('flips_list', []))} flips")

    print("\n[2/3] Building pages...")
    builders = {"landing": build_landing, "dashboard": build_dashboard, "maps_static": build_maps_static, "seats_static": build_seats_static, "scenarios_static": build_scenarios_static, "states_static": build_states_static, "news_static": build_news_static, "report_static": build_report_static}
    for template_name, filename in PAGES:
        builder = builders.get(template_name)
        if not builder:
            raise RuntimeError(f"Unknown required template: {template_name}")
        html = builder(data)
        outpath = os.path.join(ROOT, filename)
        os.makedirs(os.path.dirname(outpath), exist_ok=True)
        with open(outpath, "w", encoding="utf-8") as f:
            f.write(html)
        size = os.path.getsize(outpath)
        print(f"  ✓ {filename} ({size:,} B)")

    print("\n[3/3] File inventory:")
    for subdir, prefix in [("js", "js/"), ("css", "css/"), ("app", "app/")]:
        dir_path = os.path.join(ROOT, subdir)
        if os.path.isdir(dir_path):
            for f in sorted(os.listdir(dir_path)):
                path = os.path.join(dir_path, f)
                if os.path.isfile(path):
                    size = os.path.getsize(path)
                    print(f"  ✓ {prefix}{f} ({size:,} B)")

    print()
    total = 0
    for root, dirs, files in os.walk(ROOT):
        if ".git" in root or "__pycache__" in root or "node_modules" in root:
            continue
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    print(f"Total deploy size: {total:,} B")
    print("Done.")

if __name__ == "__main__":
    main()

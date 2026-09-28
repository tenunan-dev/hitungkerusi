#!/usr/bin/env python3
"""Focused regression tests for build_vercel required-file validation."""
import os
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import build_vercel


class RequiredBuildFilesTest(unittest.TestCase):
    def setUp(self):
        self.original_root = build_vercel.ROOT
        self.original_js_dir = build_vercel.JS_DIR
        self.original_pages = build_vercel.PAGES
        self.original_required_js_assets = build_vercel.REQUIRED_JS_ASSETS
        self.tempdir = tempfile.TemporaryDirectory()
        build_vercel.ROOT = self.tempdir.name
        build_vercel.JS_DIR = os.path.join(self.tempdir.name, "js")
        build_vercel.REQUIRED_JS_ASSETS = ("data.js", "report.js")
        os.makedirs(build_vercel.JS_DIR)

    def tearDown(self):
        build_vercel.ROOT = self.original_root
        build_vercel.JS_DIR = self.original_js_dir
        build_vercel.PAGES = self.original_pages
        build_vercel.REQUIRED_JS_ASSETS = self.original_required_js_assets
        self.tempdir.cleanup()

    def write(self, relative_path, content="ok"):
        path = os.path.join(build_vercel.ROOT, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_missing_report_js_fails_required_asset_check(self):
        self.write("js/data.js")
        self.write("index.html")
        build_vercel.PAGES = [("landing", "index.html")]

        ok, message = build_vercel.gate_bundle_and_pages()

        self.assertFalse(ok)
        self.assertIn("js/report.js is missing", message)

    def test_missing_required_page_fails_required_page_check(self):
        self.write("js/data.js")
        self.write("js/report.js")
        build_vercel.PAGES = [("landing", "index.html")]

        ok, message = build_vercel.gate_bundle_and_pages()

        self.assertFalse(ok)
        self.assertIn("index.html is missing", message)


class DashboardFlipMetricTest(unittest.TestCase):
    def test_static_dashboard_uses_deterministic_flip_list_length(self):
        summary = {
            "govt_p50": 140,
            "govt_p10": 139,
            "govt_p90": 141,
            "majority_pct": 100,
            "flips": 21,
            "flips_list": [{}] * 20,
        }
        with mock.patch.object(build_vercel.os.path, "exists", return_value=False):
            html = build_vercel.build_dashboard({"summary": summary})

        self.assertIn(
            '<div class="kpi-card__num">+20</div><div class="kpi-card__label">Kerusi bertukar dalam unjuran asas</div>',
            html,
        )


class DashboardShellFlipMetricTest(unittest.TestCase):
    def test_federal_shell_displays_flip_list_count_and_base_forecast_label(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # V3: tests live in site/tests; app/ + js/ are at site/
        node_script = r'''
const fs = require('fs');
const shell = fs.readFileSync(process.argv[1], 'utf8');
const dashboard = fs.readFileSync(process.argv[2], 'utf8');
const cards = [...shell.matchAll(/<div class="kpi-card(?: [^"]*)?"><div class="kpi-card__num">([^<]*)<\/div><div class="kpi-card__label">([^<]*)<\/div><\/div>/g)].map(function (match) {
  const label = { textContent: match[2] };
  return { textContent: match[1], parentElement: { querySelector: function () { return label; } }, label: label };
});
if (cards.length < 3) throw new Error('Dashboard shell KPI cards were not found');
const empty = null;
global.window = {
  GE16_APP_DATA: {
    master: [],
    summary: {
      flips: 999,
      flips_list: [{}, {}, {}],
      govt_p50: 140,
      govt_p10: 139,
      govt_p90: 141,
      scenarios_display: [{ govt: 140 }, { govt: 140 }]
    }
  },
  hk222: {},
  localStorage: { getItem: function () { return null; } }
};
global.document = {
  readyState: 'complete',
  body: { classList: { contains: function (name) { return name === 'dashboard'; } } },
  querySelector: function (selector) {
    if (selector === '.kpi-hero__num') return empty;
    if (selector === '.coalition-text') return empty;
    if (selector === '.tight-card') return empty;
    if (selector === '.history-card') return empty;
    return empty;
  },
  querySelectorAll: function (selector) { return selector === '.kpi-card__num' ? cards : []; },
  getElementById: function () { return empty; }
};
eval(dashboard);
process.stdout.write(JSON.stringify({ label: cards[1].label.textContent, value: cards[1].textContent }));
'''
        result = subprocess.run(
            [
                "node", "-e", node_script,
                os.path.join(root, "app", "index.html"),
                os.path.join(root, "js", "app-dashboard.js"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        displayed = json.loads(result.stdout)
        self.assertEqual(displayed["label"], "Kerusi bertukar dalam unjuran asas")
        self.assertEqual(displayed["value"], "+3")


class ProductionGitPreflightTest(unittest.TestCase):
    def test_dirty_tree_short_circuits_before_live_remote_sha_lookup(self):
        dirty_status = mock.Mock(returncode=0, stdout=" M build_vercel.py\n", stderr="")
        with mock.patch.object(
            build_vercel.subprocess, "run", return_value=dirty_status
        ) as run:
            ok, message = build_vercel.gate_git_release_integrity()

        self.assertFalse(ok)
        self.assertIn("working tree is not clean", message)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["git", "status", "--porcelain"])

    def test_live_origin_main_sha_must_match_clean_head(self):
        sha = "a" * 40
        completed = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout=sha + "\n", stderr=""),
            mock.Mock(returncode=0, stdout=f"{sha}\trefs/heads/main\n", stderr=""),
        ]
        with mock.patch.object(build_vercel.subprocess, "run", side_effect=completed) as run:
            ok, message = build_vercel.gate_git_release_integrity()

        self.assertTrue(ok, message)
        self.assertIn(sha, message)
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["git", "ls-remote", "origin", "refs/heads/main"],
        )

    def test_live_origin_main_mismatch_fails_the_preflight(self):
        completed = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout=("a" * 40) + "\n", stderr=""),
            mock.Mock(returncode=0, stdout=("b" * 40) + "\trefs/heads/main\n", stderr=""),
        ]
        with mock.patch.object(build_vercel.subprocess, "run", side_effect=completed):
            ok, message = build_vercel.gate_git_release_integrity()

        self.assertFalse(ok)
        self.assertIn("does not match live origin/main", message)


class DeploymentCliSafetyTest(unittest.TestCase):
    def test_deploy_is_rejected_without_vercel_invocation(self):
        self._assert_rejected_without_vercel("--deploy")

    def test_preview_is_rejected_without_vercel_invocation(self):
        self._assert_rejected_without_vercel("--preview")

    def _assert_rejected_without_vercel(self, flag):
        with (
            mock.patch.object(sys, "argv", ["build_vercel.py", flag]),
            mock.patch.object(build_vercel.subprocess, "run") as run,
        ):
            with self.assertRaises(SystemExit) as raised:
                build_vercel.main()

        self.assertIn("does not invoke Vercel", str(raised.exception))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

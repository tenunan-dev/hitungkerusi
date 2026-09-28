#!/usr/bin/env python3
"""Regression tests for delivery provenance validation."""
import os
import sys
import tempfile
import unittest
import warnings
from unittest import mock

import build_adapter


class CheckProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.original_js_dir = build_adapter.JS_DIR
        self.original_missing_data_md = build_adapter.MISSING_DATA_MD
        self.tempdir = tempfile.TemporaryDirectory()
        build_adapter.JS_DIR = os.path.join(self.tempdir.name, "js")
        build_adapter.MISSING_DATA_MD = os.path.join(self.tempdir.name, "MISSING_DATA.md")
        os.makedirs(build_adapter.JS_DIR)
        with open(os.path.join(build_adapter.JS_DIR, "data.js"), "w", encoding="utf-8") as f:
            f.write('window.GE16_APP_DATA = {"_adapter":{"delivery_id":"H-stale"}};\n')

    def tearDown(self):
        build_adapter.JS_DIR = self.original_js_dir
        build_adapter.MISSING_DATA_MD = self.original_missing_data_md
        self.tempdir.cleanup()

    def test_check_rejects_generated_data_from_a_different_delivery(self):
        manifest = {"delivery_id": "H-current", "file_count": 0}
        with (
            mock.patch.object(build_adapter, "load_manifest", return_value=manifest),
            mock.patch.object(build_adapter, "verify_delivery", return_value=[]),
            mock.patch.object(
                build_adapter,
                "build_app_data",
                side_effect=AssertionError("provenance failure must stop before rendering"),
            ),
            mock.patch.object(sys, "argv", ["build_adapter.py", "--check"]),
            self.assertRaises(SystemExit) as raised,
        ):
            build_adapter.main()

        self.assertIn("provenance", str(raised.exception).lower())
        self.assertIn("H-stale", str(raised.exception))
        self.assertIn("H-current", str(raised.exception))

    def test_apply_regenerates_stale_data_after_validating_manifest(self):
        """The explicit refresh path repairs, rather than rejects, stale output."""
        manifest = {"delivery_id": "H-current", "file_count": 0}
        rendered = {
            "_adapter": {},
            "master": [],
            "projection": [],
            "summary": {"flips_list": [], "scenarios_display": []},
        }
        with (
            mock.patch.object(build_adapter, "load_manifest", return_value=manifest),
            mock.patch.object(build_adapter, "verify_delivery", return_value=[]),
            mock.patch.object(build_adapter, "build_app_data", return_value=(rendered, [], [])),
            mock.patch.object(build_adapter, "build_report", return_value=([], [], {}, {}, [])),
            mock.patch.object(build_adapter, "refresh_geo", return_value=([], [])),
            mock.patch.object(build_adapter, "write_missing_data"),
            mock.patch.object(sys, "argv", ["build_adapter.py", "--apply"]),
        ):
            build_adapter.main()

        with open(os.path.join(build_adapter.JS_DIR, "data.js"), encoding="utf-8") as f:
            refreshed = f.read()
        self.assertIn('"delivery_id":"H-current"', refreshed)

    def test_apply_validates_before_refresh_and_writes_no_generated_targets_on_failure(self):
        manifest = {"delivery_id": "H-current", "file_count": 1}
        events = []

        def reject_manifest(_manifest):
            events.append("validate")
            return ["HASH MISMATCH: data/app-data.json"]

        generated_targets = [
            os.path.join(build_adapter.JS_DIR, "data.generated.js"),
            os.path.join(build_adapter.JS_DIR, "report.generated.js"),
            build_adapter.MISSING_DATA_MD,
        ]
        with (
            mock.patch.object(build_adapter, "load_manifest", return_value=manifest),
            mock.patch.object(build_adapter, "verify_delivery", side_effect=reject_manifest),
            mock.patch.object(
                build_adapter,
                "refresh_geo",
                side_effect=lambda *_args, **_kwargs: events.append("refresh"),
            ) as refresh_geo,
            mock.patch.object(sys, "argv", ["build_adapter.py", "--apply"]),
        ):
            with self.assertRaises(SystemExit) as raised:
                build_adapter.main()

        self.assertIn("delivery failed verification", str(raised.exception))
        self.assertEqual(events, ["validate"])
        refresh_geo.assert_not_called()
        for target in generated_targets:
            self.assertFalse(os.path.exists(target), target)

    def test_generated_file_writes_are_warning_free(self):
        manifest = {"delivery_id": "H-current", "file_count": 0}
        rendered = {
            "_adapter": {},
            "master": [],
            "projection": [],
            "summary": {"flips_list": [], "scenarios_display": []},
        }
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with (
                mock.patch.object(build_adapter, "load_manifest", return_value=manifest),
                mock.patch.object(build_adapter, "verify_delivery", return_value=[]),
                mock.patch.object(build_adapter, "build_app_data", return_value=(rendered, [], [])),
                mock.patch.object(build_adapter, "build_report", return_value=([], [], {}, {}, [])),
                mock.patch.object(build_adapter, "refresh_geo", return_value=([], [])),
                mock.patch.object(build_adapter, "write_missing_data"),
                mock.patch.object(sys, "argv", ["build_adapter.py", "--apply"]),
            ):
                build_adapter.main()

        resource_warnings = [warning for warning in caught if warning.category is ResourceWarning]
        self.assertEqual(resource_warnings, [])


if __name__ == "__main__":
    unittest.main()

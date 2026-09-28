"""Stage 1 refresh ordering and per-command network containment."""
import json
import os
import pathlib
import subprocess
from unittest import mock

from cron import run_stage
from test_isolated_stage_adapters import (
    FixtureSandboxTests, fixture_root, write_script, DATA_CONTRACT, VERSION,
)

EXPECTED = ("data-collect-news", "data-collect-polls", "data-collect-candidates",
            "data-commit-news", "data-refresh-canonical", "data-validate-canonical")


class CollectionLoopTests(FixtureSandboxTests):
    def setUp(self):
        super().setUp()
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def test_stale_manifest_can_refresh_and_handoff_binds_new_content_in_order(self):
        self.assertEqual(EXPECTED, run_stage.STAGE1_COMMAND_NAMES)
        self.assertEqual(EXPECTED[:3], run_stage.STAGE_ADAPTERS["data-stage-1a"][2])
        self.assertEqual(EXPECTED[3:], run_stage.STAGE_ADAPTERS["data-stage-1b"][2])
        manifest = self.root / "1_DATA/canonical-data-provenance.json"
        os.utime(manifest, (1, 1))
        news_relative = run_stage.COMMAND_ALLOWLIST["data-collect-news"][0]
        self.assertEqual(news_relative, run_stage.COMMAND_ALLOWLIST["data-commit-news"][0])
        # data-collect-news and data-commit-news share one script file; the
        # stub distinguishes them the same way the real script does, by argv.
        write_script(self.root, news_relative,
                     "import sys\n"
                     "mode = 'data-commit-news' if '--commit' in sys.argv[1:] else 'data-collect-news'\n"
                     "with open('1_DATA/order.txt', 'a') as f: f.write(mode + '\\n')")
        for name in EXPECTED:
            if name in ("data-collect-news", "data-commit-news"):
                continue
            body = "with open('1_DATA/order.txt', 'a') as f: f.write(%r)" % (name + "\n")
            if name == "data-refresh-canonical":
                body += "\npathlib.Path('1_DATA/canonical-data-provenance.json').write_text('{\"refreshed\":true}\\n')"
            if name == "data-validate-canonical":
                body += "\nassert 'refreshed' in pathlib.Path('1_DATA/canonical-data-provenance.json').read_text()"
            write_script(self.root, run_stage.COMMAND_ALLOWLIST[name][0], body)
        contract = run_stage._load_validated_request(DATA_CONTRACT, VERSION)
        run_stage._run_isolated(contract, self.root, 1, phase="a")
        self.assertFalse(run_stage._handoff_path(self.root, 1).exists())
        run_stage._run_isolated(contract, self.root, 1, phase="b")
        self.assertEqual(list(EXPECTED), (self.root / "1_DATA/order.txt").read_text().splitlines())
        handoff = json.loads(run_stage._handoff_path(self.root, 1).read_text())
        self.assertEqual(["1_DATA/canonical-data-provenance.json#sha256:" + run_stage._sha256(manifest)], handoff["input_manifest_refs"])

    def test_exact_network_properties_and_native_profiles(self):
        self.assertEqual(set(EXPECTED[:3]), {name for name, mode in run_stage.COMMAND_CHILDPOLICY.items() if mode == "https"})
        for name, command in run_stage.COMMAND_ALLOWLIST.items():
            def inspect(argv, **kwargs):
                profile = pathlib.Path(argv[2]).read_text()
                allowed = name in EXPECTED[:3]
                self.assertEqual(allowed, '(allow network-outbound (remote tcp "*:443"))' in profile)
                self.assertEqual(allowed, 'mDNSResponder' in profile)
                self.assertNotIn('(allow network*)', profile)
                self.assertNotIn('(allow network-outbound)', profile)
                self.assertIn('(deny default)', profile)
                self.assertEqual(run_stage.COMMAND_CHILDPOLICY[name], argv[argv.index("isolated", 8) + 1])
                self.assertEqual(name in {'outputs-intake-seal', 'delivery-publish'},
                                 '(allow process-exec (literal "/usr/bin/git"))' in profile)
                self.assertIn('(allow file-write* (subpath %s))' % json.dumps(str(self.root / command[0].split('/')[0])), profile)
                return subprocess.CompletedProcess(argv, 0)
            with self.subTest(command=name), mock.patch.object(run_stage.subprocess, 'run', side_effect=inspect):
                run_stage._run_allowlisted_command(self.root, name, command[0].split('/')[0])

    def test_real_refresh_runs_under_the_audited_child_in_both_modes(self):
        relative = run_stage.COMMAND_ALLOWLIST['data-refresh-canonical'][0]
        script = self.root / relative
        script.write_bytes((pathlib.Path(run_stage.__file__).resolve().parents[2] / relative).read_bytes())
        roots = ['research/raw', 'research/derived', 'research/trackers',
                 'research/federal', 'geo', 'research/states']
        for root in roots:
            (self.root / '1_DATA' / root).mkdir(parents=True, exist_ok=True)
        manifest = self.root / '1_DATA/canonical-data-provenance.json'
        manifest.write_text(json.dumps({'roots': roots, 'files': [],
            'methodology_inputs': [], 'format_exceptions': {'entries': []}}))
        (self.root / '1_DATA/research/raw/new.txt').write_text('canonical')
        # fixture_root() already seeds research/trackers/ge16-news-candidates.json
        # and ge16-news-judged.json as phase b's freshness/judging evidence; both
        # are legitimate canonical tracker files (refresh_canonical_data.py's own
        # NEWS mapping), so both are picked up alongside new.txt.
        expected_files = 3
        for pins in (None, {relative: run_stage._sha256(script)}):
            before = run_stage._inventory(self.root)
            run_stage._run_allowlisted_command(self.root, 'data-refresh-canonical', '1_DATA', live_script_hashes=pins)
            self.assertEqual(expected_files, len(json.loads(manifest.read_text())['files']))
            run_stage._assert_only_owner_changed(before, self.root, '1_DATA')

    def test_non_collectors_cannot_connect_even_to_https(self):
        for name, command in run_stage.COMMAND_ALLOWLIST.items():
            if name in EXPECTED[:3]:
                continue
            # sys.audit triggers the actual hook before any packet is emitted.
            body = "import socket, sys\ns = socket.socket()\ntry:\n    sys.audit('socket.connect', s, ('127.0.0.1', 443))\nexcept PermissionError:\n    pass\nelse:\n    raise AssertionError('network permitted')"
            write_script(self.root, command[0], body)
            run_stage._run_allowlisted_command(self.root, name, command[0].split('/')[0])

    def test_collectors_allow_only_outbound_tcp_443_and_owner_writes(self):
        for name in EXPECTED[:3]:
            command = run_stage.COMMAND_ALLOWLIST[name]
            write_script(self.root, command[0], """import socket, sys
s = socket.socket()
sys.audit('socket.connect', s, ('127.0.0.1', 443))
for event, args in [('socket.connect', (s, ('127.0.0.1', 80))),
                    ('socket.connect', (socket.socket(type=socket.SOCK_DGRAM), ('127.0.0.1', 443))),
                    ('socket.bind', (s, ('127.0.0.1', 443))),
                    ('socket.sendto', (s, ('127.0.0.1', 443)))]:
    try: sys.audit(event, *args)
    except PermissionError: pass
    else: raise AssertionError('forbidden network permitted')
pathlib.Path('1_DATA/allowed.txt').write_text('ok')
try: pathlib.Path('2_ANALYTICS/escaped.txt').write_text('bad')
except PermissionError: pass
else: raise AssertionError('cross-domain write permitted')
""")
            run_stage._run_allowlisted_command(self.root, name, '1_DATA')
            self.assertFalse((self.root / '2_ANALYTICS/escaped.txt').exists())

    def test_data_commit_news_is_network_deny(self):
        self.assertEqual("deny", run_stage.COMMAND_CHILDPOLICY["data-commit-news"])

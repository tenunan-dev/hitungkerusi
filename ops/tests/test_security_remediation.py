"""Stage 3/4 attack replays use fresh isolated roots and real parent Git."""
import ast
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from cron import run_stage
from validate_ops_contract import ContractError, validate_contract
try:
    import test_isolated_stage_adapters as _tis
except ModuleNotFoundError as error:
    if error.name != "test_isolated_stage_adapters":
        raise
    sys.path.insert(0, os.path.dirname(__file__))
    import test_isolated_stage_adapters as _tis
from test_isolated_stage_adapters import (
    RealParentFixtureSandboxTests, fixture_root, write_script, invoke,
    data_phase, DATA_CONTRACT, ANALYTICS_CONTRACT, OUTPUTS_CONTRACT,
    DELIVERY_CONTRACT, VERSION, NATIVE_SANDBOX_AVAILABLE, assert_execution_imports,
)


# Direct fork_exec deliberately bypasses the Python subprocess audit event.
FORK_EXEC_ATTACK = '''
import _posixsubprocess, os, sys
argv = [b'git', b'-C', os.fsencode(pathlib.Path('3_OUTPUTS').resolve()),
        b'-c', b'user.name=Attack', b'-c', b'user.email=attack@example.invalid',
        b'-c', b'commit.gpgsign=false', b'commit', b'--allow-empty', b'-m', b'A18b']
r, w = os.pipe()
args = [argv, [b'/usr/bin/git'], True, (w,), None, None,
        -1, -1, -1, -1, -1, -1, r, w, True, False]
if sys.version_info >= (3, 11): args.append(-1)  # process_group
args.extend([None, None, None, -1, None])
if (3, 11) <= sys.version_info < (3, 14): args.append(False)  # use_vfork
pid = _posixsubprocess.fork_exec(*args)
os.close(w)
error = os.read(r, 4096)
os.close(r)
_, status = os.waitpid(pid, 0)
rc = os.waitstatus_to_exitcode(status)
pathlib.Path('3_OUTPUTS/attack-rc').write_text(str(rc))
raise SystemExit(rc)
'''


class SecurityRemediationTests(RealParentFixtureSandboxTests):
    def setUp(self):
        super().setUp()
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def command(self, body, hashes=None):
        name = 'outputs-intake-seal'
        write_script(self.root, run_stage.COMMAND_ALLOWLIST[name][0], body)
        compile((self.root / run_stage.COMMAND_ALLOWLIST[name][0]).read_bytes(), '<attack-fixture>', 'exec')
        if hashes is not None:
            hashes[run_stage.COMMAND_ALLOWLIST[name][0]] = run_stage._sha256(self.root / run_stage.COMMAND_ALLOWLIST[name][0])
        run_stage._run_allowlisted_command(self.root, name, '3_OUTPUTS', live_script_hashes=hashes)

    def prepare_pipeline(self):
        run_stage._initialize_isolated_repositories(self.root)
        for contract in (DATA_CONTRACT, ANALYTICS_CONTRACT):
            status, record = invoke('--contract', contract, '--version', VERSION, '--dry-run',
                                    '--isolated-root', str(self.root), *data_phase(contract))
            self.assertEqual(0, status, record)

    def stage(self, contract):
        return invoke('--contract', contract, '--version', VERSION, '--dry-run',
                      '--isolated-root', str(self.root))

    def count(self):
        return len(run_stage._parent_git(self.root, '3_OUTPUTS', 'log', '--format=%H').splitlines())

    def test_real_stage3_seal_parent_commits_stage4_current(self):
        self.prepare_pipeline()
        before = self.count()
        stray = self.root / '3_OUTPUTS/manifest/stray.json'
        stray.parent.mkdir(exist_ok=True)
        stray.write_text('stray must stay untracked')
        for contract in (OUTPUTS_CONTRACT, DELIVERY_CONTRACT):
            status, record = self.stage(contract)
            self.assertEqual(0, status, record)
        seal = json.loads((self.root / '3_OUTPUTS/manifest/ge16-release-seal-input.json').read_text())
        self.assertEqual(before + 1, self.count())
        self.assertEqual(seal['sealed_commit'], run_stage._parent_git(self.root, '3_OUTPUTS', 'rev-parse', 'HEAD'))
        tracked = run_stage._parent_git(self.root, '3_OUTPUTS', 'ls-tree', '-r', '--name-only', 'HEAD')
        self.assertNotIn('manifest/stray.json', tracked)
        self.assertIn('releases/' + seal['release_id'] + '/manifest.json', tracked)
        current = self.root / '4_DELIVERY/current'
        self.assertTrue(current.is_symlink())
        self.assertEqual(current.resolve(), self.root / '4_DELIVERY/releases' / seal['release_id'])
        for domain in ('1_DATA', '2_ANALYTICS'):
            self.assertEqual([], run_stage._dirty_paths(self.root, domain))

    def test_A20_filter_plant_aborts_pipeline_without_parent_commit(self):
        self.prepare_pipeline()
        before = self.count()
        script = self.root / run_stage.COMMAND_ALLOWLIST['outputs-intake-seal'][0]
        script.write_text("import pathlib\np = pathlib.Path('3_OUTPUTS/.git/config')\np.write_text(p.read_text() + '\\n[filter \\\"escape\\\"]\\n clean = /usr/bin/id\\n')\n")
        compile(script.read_bytes(), str(script), 'exec')
        status, record = self.stage(OUTPUTS_CONTRACT)
        self.assertEqual(1, status, record)
        self.assertEqual(before, self.count())
        self.assertFalse((self.root / '4_DELIVERY/current').exists())

    def test_A21_attributes_swap_aborts_pipeline_without_parent_commit(self):
        self.prepare_pipeline()
        before = self.count()
        script = self.root / run_stage.COMMAND_ALLOWLIST['outputs-intake-seal'][0]
        script.write_text("import pathlib\npathlib.Path('3_OUTPUTS/.gitattributes').write_text('*.json filter=escape\\n')\n")
        compile(script.read_bytes(), str(script), 'exec')
        status, record = self.stage(OUTPUTS_CONTRACT)
        self.assertEqual(1, status, record)
        # The context guard is released after failure; reads still inspect content.
        self.assertEqual(before, self.count())
        self.assertFalse((self.root / '3_OUTPUTS/manifest/ge16-release-seal.json').exists())

    @unittest.skipUnless(NATIVE_SANDBOX_AVAILABLE, 'native Seatbelt unavailable: A18b cannot be attested by an audit fallback')
    def test_A18b_fork_exec_commit_refused_no_commit_lands(self):
        self.prepare_pipeline()
        before = self.count()
        refs = run_stage._refs_snapshot(self.root / '3_OUTPUTS')
        with self.assertRaises(run_stage.IsolatedRunError):
            self.command(FORK_EXEC_ATTACK)
        rc = int((self.root / '3_OUTPUTS/attack-rc').read_text())
        self.assertNotEqual(0, rc)
        self.assertEqual(before, self.count())
        run_stage._refs_unchanged(self.root / '3_OUTPUTS', refs)
        # A refused child leaves room only for the one authorized parent commit.
        fixture = (pathlib.Path(__file__).parent / 'fixtures/release_adapter.py').read_text()
        write_script(self.root, run_stage.COMMAND_ALLOWLIST['outputs-intake-seal'][0], fixture)
        self.assertEqual(0, self.stage(OUTPUTS_CONTRACT)[0])
        self.assertEqual(before + 1, self.count())

    def test_A18b_parent_invariant_aborts_if_native_fence_is_bypassed(self):
        # Explicitly remove ONLY Seatbelt in this isolated attack replay. The
        # actual fork_exec bypass still reaches Git; the real parent detects it.
        self.prepare_pipeline()
        before = self.count()
        def unfenced_child(command, **kwargs):
            self.assertEqual('/usr/bin/sandbox-exec', command[0])
            self.assertEqual(str(self.root), command[7])
            with subprocess.Popen(command[3:], **{key: value for key, value in kwargs.items()
                                                   if key != 'check'}) as process:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        with mock.patch.object(run_stage.subprocess, 'run', side_effect=unfenced_child):
            with self.assertRaisesRegex(run_stage.IsolatedRunError, 'changed owner Git refs'):
                self.command(FORK_EXEC_ATTACK)
        self.assertEqual('0', (self.root / '3_OUTPUTS/attack-rc').read_text())
        self.assertEqual(before + 1, self.count())
        self.assertFalse((self.root / '3_OUTPUTS/manifest/ge16-release-seal.json').exists())

    def test_refs_invariant_catches_changes_even_when_child_fails(self):
        run_stage._initialize_isolated_repositories(self.root)
        for relative in ('HEAD', 'refs/heads/attack', 'packed-refs'):
            path = self.root / '3_OUTPUTS/.git' / relative
            before = run_stage._refs_snapshot(self.root / '3_OUTPUTS')
            original = path.read_bytes() if path.exists() else None
            def mutate(*args, **kwargs):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('ref changed\n')
                return subprocess.CompletedProcess(args, 1)
            with self.subTest(relative=relative), mock.patch.object(run_stage.subprocess, 'run', side_effect=mutate):
                with self.assertRaisesRegex(run_stage.IsolatedRunError, 'changed owner Git refs'):
                    self.command('pass')
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
            run_stage._refs_unchanged(self.root / '3_OUTPUTS', before)

    def test_audit_denies_git_writes_in_ro_git_and_deny_modes(self):
        for name in ('outputs-intake-seal', 'analytics-forecast'):
            relative = run_stage.COMMAND_ALLOWLIST[name][0]
            owner = relative.split('/')[0]
            write_script(self.root, relative, "pathlib.Path(%r).mkdir()" % (owner + '/.git'))
            with self.assertRaises(run_stage.IsolatedRunError):
                run_stage._run_allowlisted_command(self.root, name, owner)

    def test_A16_A17_git_environment_overrides_rejected_none_allowed(self):
        self.command('''import sys
sys.audit('subprocess.Popen', 'git', ['git', 'status'], None, None)
for key in ('GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_INDEX_FILE', b'GIT_DIR'):
    try: sys.audit('subprocess.Popen', 'git', ['git', 'status'], None, {key: 'attack'})
    except PermissionError: pass
    else: raise AssertionError('Git env override accepted')
''')

    def test_parent_rejects_driver_configs_and_attributes_before_execution(self):
        run_stage._initialize_isolated_repositories(self.root)
        config = self.root / '3_OUTPUTS/.git/config'
        original = config.read_text()
        cases = ('[filter "evil"] clean = /usr/bin/id', '[filter "evil"]\n clean = /usr/bin/id', '[diff "evil"]\n textconv = /usr/bin/id',
                 'filter.evil = /usr/bin/id', '[core]\n pager = /bin/sh', '[core]\n fsmonitor = /bin/sh',
                 '[core]\n fsmonitorDaemon = /bin/sh', '[color]\n ui = !/bin/sh',
                 '[include]\n path = ../evil-config')
        for payload in cases:
            with self.subTest(payload=payload):
                config.write_text(original + '\n' + payload + '\n')
                with mock.patch.object(run_stage.subprocess, 'run') as child:
                    with self.assertRaises(run_stage.IsolatedRunError):
                        run_stage._parent_git(self.root, '3_OUTPUTS', 'status')
                    child.assert_not_called()
        config.write_text(original)
        for relative in ('.git/config.worktree', '.git/info/attributes', '.gitattributes', 'nested/.gitattributes'):
            path = self.root / '3_OUTPUTS' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('filter.evil = /usr/bin/id\n')
            with self.assertRaises(run_stage.IsolatedRunError):
                run_stage._parent_git(self.root, '3_OUTPUTS', 'status')
            path.unlink()
        with run_stage._parent_git_input_guard(self.root):
            config.write_text(original + '\n# harmless change still invalidates snapshot\n')
            with self.assertRaisesRegex(run_stage.IsolatedRunError, 'changed since child staging'):
                run_stage._parent_git(self.root, '3_OUTPUTS', 'status')

    def test_each_dynamic_module_pin_is_required_and_checked(self):
        pins = {p: run_stage._sha256(self.root / p) for p in run_stage.PINNED_MODULE_PATHS}
        for relative in run_stage.PINNED_MODULE_PATHS:
            path = self.root / relative
            original = path.read_bytes()
            path.write_bytes(original + b'\n# Stage 2 tamper\n')
            with self.subTest(relative=relative), self.assertRaises(run_stage.LiveValidationError):
                self.command('pass', dict(pins))
            path.write_bytes(original)
        contract = json.loads(run_stage.CONTRACT_PATH.read_text())
        for relative in run_stage.PINNED_MODULE_PATHS:
            modified = json.loads(json.dumps(contract))
            del modified['migration']['bounded_live_execution']['script_sha256'][relative]
            with self.assertRaises(ContractError):
                validate_contract(modified)
        modified = json.loads(json.dumps(contract))
        modified['migration']['bounded_live_execution']['modules_pinned'] = []
        with self.assertRaises(ContractError):
            validate_contract(modified)

    def test_dynamic_loader_rechecks_exact_source_after_child_swap(self):
        pins = {p: run_stage._sha256(self.root / p) for p in run_stage.PINNED_MODULE_PATHS}
        with self.assertRaises(run_stage.IsolatedRunError):
            self.command('''import importlib.util
p = pathlib.Path('3_OUTPUTS/scripts/validate_release_intake.py')
p.write_text("pathlib.Path('3_OUTPUTS/pin-bypassed').touch()")
spec = importlib.util.spec_from_file_location('evil', p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
''', pins)
        self.assertFalse((self.root / '3_OUTPUTS/pin-bypassed').exists())

    def test_pinned_module_symlink_cannot_bypass_parent_or_loader_check(self):
        relative = '3_OUTPUTS/scripts/validate_release_intake.py'
        path = self.root / relative
        pins = {p: run_stage._sha256(self.root / p) for p in run_stage.PINNED_MODULE_PATHS}
        original = path.read_bytes()
        target = self.root / '3_OUTPUTS/copied-validator.py'
        target.write_bytes(original)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(run_stage.IsolatedRunError, 'unsafe pinned release module'):
            self.command('pass', dict(pins))
        path.unlink()
        path.write_bytes(original)
        with self.assertRaises(run_stage.IsolatedRunError):
            self.command("""import importlib.util
p = pathlib.Path('3_OUTPUTS/scripts/validate_release_intake.py')
p.unlink()
p.symlink_to(pathlib.Path('3_OUTPUTS/copied-validator.py').resolve())
spec = importlib.util.spec_from_file_location('evil_link', p)
spec.loader.exec_module(importlib.util.module_from_spec(spec))
""", dict(pins))

    def test_committed_validator_source_is_verified_before_exec(self):
        pins = {p: run_stage._sha256(self.root / p) for p in run_stage.PINNED_MODULE_PATHS}
        for filename in ('a' * 40 + ':validate_release_intake.py', 'b' * 40 + ':scripts/validate_release_intake.py'):
            with self.subTest(filename=filename), self.assertRaises(run_stage.IsolatedRunError):
                self.command("exec(compile(b'raise RuntimeError(123)', %r, 'exec'), {})" % filename, dict(pins))

    def test_committed_validator_pin_accepts_only_exact_historical_bytes(self):
        pins = {p: run_stage._sha256(self.root / p) for p in run_stage.PINNED_MODULE_PATHS}
        filename = next(iter(run_stage.COMMITTED_MODULE_SHA256))
        source = b"answer = 42\n"
        historical = {filename: hashlib.sha256(source).hexdigest()}
        with mock.patch.object(run_stage, 'COMMITTED_MODULE_SHA256', historical):
            self.command("ns = {}; exec(compile(%r, %r, 'exec'), ns); assert ns['answer'] == 42" % (source, filename), dict(pins))
            with self.assertRaises(run_stage.IsolatedRunError):
                self.command("exec(compile(%r, %r, 'exec'), {})" % (source + b'# tamper', filename), dict(pins))
        contract = json.loads(run_stage.CONTRACT_PATH.read_text())
        contract['migration']['bounded_live_execution']['committed_module_sha256'][filename] = '0' * 64
        with self.assertRaises(ContractError):
            validate_contract(contract)

    def test_outputs_dirty_preflight_is_fail_closed(self):
        path = run_stage.REPOSITORY_ROOT / '2_ANALYTICS/automation/outputs/stage_current_release.py'
        spec = importlib.util.spec_from_file_location('security_stage_current', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(module)
        def facts(path, label, sha):
            return path, ([{'code': 'outputs_dirty', 'paths': ['stray']}] if label == 'OUTPUTS' else [])
        with mock.patch.object(module, '_repository_facts', side_effect=facts), \
                mock.patch.object(module, '_data_provenance_error', return_value=None), \
                mock.patch.object(module, '_load_contract_entries', return_value=[]), \
                mock.patch.object(module, '_payload_sources', return_value=({}, None)):
            plan = module.build_plan(self.root/'2_ANALYTICS', self.root/'1_DATA', self.root/'3_OUTPUTS',
                                     'O-fixture', 'a'*40, 'b'*40)
        self.assertIn({'code': 'outputs_dirty', 'paths': ['stray']}, plan.report['unsafe'])

    def test_profiles_keep_deny_and_https_without_fork_or_git_exec(self):
        for name in ('data-collect-news', 'analytics-forecast', 'outputs-intake-seal'):
            owner = run_stage.COMMAND_ALLOWLIST[name][0].split('/')[0]
            profiles = []
            def capture(command, **kwargs):
                profiles.append(pathlib.Path(command[2]).read_text())
                return subprocess.CompletedProcess(command, 0)
            with mock.patch.object(run_stage.subprocess, 'run', side_effect=capture):
                run_stage._run_allowlisted_command(self.root, name, owner)
            profile = profiles[0]
            allow = '(allow file-write* (subpath %s))' % json.dumps(str(self.root / owner))
            deny = '(deny file-write* (subpath %s))' % json.dumps(str(self.root / owner / '.git'))
            self.assertGreater(profile.index(deny), profile.index(allow))
            if run_stage.COMMAND_CHILDPOLICY[name] != 'ro-git':
                self.assertNotIn('process-fork', profile)
                self.assertNotIn('(allow process-exec (literal "/usr/bin/git"))', profile)
                self.assertNotIn('(allow process-exec (literal "/Library/Developer/CommandLineTools/usr/bin/git"))', profile)

    def test_execution_surface_rejects_injected_imports_and_calls(self):
        baseline = pathlib.Path(run_stage.__file__).read_text()
        for source in ('import subprocess', 'from subprocess import Popen', 'import _posixsubprocess',
                       'from asyncio import create_subprocess_exec', 'os.system("id")', 'os.fork()',
                       'os.posix_spawn("x", [], {})', 'os.spawnve(0,"x",[],{})', '_posixsubprocess()'):
            with self.subTest(source=source), self.assertRaises(AssertionError):
                assert_execution_imports(self, ast.parse(baseline + '\n' + source))

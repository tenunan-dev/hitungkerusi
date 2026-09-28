"""Read-only Git child policy and release sequencing regression coverage."""
import os
import pathlib
import sys

from cron import run_stage
try:
    import test_isolated_stage_adapters
except ModuleNotFoundError as error:
    if error.name != "test_isolated_stage_adapters":
        raise
    sys.path.insert(0, os.path.dirname(__file__))
    import test_isolated_stage_adapters

FixtureSandboxTests = test_isolated_stage_adapters.FixtureSandboxTests
fixture_root = test_isolated_stage_adapters.fixture_root
write_script = test_isolated_stage_adapters.write_script


class ReadOnlyGitFenceTests(FixtureSandboxTests):
    def setUp(self):
        super().setUp()
        temporary = fixture_root()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def test_ro_git_allows_only_listed_read_verbs(self):
        body = '''import subprocess, sys
for verb in ('rev-parse', 'status', 'show', 'log', 'cat-file', 'ls-tree'):
    argv = ['git', verb] + (['--porcelain'] if verb == 'status' else [])
    sys.audit('subprocess.Popen', 'git', argv, None, None)
# A real process proves the permission reaches execution; this needs no repo.
assert subprocess.run(['git', 'rev-parse', '--local-env-vars'], capture_output=True).returncode == 0
'''
        for name in ('outputs-intake-seal', 'delivery-publish'):
            relative = run_stage.COMMAND_ALLOWLIST[name][0]
            write_script(self.root, relative, body)
            run_stage._run_allowlisted_command(self.root, name, relative.split('/')[0])

    def test_ro_git_rejects_mutations_other_binaries_and_option_escapes(self):
        body = '''import subprocess, sys
# Owner decision A: git status and git ls-tree are permitted, without writes.
for argv in (['git', 'status'], ['git', 'ls-tree', 'HEAD']):
    for event in ('subprocess.Popen', 'os.exec', 'os.posix_spawn'):
        args = (argv[0], argv, None, None) if event == 'subprocess.Popen' else (argv[0], argv, {})
        sys.audit(event, *args)  # Allowed assertion: neither verb raises.
    result = subprocess.run(argv, capture_output=True, text=True)
    # The isolated fixture intentionally has no repository; Git still executes.
    assert result.returncode == 128 and 'not a git repository' in result.stderr
bad = [['git', verb] for verb in ('add', 'commit', 'push', 'checkout', 'reset', 'rebase', 'merge', 'tag', 'remote')]
bad += [['python3', '-c', 'pass'], ['sh', '-c', 'true'], ['git', '-c', 'alias.x=commit', 'x'],
        ['git', 'show', '--output=escaped'], ['git', 'show', '--ext-diff']]
for argv in bad:
    for event in ('subprocess.Popen', 'os.exec', 'os.posix_spawn'):
        args = (argv[0], argv, None, None) if event == 'subprocess.Popen' else (argv[0], argv, {})
        try: sys.audit(event, *args)
        except PermissionError: pass
        else: raise AssertionError('forbidden child permitted: ' + repr(argv))
try: sys.audit('subprocess.Popen', '/bin/sh', ['git', 'show'], None, None)
except PermissionError: pass
else: raise AssertionError('executable substitution permitted')
'''
        for name in ('outputs-intake-seal', 'delivery-publish'):
            relative = run_stage.COMMAND_ALLOWLIST[name][0]
            write_script(self.root, relative, body)
            before = run_stage._inventory(self.root)
            run_stage._run_allowlisted_command(self.root, name, relative.split('/')[0])
            self.assertEqual(before, run_stage._inventory(self.root))

    def test_deny_and_https_commands_cannot_start_any_process(self):
        body = '''import sys
for argv in (['git', 'rev-parse', 'HEAD'], ['git', 'commit'], ['python3', '-V']):
    try: sys.audit('subprocess.Popen', argv[0], argv, None, None)
    except PermissionError: pass
    else: raise AssertionError('child process permitted')
'''
        for name, mode in run_stage.COMMAND_CHILDPOLICY.items():
            if mode == 'ro-git':
                continue
            relative = run_stage.COMMAND_ALLOWLIST[name][0]
            write_script(self.root, relative, body)
            run_stage._run_allowlisted_command(self.root, name, relative.split('/')[0])

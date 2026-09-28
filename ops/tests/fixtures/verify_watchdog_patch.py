#!/usr/bin/env python3
"""Apply the follow-up patch only to temp copies; test actual job entry points."""
import ast
import pathlib
import shutil
import subprocess
import tempfile
from unittest import mock

OPS = pathlib.Path(__file__).resolve().parents[2]
SOURCE = pathlib.Path('/Users/faisal.muthalib/.hermes/hermes-agent')


def main():
    with tempfile.TemporaryDirectory(prefix='ge16-watchdog-patch-') as directory:
        root = pathlib.Path(directory)
        (root / 'cron').mkdir()
        for name in ('jobs.py', 'scheduler.py'):
            shutil.copyfile(SOURCE / 'cron' / name, root / 'cron' / name)
        result = subprocess.run(['/usr/bin/patch', '-p1', '-i', str(OPS / 'cron/patches/scheduler-per-job-inactivity.patch')],
                                cwd=root, capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        print('follow-up patch applied to temporary copies only: PASS')
        jobs = ast.parse((root / 'cron/jobs.py').read_text())
        wanted = {'_validate_inactivity_limit', 'create_job', '_normalize_job_updates'}
        nodes = [node for node in jobs.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        # Postpone annotations, and never import or execute live cron module code.
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        sentinel = object()
        scope = {'_INACTIVITY_UNSET': sentinel, '_UPDATE_FIELD_NORMALIZERS': {}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), '<patched-jobs>', 'exec'), scope)
        schedule = mock.Mock(side_effect=RuntimeError('schedule-boundary'))
        persist = mock.Mock()
        scope.update(parse_schedule=schedule, save_jobs=persist)
        bad_values = (True, False, 0, -1, 3601, 10**9, None, '1800', 1.5)
        for value in bad_values:
            for name, args, kwargs in (
                ('create_job', ('fixture', '0 22 * * 5'), {'inactivity_limit': value}),
                ('_normalize_job_updates', ({}, {'inactivity_limit': value}), {}),
            ):
                try:
                    scope[name](*args, **kwargs)
                except ValueError:
                    pass
                else:
                    raise AssertionError((name, value, 'accepted'))
        schedule.assert_not_called()
        persist.assert_not_called()
        for value in (1, 1800, 3600, sentinel):
            kwargs = {} if value is sentinel else {'inactivity_limit': value}
            try:
                scope['create_job']('fixture', '0 22 * * 5', **kwargs)
            except RuntimeError as error:
                assert str(error) == 'schedule-boundary'
            else:
                raise AssertionError('valid creation never reached scheduling')
        print('create/update reject 9 invalid values before scheduling/persistence: PASS')
        print('create accepts 1, 1800, 3600 and absent override: PASS')


if __name__ == '__main__':
    main()

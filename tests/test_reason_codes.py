"""Fork SN: a rejected or failed result carries a mandatory reason code."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_deepseek_team import coordination
from tests import test_job_integration as job_tests
from tests.test_job_coordination_cli import FinalizationCliTests
from tests.test_workspace_lifecycle import LifecycleFixture


class ReasonCodeCliTests(LifecycleFixture):
    task = job_tests.JobIntegrationTests.task
    run_attempt = job_tests.JobIntegrationTests.run_attempt
    command = FinalizationCliTests.command

    def row(self, task_id, assignment):
        return coordination._assignment(coordination.load_task(self.source, task_id), assignment)

    def test_failed_run_rejection_requires_a_code_and_stores_it(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'failed')
        before = coordination._task_path(self.source, task['id']).read_bytes()
        code, _out, err = self.command('coordination', 'use', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--disposition', 'rejected',
            '--evidence', 'tests stay red')
        self.assertEqual(code, 64)
        self.assertIn('--reason-code', err)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)
        self.assertTrue(copy.path.exists())
        code, _out, err = self.command('coordination', 'use', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--disposition', 'rejected',
            '--evidence', 'tests stay red', '--reason-code', 'tests-fail')
        self.assertEqual(code, 0, err)
        self.assertFalse(copy.path.exists())
        self.assertEqual(self.row(task['id'], assignment)['reason_code'], 'tests-fail')
        report = coordination.reason_report(
            (coordination._project_dir(self.source) / 'tasks').glob('task-*.json'))
        self.assertEqual(report['reasons'], {'tests-fail': 1})
        # an idempotent repeat neither needs nor rewrites a code
        before = coordination._task_path(self.source, task['id']).read_bytes()
        code, _out, err = self.command('coordination', 'finalize', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--evidence', 'repeat', '--cancel')
        self.assertEqual(code, 0, err)
        self.assertEqual(coordination._task_path(self.source, task['id']).read_bytes(), before)

    def test_rejected_use_requires_a_code(self):
        self.require_capture()
        task, assignment = self.task()
        copy = self.copy()
        self.run_attempt(task, assignment, copy, 'succeeded')
        code, _out, err = self.command('coordination', 'use', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--disposition', 'rejected',
            '--evidence', 'changed the wrong module', '--final')
        self.assertEqual(code, 64)
        self.assertTrue(copy.path.exists())
        code, _out, err = self.command('coordination', 'use', '--path', str(self.source),
            '--task', task['id'], '--assignment', assignment, '--disposition', 'rejected',
            '--evidence', 'changed the wrong module', '--final', '--reason-code', 'wrong-change')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.row(task['id'], assignment)['reason_code'], 'wrong-change')

    def test_unknown_code_is_refused_by_the_parser(self):
        code, _out, err = self.command('coordination', 'finalize', '--path', str(self.source),
            '--task', 'task-' + '0' * 20, '--assignment', 'a', '--evidence', 'x', '--cancel',
            '--reason-code', 'bad-luck')
        self.assertEqual(code, 2)
        self.assertIn('invalid choice', err)


class ReasonReportTests(unittest.TestCase):
    def test_grouping_and_unknown_for_older_rows(self):
        rows = [
            {'status': 'succeeded', 'disposition': {'kind': 'incorporated'}},
            {'status': 'succeeded', 'disposition': {'kind': 'reproduced'}},
            {'status': 'failed', 'disposition': {'kind': 'incorporated'}},
            {'status': 'failed', 'disposition': {'kind': 'rejected'}, 'reason_code': 'oom'},
            {'status': 'failed', 'disposition': {'kind': 'rejected'}},
            {'status': 'succeeded', 'disposition': {'kind': 'rejected'}, 'reason_code': 'out-of-scope'},
            {'status': 'succeeded', 'disposition': {'kind': 'needs-rework'}},
            {'status': 'running', 'disposition': None},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'task-00000000000000000000.json'
            path.write_text(json.dumps({'assignments': rows}))
            (Path(tmp) / 'task-broken.json').write_text('{')
            report = coordination.reason_report(sorted(Path(tmp).glob('task-*.json')))
        self.assertEqual(report['assignments'], 8)
        self.assertEqual(report['outcomes'], {'incorporated': 3, 'failed': 2, 'rejected': 1,
                                              'rework': 1, 'pending': 1})
        self.assertEqual(report['reasons'], {'oom': 1, 'unknown': 1, 'out-of-scope': 1})

    def test_check_reason_code(self):
        coordination.check_reason_code(None, False)
        coordination.check_reason_code('timeout', True)
        for code, required in ((None, True), ('nope', False)):
            with self.assertRaises(coordination.CoordinationError) as caught:
                coordination.check_reason_code(code, required)
            self.assertEqual(caught.exception.code, 64)


if __name__ == '__main__':
    unittest.main()

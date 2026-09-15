import tempfile
import unittest
from pathlib import Path

from assistant import Assistant
from ehall_submit import EhallSubmitService, fingerprint
from ehall_tasks import EhallTasks
from ehall_worker import EhallWorker
from persistence import AppError
from test_assistant import ScriptedClient
from test_ehall_tasks import URL


class PreviewAdapter:
    name = 'preview_form'
    version = 1
    transaction_name = '合成高后果事务'

    def matches(self, url):
        return '/wdkb/' in url

    def consequences(self, values):
        return {'action': '退选合成课程', 'warnings': ['结果可能不可恢复。'],
                'submission_mode': 'confirmed_once'}


class Operation:
    def __init__(self, fingerprint, result=None, prepare_error=None, submit_error=None,
                 confirmation_error=None):
        self.fingerprint = fingerprint
        self.result = result or {'status': 'succeeded', 'evidence': {'receipt': 'fake'}}
        self.prepare_error = prepare_error
        self.submit_error = submit_error
        self.confirmation_error = confirmation_error
        self.calls = 0

    def prepare(self, content):
        if self.prepare_error:
            raise self.prepare_error
        return self.fingerprint

    def submit_once(self):
        self.calls += 1
        if self.submit_error:
            raise self.submit_error
        return self.result

    def open_confirmation(self):
        if self.confirmation_error:
            raise self.confirmation_error
        return {'prompt': '是否确认退出合成课程 A'}


class SubmitBackend:
    def __init__(self):
        self.operation_instance = None

    def operation(self, task, adapter):
        class DynamicOperation:
            calls = 0

            def prepare(inner, content):
                return fingerprint(content)

            def submit_once(inner):
                inner.calls += 1
                return {'status': 'succeeded',
                        'evidence': {'kind': 'fake-site-receipt', 'code': '0'}}
        self.operation_instance = DynamicOperation()
        return self.operation_instance


class EhallSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        personal = root / 'personal'
        personal.mkdir()
        (personal / 'profile.md').write_text('# empty\n', encoding='utf-8')
        assistant = Assistant({'model': 'test', 'personal_data_dir': str(personal),
                               'max_steps': 4}, ScriptedClient([]), root / 'tasks.sqlite')
        adapter = PreviewAdapter()
        self.tasks = EhallTasks(assistant, {adapter.name: adapter})
        self.submit = EhallSubmitService(self.tasks)

    def ready(self):
        task = self.tasks.create(URL)
        task.update(status='preview_ready', adapter='preview_form', adapter_version=1,
                    transaction_name='合成高后果事务', field_version=1,
                    page_structure='page-v1', preparation_fingerprint='prep-v1',
                    prepared_values={'course': 'course-a'}, attachments=[], facts=[], decisions=[],
                    fields=[{'id': 'course', 'label': '课程', 'source': 'decision',
                             'required': True, 'input': 'choice',
                             'options': [{'value': 'course-a', 'label': '合成课程 A'}]}],
                    fill_readback={'submitted': False, 'field_version': 1,
                                   'page_structure': 'page-v1',
                                   'values': {'course': 'course-a'}})
        self.tasks.save(task)
        return task

    def confirmed(self):
        task = self.ready()
        preview = self.submit.preview(task['task_id'], 1)
        self.submit.confirm(task['task_id'], preview['id'], preview['fingerprint'], True)
        return task['task_id'], preview

    def test_preview_is_frozen_and_confirmation_is_exact(self):
        task = self.ready()
        preview = self.submit.preview(task['task_id'], 1)
        self.assertEqual(preview['content']['fields'][0]['display'], '合成课程 A')
        self.assertEqual(preview['content']['consequences']['submission_mode'], 'confirmed_once')
        with self.assertRaises(AppError):
            self.submit.confirm(task['task_id'], preview['id'], 'wrong', True)
        with self.assertRaises(AppError):
            self.submit.confirm(task['task_id'], preview['id'], preview['fingerprint'], False)
        self.assertEqual(self.submit.confirm(task['task_id'], preview['id'],
                                             preview['fingerprint'], True)['status'], 'confirmed')

    def test_change_invalidates_confirmation(self):
        task_id, preview = self.confirmed()
        task = self.tasks.get(task_id)
        task['page_structure'] = 'changed'
        self.tasks.save(task)
        with self.assertRaises(AppError):
            self.submit.execute(task_id, preview['id'], Operation(preview['fingerprint']))

    def test_edit_after_confirmation_invalidates_old_version(self):
        task_id, preview = self.confirmed()
        task = self.tasks.edit(task_id, 1, {'course': 'course-a'})
        self.assertEqual(task['field_version'], 2)
        self.assertNotIn('confirmed_preview_id', task)
        with self.assertRaises(AppError):
            self.submit.execute(task_id, preview['id'], Operation(preview['fingerprint']))

    def test_unknown_result_blocks_retry(self):
        task_id, preview = self.confirmed()
        operation = Operation(preview['fingerprint'], {'status': 'unknown'})
        record = self.submit.execute(task_id, preview['id'], operation)
        self.assertEqual((record['status'], record['stage'], record['click_count']),
                         ('unknown', 'after_click', 1))
        with self.assertRaises(AppError):
            self.submit.execute(task_id, preview['id'], operation)

    def test_fake_success_calls_once_and_blocks_second_execution(self):
        task_id, preview = self.confirmed()
        operation = Operation(preview['fingerprint'])
        record = self.submit.execute(task_id, preview['id'], operation)
        self.assertEqual(record['status'], 'succeeded')
        self.assertEqual(operation.calls, 1)
        with self.assertRaises(AppError):
            self.submit.execute(task_id, preview['id'], operation)
        self.assertEqual(operation.calls, 1)

    def test_before_click_failure_is_retryable_failure(self):
        task_id, preview = self.confirmed()
        operation = Operation(preview['fingerprint'], prepare_error=RuntimeError('offline'))
        record = self.submit.execute(task_id, preview['id'], operation)
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(operation.calls, 0)

    def test_click_exception_is_unknown(self):
        task_id, preview = self.confirmed()
        operation = Operation(preview['fingerprint'], submit_error=TimeoutError('timeout'))
        record = self.submit.execute(task_id, preview['id'], operation)
        self.assertEqual(record['status'], 'unknown')
        self.assertEqual(operation.calls, 1)

    def test_confirmation_dialog_failure_is_before_click(self):
        task_id, preview = self.confirmed()
        operation = Operation(preview['fingerprint'], confirmation_error=RuntimeError('covered'))
        record = self.submit.execute(task_id, preview['id'], operation)
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['click_count'], 0)
        self.assertEqual(operation.calls, 0)

    def test_reconcile_requires_evidence_for_success(self):
        task_id, preview = self.confirmed()
        record = self.submit.execute(task_id, preview['id'],
                                     Operation(preview['fingerprint'], {'status': 'unknown'}))
        with self.assertRaises(AppError):
            self.submit.reconcile(task_id, record['id'],
                                  lambda submission, frozen: {'status': 'succeeded'})
        result = self.submit.reconcile(task_id, record['id'], lambda submission, frozen: {
            'status': 'succeeded', 'evidence': {'receipt': 'fake-read-only'}})
        self.assertEqual(result['status'], 'succeeded')

    def test_restart_recovery_is_conservative(self):
        task_id, preview = self.confirmed()
        _, _, record = self.submit._begin(task_id, preview['id'], 'automatic')
        recovered = EhallSubmitService(self.tasks).recover()
        self.assertEqual(recovered[0]['status'], 'failed')
        # Simulate a persisted pre-click boundary on another task.
        task_id, preview = self.confirmed()
        _, _, record = self.submit._begin(task_id, preview['id'], 'automatic')
        record['stage'] = 'before_click'
        self.submit._save_submission(record)
        recovered = EhallSubmitService(self.tasks).recover()
        self.assertEqual(recovered[0]['status'], 'unknown')

    def test_stage4e_end_to_end_submit_archive_restore_preserves_audit(self):
        task_id, preview = self.confirmed()
        backend = SubmitBackend()
        worker = EhallWorker(self.tasks, backend)
        queued = worker.enqueue_submit(task_id, preview['id'])
        self.assertEqual(queued['status'], 'queued')
        done = worker.run_once()
        self.assertEqual((done['status'], done['task_status']), ('done', 'succeeded'))
        self.assertEqual(backend.operation_instance.calls, 1)
        self.tasks.archive(task_id)
        self.assertNotIn(task_id, {item['task_id'] for item in self.tasks.summaries()})
        self.assertIn(task_id, {item['task_id'] for item in
                                self.tasks.summaries(archived=True)})
        reopened = EhallTasks(self.tasks.assistant, self.tasks.adapters)
        self.assertEqual(EhallSubmitService(reopened).submissions(task_id)[0]['status'],
                         'succeeded')
        reopened.restore(task_id)
        self.assertIn(task_id, {item['task_id'] for item in reopened.summaries()})


if __name__ == '__main__':
    unittest.main()
